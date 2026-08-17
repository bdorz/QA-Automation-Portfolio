import argparse
import ast
import importlib.util
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from types import ModuleType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from slack import (
    SlackProgressClient,
    SlackProgressState,
    SlackRunLink,
    SlackTestSummary,
    get_slack_progress_interval_seconds,
    get_slack_webhook_url,
    send_test_run_finished,
)
from testrail_client import (
    BLOCKED,
    FAILED,
    PASSED,
    CaseExecutionSummary,
    TestRailClient,
    get_env_bool,
    get_env_int,
    get_env_str,
    get_last_case_execution_summary,
    get_status_mapping,
    load_dotenv,
)

# 路徑設定
PROJECT_ROOT = Path(__file__).resolve().parent
TEST_DIR = PROJECT_ROOT / "TEST"

# 執行模式
RUN_MODE_MULTI = 2
RUN_MODE_DAILY = 3

# 專案預設值
DEFAULT_MULTI_RUN_STRATEGY = "existing"
DEFAULT_DAILY_RUN_STRATEGY = "new"
DEFAULT_EXISTING_RUN_ID = 0
DEFAULT_MULTI_RUN_NAME_PREFIX = "Automation Run"
DEFAULT_DAILY_RUN_NAME_PREFIX = "Daily Auto Check"
DEFAULT_TIMEZONE = "Asia/Taipei"

# 命名輔助工具
CASE_ID_PATTERN = re.compile(r"(?:^|[_-])c(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class TestCaseModule:
    """已載入測試案例模組的中繼資料。"""

    file_path: Path
    module: ModuleType
    case_id: int
    test_run_id: int | None


@dataclass(frozen=True)
class TestCaseReference:
    """不匯入模組時解析出的靜態測試案例識別資料。"""

    file_path: Path
    case_id: int | None
    test_run_id: int | None


@dataclass(frozen=True)
class RunInfo:
    """目前執行所解析出的 TestRail run 資訊。"""

    run_id: int
    run_name: str
    run_url: str


class SlackProgressNotifier:
    """在執行期間維持並更新同一則 Slack 訊息。"""

    def __init__(self, run_name: str, run_url: str, total: int, interval_seconds: int) -> None:
        self.run_name = run_name
        self.run_url = run_url
        self.total = total
        self.interval_seconds = interval_seconds
        self.client = SlackProgressClient.from_env()
        self.started_at = monotonic()
        self.completed = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.current_case = ""
        self.aborted = False
        self.abort_reason = ""
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._send_snapshot(finished=False)
        if self.interval_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="slack-progress", daemon=True)
        self._thread.start()

    def set_current_case(self, case_name: str) -> None:
        with self._lock:
            self.current_case = case_name

    def set_run_display(self, run_name: str, run_url: str = "") -> None:
        with self._lock:
            self.run_name = run_name
            self.run_url = run_url

    def record_case_summary(self, summary: CaseExecutionSummary) -> None:
        status_mapping = get_status_mapping()
        with self._lock:
            self.completed += 1
            if summary.status_id == status_mapping.passed:
                self.passed += 1
            elif summary.status_id == status_mapping.failed:
                self.failed += 1
            elif summary.status_id == status_mapping.blocked:
                self.skipped += 1
            self.current_case = summary.case_title or f"C{summary.case_id}"
        self._send_snapshot(finished=False)

    def finish(self, aborted: bool = False, abort_reason: str = "") -> None:
        """送出最後一則通知；`aborted` 代表執行是被例外中斷而非正常跑完。"""

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            self.aborted = aborted
            self.abort_reason = abort_reason
            # 中斷時保留最後執行到的案例名稱，方便從 Slack 直接看出斷在哪。
            if not aborted:
                self.current_case = "Completed"
        self._send_snapshot(finished=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._send_snapshot(finished=False)

    def _snapshot(self) -> SlackProgressState:
        with self._lock:
            return SlackProgressState(
                run_name=self.run_name,
                total=self.total,
                completed=self.completed,
                passed=self.passed,
                failed=self.failed,
                skipped=self.skipped,
                run_url=self.run_url,
                current_case=self.current_case,
                started_at=self.started_at,
                aborted=self.aborted,
                abort_reason=self.abort_reason,
            )

    def _send_snapshot(self, finished: bool) -> None:
        try:
            self.client.send_or_update_progress(self._snapshot(), finished=finished)
        except Exception as exc:
            print(f"Slack progress update failed: {exc}")


def main() -> int:
    """執行已設定的測試流程，並在啟用時同步 TestRail 結果。"""

    parser = argparse.ArgumentParser(description="依 .env 設定批次執行 TestRail 測試案例")
    parser.add_argument("--dry-run", action="store_true", help="只執行 testcase，不回報 TestRail")
    parser.add_argument("--project-id", type=int, help="覆寫 TESTRAIL_PROJECT_ID")
    parser.add_argument("--suite-id", type=int, help="覆寫 TESTRAIL_SUITE_ID")
    parser.add_argument("--name", help="指定建立的 Run 名稱")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    mode = get_env_int("TESTRAIL_RUN_MODE", required=True)
    if mode == RUN_MODE_MULTI:
        return run_multi_tests(args.dry_run)
    if mode == RUN_MODE_DAILY:
        return run_daily_tests(args)
    raise RuntimeError("TESTRAIL_RUN_MODE must be 2 or 3")


def resolve_run_strategy(mode: int) -> str:
    return DEFAULT_DAILY_RUN_STRATEGY if mode == RUN_MODE_DAILY else DEFAULT_MULTI_RUN_STRATEGY


def is_testrail_reporting_enabled(dry_run: bool = False) -> bool:
    return (not dry_run) and get_env_bool("TESTRAIL_REPORT_ENABLED", default=True)


def should_use_slack_progress_updates() -> bool:
    return get_env_bool("SLACK_NOTIFY_ENABLED", default=False)


def build_local_run_name(mode: int, args: argparse.Namespace | None = None) -> str:
    name_override = args.name if args is not None else None
    default_prefix = DEFAULT_DAILY_RUN_NAME_PREFIX if mode == RUN_MODE_DAILY else DEFAULT_MULTI_RUN_NAME_PREFIX
    timestamp = datetime.now(resolve_timezone()).strftime("%Y-%m-%d %H:%M")
    return name_override or f"{default_prefix} - {timestamp}"


def resolve_timezone() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)


def parse_env_file_list(env_name: str) -> list[str | int]:
    """內部輔助函式。"""

    raw_value = os.getenv(env_name, "").strip()
    if not raw_value:
        return []
    if raw_value.startswith("["):
        parsed = json.loads(raw_value)
        if not isinstance(parsed, list) or not all(isinstance(item, (str, int)) for item in parsed):
            raise RuntimeError(f"{env_name} must be a JSON array of strings or integers")
        normalized_items: list[str | int] = []
        for item in parsed:
            if isinstance(item, int):
                normalized_items.append(item)
            elif item.strip():
                normalized_items.append(item.strip())
        return normalized_items
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def normalize_test_case_name(test_case_name: str) -> str:
    """內部輔助函式。"""

    normalized_name = test_case_name.strip()
    if not normalized_name:
        raise RuntimeError("Test case name cannot be empty")
    if normalized_name.endswith(".py"):
        normalized_name = normalized_name[:-3]
    if "/" in normalized_name or "\\" in normalized_name:
        raise RuntimeError(f"Test case names must not contain paths: {test_case_name}")
    return f"{normalized_name}.py"


def load_test_cases_from_env(env_name: str) -> list[TestCaseModule]:
    """內部輔助函式。"""

    test_cases: list[TestCaseModule] = []
    seen_paths: set[Path] = set()
    selectors = parse_env_file_list(env_name)
    needs_catalog = any(
        isinstance(selector, int) or (isinstance(selector, str) and selector.strip().isdigit())
        for selector in selectors
    )
    all_test_cases = load_all_test_case_references() if needs_catalog else []

    for selector in selectors:
        for test_case in resolve_test_case_selector(selector, env_name, all_test_cases):
            if test_case.file_path in seen_paths:
                continue
            seen_paths.add(test_case.file_path)
            test_cases.append(test_case)
    return test_cases


def load_all_test_case_references() -> list[TestCaseReference]:
    """內部輔助函式。"""

    test_cases: list[TestCaseReference] = []
    for file_path in sorted(TEST_DIR.glob("*.py")):
        if file_path.name == "__init__.py":
            continue
        test_cases.append(load_test_case_reference(file_path))
    return test_cases


def resolve_test_case_selector(
    selector: str | int,
    env_name: str,
    all_test_cases: list[TestCaseReference],
) -> list[TestCaseModule]:
    """內部輔助函式。"""

    if isinstance(selector, int):
        return resolve_test_case_numeric_selector(selector, env_name, all_test_cases)

    selector_text = selector.strip()
    if selector_text.isdigit():
        return resolve_test_case_numeric_selector(int(selector_text), env_name, all_test_cases)

    file_path = TEST_DIR / normalize_test_case_name(selector_text)
    if not file_path.exists():
        raise FileNotFoundError(f"{env_name} contains missing test file: {selector_text}")

    return [load_test_case_module(file_path)]


def resolve_test_case_numeric_selector(
    selector: int,
    env_name: str,
    all_test_cases: list[TestCaseReference],
) -> list[TestCaseModule]:
    """內部輔助函式。"""

    matched_case_ids = [test_case for test_case in all_test_cases if test_case.case_id == selector]
    if matched_case_ids:
        return [load_test_case_module(test_case.file_path) for test_case in matched_case_ids]

    matched_run_ids = [test_case for test_case in all_test_cases if test_case.test_run_id == selector]
    if matched_run_ids:
        return [load_test_case_module(test_case.file_path) for test_case in matched_run_ids]

    raise FileNotFoundError(f"{env_name} contains unknown CASE_ID/TEST_RUN_ID: {selector}")


def load_test_case_reference(file_path: Path) -> TestCaseReference:
    """內部輔助函式。"""

    case_id, test_run_id = scan_test_case_identifiers(file_path)
    return TestCaseReference(
        file_path=file_path,
        case_id=case_id,
        test_run_id=test_run_id,
    )


def scan_test_case_identifiers(file_path: Path) -> tuple[int | None, int | None]:
    """不匯入模組，直接從原始碼讀取 CASE_ID/TEST_RUN_ID。"""

    source = file_path.read_text(encoding="utf-8-sig")
    module = ast.parse(source, filename=str(file_path))
    case_id = resolve_static_int_attr_from_ast(module, ("CASE_ID", "TESTRAIL_CASE_ID"))
    test_run_id = resolve_static_int_attr_from_ast(module, ("TEST_RUN_ID",))
    return case_id, test_run_id


def resolve_static_int_attr_from_ast(module: ast.Module, names: tuple[str, ...]) -> int | None:
    """從模組 AST 解析簡單的整數常數指定。"""

    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        value = resolve_ast_int(node.value)
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                return value
    return None


def resolve_ast_int(node: ast.AST) -> int | None:
    """從 AST 解析靜態整數值。"""

    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip().isdigit():
        return int(node.value.strip())
    return None


def load_test_case_module(file_path: Path) -> TestCaseModule:
    """內部輔助函式。"""

    module = load_module(file_path)
    case_id = resolve_case_id(module, file_path)
    test_run_id = resolve_optional_int_attr(module, "TEST_RUN_ID")
    # 相容兩種案例樣式：舊版定義 main()，新版（case_runner 樣板）定義 steps()。
    entry_func = getattr(module, "main", None) or getattr(module, "steps", None)
    if not isinstance(case_id, int):
        raise RuntimeError(f"{file_path.name} must define CASE_ID or TESTRAIL_CASE_ID")
    if not callable(entry_func):
        raise RuntimeError(f"{file_path.name} must define main() or steps()")
    setattr(module, "CASE_ID", case_id)
    setattr(module, "TESTRAIL_CASE_ID", case_id)
    return TestCaseModule(
        file_path=file_path,
        module=module,
        case_id=case_id,
        test_run_id=test_run_id,
    )


def load_module(file_path: Path) -> ModuleType:
    """內部輔助函式。"""

    spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load test module: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def resolve_case_id(module: ModuleType, file_path: Path) -> int | None:
    """內部輔助函式。"""

    for attr_name in ("CASE_ID", "TESTRAIL_CASE_ID"):
        value = getattr(module, attr_name, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())

    for text in (file_path.stem, file_path.name):
        case_id = extract_case_id_from_text(text)
        if case_id is not None:
            return case_id
    return None


def resolve_optional_int_attr(module: ModuleType, attr_name: str) -> int | None:
    """內部輔助函式。"""

    value = getattr(module, attr_name, None)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def extract_case_id_from_text(value: object) -> int | None:
    """內部輔助函式。"""

    if not isinstance(value, str) or not value.strip():
        return None
    match = CASE_ID_PATTERN.search(value)
    if match:
        return int(match.group(1))
    return None



def run_multi_tests(dry_run: bool) -> int:
    test_cases = load_test_cases_from_env("TESTRAIL_MULTI_TESTS")
    if not test_cases:
        print("TESTRAIL_MULTI_TESTS has no executable test cases.")
        return 1

    reporting_enabled = is_testrail_reporting_enabled(dry_run)
    client = TestRailClient.from_env(PROJECT_ROOT / ".env") if reporting_enabled else None
    if client is not None:
        run_info = prepare_run_info(
            client=client,
            test_cases=test_cases,
            dry_run=dry_run,
            mode=RUN_MODE_MULTI,
        )
    else:
        run_info = RunInfo(run_id=0, run_name=build_local_run_name(RUN_MODE_MULTI), run_url="")

    progress_notifier = None
    if should_use_slack_progress_updates():
        progress_notifier = SlackProgressNotifier(
            run_name=run_info.run_name,
            run_url=run_info.run_url,
            total=len(test_cases),
            interval_seconds=get_slack_progress_interval_seconds(),
        )

    case_summaries: list[CaseExecutionSummary] = []
    previous_deferred = os.environ.get("SLACK_NOTIFY_DEFERRED")
    os.environ["SLACK_NOTIFY_DEFERRED"] = "1"
    run_error: BaseException | None = None
    try:
        if progress_notifier is not None:
            progress_notifier.start()
        for test_case in test_cases:
            if progress_notifier is not None:
                progress_notifier.set_current_case(test_case.file_path.name)
            print(f"Running multi test case: {test_case.file_path.name} (C{test_case.case_id})")
            summary = run_case_main(
                test_case,
                dry_run=dry_run,
                run_id=run_info.run_id if run_info.run_id else None,
            )
            case_summaries.append(summary)
            if progress_notifier is not None:
                progress_notifier.record_case_summary(summary)
    except BaseException as exc:
        # 記錄中斷原因後照常往上拋，讓 job 仍以非零 exit code 結束。
        run_error = exc
        raise
    finally:
        restore_env_var("SLACK_NOTIFY_DEFERRED", previous_deferred)
        if progress_notifier is not None:
            if client is not None:
                # 中斷時 TestRail 很可能正是出問題的一方，查 run link 失敗
                # 不該連帶讓通知送不出去。
                try:
                    run_links = resolve_run_links(client, case_summaries, preferred_run=run_info)
                    if len(run_links) == 1 and run_info.run_id:
                        progress_notifier.set_run_display(run_info.run_name, run_info.run_url)
                    else:
                        progress_notifier.set_run_display(render_run_links_for_progress(run_links, run_info))
                except Exception as exc:
                    print(f"解析 TestRail run link 失敗，改用預設名稱：{exc}")
            progress_notifier.finish(
                aborted=run_error is not None,
                abort_reason=format_abort_reason(run_error) if run_error is not None else "",
            )

    print(f"Multi test run completed: {len(test_cases)} case(s)")
    if client is not None and not should_use_slack_progress_updates():
        send_slack_summary_if_needed(client, case_summaries, preferred_run=run_info)
    return 0


def run_daily_tests(args: argparse.Namespace) -> int:
    test_cases = load_test_cases_from_env("TESTRAIL_DAILY_TESTS")
    if not test_cases:
        print("TESTRAIL_DAILY_TESTS has no executable test cases.")
        return 1

    reporting_enabled = is_testrail_reporting_enabled(args.dry_run)
    client = TestRailClient.from_env(PROJECT_ROOT / ".env") if reporting_enabled else None
    if client is not None:
        run_info = prepare_run_info(
            client=client,
            test_cases=test_cases,
            dry_run=args.dry_run,
            mode=RUN_MODE_DAILY,
            args=args,
        )
    else:
        run_info = RunInfo(run_id=0, run_name=build_local_run_name(RUN_MODE_DAILY, args=args), run_url="")

    progress_notifier = None
    if should_use_slack_progress_updates():
        progress_notifier = SlackProgressNotifier(
            run_name=run_info.run_name,
            run_url=run_info.run_url,
            total=len(test_cases),
            interval_seconds=get_slack_progress_interval_seconds(),
        )

    case_summaries: list[CaseExecutionSummary] = []
    previous_deferred = os.environ.get("SLACK_NOTIFY_DEFERRED")
    os.environ["SLACK_NOTIFY_DEFERRED"] = "1"
    run_error: BaseException | None = None
    try:
        if progress_notifier is not None:
            progress_notifier.start()
        for test_case in test_cases:
            if progress_notifier is not None:
                progress_notifier.set_current_case(test_case.file_path.name)
            print(f"Running daily test case: {test_case.file_path.name} (C{test_case.case_id})")
            summary = run_case_main(test_case, dry_run=args.dry_run, run_id=run_info.run_id)
            case_summaries.append(summary)
            if progress_notifier is not None:
                progress_notifier.record_case_summary(summary)
    except BaseException as exc:
        # 記錄中斷原因後照常往上拋，讓 job 仍以非零 exit code 結束。
        run_error = exc
        raise
    finally:
        restore_env_var("SLACK_NOTIFY_DEFERRED", previous_deferred)
        if progress_notifier is not None:
            if client is not None:
                # 中斷時 TestRail 很可能正是出問題的一方，查 run link 失敗
                # 不該連帶讓通知送不出去。
                try:
                    run_links = resolve_run_links(client, case_summaries, preferred_run=run_info)
                    if len(run_links) == 1 and run_info.run_id:
                        progress_notifier.set_run_display(run_info.run_name, run_info.run_url)
                    else:
                        progress_notifier.set_run_display(render_run_links_for_progress(run_links, run_info))
                except Exception as exc:
                    print(f"解析 TestRail run link 失敗，改用預設名稱：{exc}")
            progress_notifier.finish(
                aborted=run_error is not None,
                abort_reason=format_abort_reason(run_error) if run_error is not None else "",
            )

    print(f"Daily test run completed: {len(test_cases)} case(s)")
    if client is not None and not should_use_slack_progress_updates():
        send_slack_summary_if_needed(client, case_summaries, preferred_run=run_info)
    return 0


def format_abort_reason(exc: BaseException, max_length: int = 300) -> str:
    """把中斷用的例外整理成適合放進 Slack 的一行說明。"""

    detail = " ".join(str(exc).split())
    reason = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    if len(reason) <= max_length:
        return reason
    return reason[: max_length - 3] + "..."


def prepare_run_info(
    client: TestRailClient,
    test_cases: list[TestCaseModule],
    dry_run: bool,
    mode: int,
    args: argparse.Namespace | None = None,
) -> RunInfo:
    """內部輔助函式。"""

    strategy = resolve_run_strategy(mode)
    if strategy == "existing":
        existing_run_id = DEFAULT_EXISTING_RUN_ID
        run_name = (
            f"Existing Run {existing_run_id}"
            if existing_run_id
            else "Per-case TEST_RUN_ID (執行中，完成後解析實際 Run)"
        )
        if dry_run:
            print(f"Dry run: using existing run {run_name}")
            return RunInfo(run_id=0, run_name=run_name, run_url="")
        return RunInfo(
            run_id=existing_run_id,
            run_name=run_name,
            run_url=build_run_url(client, existing_run_id) if existing_run_id else "",
        )

    run_name = build_local_run_name(mode, args=args)
    if dry_run:
        print(f"Dry run: would create run {run_name}")
        return RunInfo(run_id=0, run_name=run_name, run_url="")

    project_id = (
        args.project_id
        if args is not None and args.project_id is not None
        else get_env_int("TESTRAIL_PROJECT_ID", required=True)
    )
    suite_id = args.suite_id if args is not None and args.suite_id is not None else get_env_int("TESTRAIL_SUITE_ID")
    created_run = client.add_run(
        project_id=project_id,
        name=run_name,
        case_ids=[test_case.case_id for test_case in test_cases],
        suite_id=suite_id,
        description="Created by TestRail automation.",
        include_all=get_env_bool("TESTRAIL_INCLUDE_ALL", default=False),
        milestone_id=get_env_int("TESTRAIL_MILESTONE_ID"),
    )
    run_id = int(created_run["id"])
    print(f"Created TestRail run via strategy '{strategy}': ID={run_id}, name={run_name}")
    return RunInfo(run_id=run_id, run_name=run_name, run_url=build_run_url(client, run_id))


def run_case_main(
    test_case: TestCaseModule,
    dry_run: bool,
    run_id: int | None = None,
) -> CaseExecutionSummary:
    """內部輔助函式。"""

    main_func = getattr(test_case.module, "main", None)
    steps_func = getattr(test_case.module, "steps", None)
    if not callable(main_func) and not callable(steps_func):
        raise RuntimeError(f"{test_case.file_path.name} must define main() or steps()")

    old_argv = sys.argv[:]
    try:
        sys.argv = [str(test_case.file_path)]
        if run_id:
            sys.argv.extend(["--run-id", str(run_id)])
        if dry_run:
            sys.argv.append("--dry-run")

        if callable(main_func):
            # 舊版案例：直接呼叫 main()，run_id/dry_run 由 sys.argv 傳遞。
            exit_code = main_func()
        else:
            # 新版案例：用 case_runner.run_case 包 steps()。run_id 仍靠 sys.argv 的
            # --run-id 傳進 run_step_case；dry_run 顯式帶入以與舊版行為一致。
            from support.case_runner import run_case

            exit_code = run_case(
                test_case.case_id,
                test_case.test_run_id or 0,
                steps_func,
                dry_run=dry_run,
            )
        if exit_code not in (0, None):
            raise RuntimeError(f"{test_case.file_path.name} returned exit code {exit_code}")

        execution_summary = get_last_case_execution_summary()
        if execution_summary is None:
            raise RuntimeError(f"{test_case.file_path.name} finished without producing a TestRail execution summary")
        return execution_summary
    finally:
        sys.argv = old_argv


def send_slack_summary_if_needed(
    client: TestRailClient,
    case_summaries: list[CaseExecutionSummary],
    preferred_run: RunInfo | None = None,
) -> None:
    """內部輔助函式。"""

    if not case_summaries:
        return
    if any(not summary.reported for summary in case_summaries):
        print("Skipping Slack summary because some testcases were not reported to TestRail.")
        return
    if not get_env_bool("SLACK_NOTIFY_ENABLED", default=False):
        return

    webhook_url = get_slack_webhook_url()
    run_links = resolve_run_links(client, case_summaries, preferred_run)
    status_mapping = get_status_mapping()
    summary = SlackTestSummary(
        passed=sum(1 for item in case_summaries if item.status_id == status_mapping.passed),
        failed=sum(1 for item in case_summaries if item.status_id == status_mapping.failed),
        skipped=sum(1 for item in case_summaries if item.status_id == status_mapping.blocked),
    )
    send_test_run_finished(webhook_url, run_links, summary)
    print("Sent Slack summary notification.")


def resolve_run_links(
    client: TestRailClient,
    case_summaries: list[CaseExecutionSummary],
    preferred_run: RunInfo | None = None,
) -> list[SlackRunLink]:
    """內部輔助函式。"""

    run_links: list[SlackRunLink] = []
    seen_run_ids: set[int] = set()
    if preferred_run is not None and preferred_run.run_id:
        seen_run_ids.add(preferred_run.run_id)
        run_links.append(SlackRunLink(name=preferred_run.run_name, url=preferred_run.run_url))

    for summary in case_summaries:
        if not summary.run_id or summary.run_id in seen_run_ids:
            continue
        run_data = client.get_run(summary.run_id)
        seen_run_ids.add(summary.run_id)
        run_links.append(
            SlackRunLink(
                name=str(run_data.get("name") or f"Run {summary.run_id}"),
                url=build_run_url(client, summary.run_id),
            )
        )
    return run_links


def build_run_url(client: TestRailClient, run_id: int) -> str:
    """內部輔助函式。"""

    return f"{client.config.base_url.rstrip('/')}/index.php?/runs/view/{run_id}"


def render_run_links_for_progress(run_links: list[SlackRunLink], fallback_run: RunInfo | None = None) -> str:
    """內部輔助函式。"""

    if run_links:
        return "\n".join(f"<{link.url}|{link.name}>" for link in run_links if link.url and link.name)
    if fallback_run is not None and fallback_run.run_name:
        return fallback_run.run_name
    return "Local execution"


def restore_env_var(name: str, value: str | None) -> None:
    """內部輔助函式。"""

    if value is None:
        os.environ.pop(name, None)
        return
    os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
