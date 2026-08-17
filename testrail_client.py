import argparse
import base64
import html
import json
import mimetypes
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable
from urllib import error, request

from slack import (
    SlackRunLink,
    SlackTestSummary,
    get_slack_webhook_url,
    send_test_run_finished,
)

# 專案內部約定的 TestRail 狀態碼。
PASSED = 1
BLOCKED = 2
FAILED = 5

PROJECT_ROOT = Path(__file__).resolve().parent

# TestRail 顯示結果圖片時的預設寬度。
RESULT_IMAGE_WIDTH = 600

# 一般 JSON API 的逾時秒數；附件上傳要傳檔案，給比較寬鬆的時間。
TESTRAIL_REQUEST_TIMEOUT = 30
TESTRAIL_UPLOAD_TIMEOUT = 120

# TestRail 偶爾會出現讀取逾時或 5xx，重試幾次再放棄。
TESTRAIL_MAX_ATTEMPTS = 3
TESTRAIL_RETRY_BACKOFF = 3
TESTRAIL_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# 預設是否印出較詳細的 console log。
DEFAULT_VERBOSE = True

# 若 step 沒有被執行，會以 blocked actual 補上這段說明。
DEFAULT_BLOCKED_ACTUAL = "AUTOTEST: blocked - 前一步未完成，後續步驟未執行"

# 若 testcase 主動中止剩餘步驟，則使用這段 blocked 說明。
ABORTED_BLOCKED_ACTUAL = "AUTOTEST: blocked - 前一步失敗或中止，後續步驟未執行"

# StepReport 第三個欄位統一只接受 list；空陣列代表不附圖。
AttachmentInput = list[str | Path]


@dataclass(frozen=True)
class TestRailConfig:
    """保存 TestRail API 連線設定。"""

    base_url: str
    user: str
    api_key: str

    @property
    def api_base_url(self) -> str:
        """回傳 TestRail v2 API 的 base url。"""

        return self.base_url.rstrip("/") + "/index.php?/api/v2/"


@dataclass(frozen=True)
class TestRailStep:
    """表示一個 TestRail case step。"""

    content: str
    expected: str = ""


@dataclass(frozen=True)
class TestRailResult:
    """表示最終要送回 TestRail 的 result payload。"""

    status_id: int
    comment: str
    elapsed: str
    step_results: list[dict[str, object]] | None = None


@dataclass(frozen=True)
class StatusMapping:
    passed: int
    blocked: int
    failed: int


@dataclass(frozen=True)
class CaseExecutionSummary:
    """表示單一 testcase 執行後的摘要，供 runner / Slack 使用。"""

    case_id: int
    run_id: int
    case_title: str
    status_id: int
    result_id: int | None
    reported: bool
    dry_run: bool


@dataclass(frozen=True)
class StepReport:
    """表示自動化程式對單一步驟的執行結果。

    attachments 一律使用 list：
    - `[]` 表示不附圖
    - `[path]` 表示附一張圖
    - `[path1, path2]` 表示附多張圖
    """

    status_id: int
    actual: str
    attachments: AttachmentInput = field(default_factory=list)


StepReportCollection = list[StepReport] | dict[int, StepReport]
StepReportBuilder = Callable[[], StepReportCollection]


class TestRailError(RuntimeError):
    """封裝 TestRail API 呼叫失敗時的錯誤。"""


class AbortRemainingSteps(RuntimeError):
    """讓 testcase 能主動中止後續 steps，並交由 client 自動補 blocked。"""

    def __init__(
        self,
        step_reports: StepReportCollection,
        blocked_actual: str = ABORTED_BLOCKED_ACTUAL,
    ):
        super().__init__(blocked_actual)
        self.step_reports = step_reports
        self.blocked_actual = blocked_actual


def abort_remaining_steps(
    step_reports: StepReportCollection,
    blocked_actual: str = ABORTED_BLOCKED_ACTUAL,
) -> None:
    """中止剩餘步驟，讓未執行 steps 自動以 BLOCKED 回報。"""

    raise AbortRemainingSteps(step_reports=step_reports, blocked_actual=blocked_actual)


class TestRailClient:
    """專案專用的輕量 TestRail API client。"""

    def __init__(self, config: TestRailConfig):
        self.config = config

    @classmethod
    def from_env(cls, path: str | os.PathLike[str] = ".env") -> "TestRailClient":
        """從 `.env` 讀取設定並建立 client。"""

        load_dotenv(path)
        return cls(load_config_from_env())

    def get_case(self, case_id: int) -> dict[str, Any]:
        """讀取單一 TestRail case。"""

        response = self._request("GET", f"get_case/{case_id}")
        if not isinstance(response, dict):
            return {}
        return response

    def get_case_steps(self, case_id: int) -> list[TestRailStep]:
        """讀取 case 並轉成本專案可用的 step 結構。"""

        return extract_case_steps(self.get_case(case_id))

    def get_run(self, run_id: int) -> dict[str, Any]:
        """讀取單一 TestRail run。"""

        response = self._request("GET", f"get_run/{run_id}")
        if not isinstance(response, dict):
            return {}
        return response

    def add_run(
        self,
        project_id: int,
        name: str,
        case_ids: list[int],
        suite_id: int | None = None,
        description: str = "",
        include_all: bool = False,
        milestone_id: int | None = None,
    ) -> dict[str, Any]:
        """建立新的 TestRail run。"""

        payload: dict[str, Any] = {
            "name": name,
            "include_all": include_all,
        }
        if not include_all:
            payload["case_ids"] = case_ids
        if suite_id is not None:
            payload["suite_id"] = suite_id
        if milestone_id is not None:
            payload["milestone_id"] = milestone_id
        if description:
            payload["description"] = description

        response = self._request("POST", f"add_run/{project_id}", payload)
        if not isinstance(response, dict):
            return {}
        return response

    def add_result_for_case(
        self,
        run_id: int,
        case_id: int,
        result: TestRailResult,
    ) -> dict[str, Any]:
        """把單一 testcase 的結果寫回指定 run / case。"""

        payload: dict[str, Any] = {
            "status_id": result.status_id,
            "comment": result.comment,
            "elapsed": result.elapsed,
        }
        if result.step_results is not None:
            payload["custom_step_results"] = result.step_results
        response = self._request(
            "POST", f"add_result_for_case/{run_id}/{case_id}", payload
        )
        if not isinstance(response, dict):
            return {}
        return response

    def add_attachment_to_run(
        self, run_id: int, attachment_path: Path
    ) -> dict[str, Any]:
        """把圖片附件上傳到指定 run。"""

        return self._request_multipart(
            endpoint=f"add_attachment_to_run/{run_id}",
            field_name="attachment",
            file_path=attachment_path,
        )

    def add_attachment_to_result(
        self, result_id: int, attachment_path: Path
    ) -> dict[str, Any]:
        """將附件上傳到指定的 TestRail result。"""

        return self._request_multipart(
            endpoint=f"add_attachment_to_result/{result_id}",
            field_name="attachment",
            file_path=attachment_path,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """發送 JSON 格式的 TestRail API 請求。"""

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            self.config.api_base_url + endpoint,
            data=data,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "application/json",
            },
            method=method,
        )
        return self._open_json(req)

    def _request_multipart(
        self,
        endpoint: str,
        field_name: str,
        file_path: Path,
    ) -> dict[str, Any]:
        """發送 multipart/form-data 請求，用於附件上傳。"""

        if not file_path.exists():
            raise FileNotFoundError(f"Attachment not found: {file_path}")
        if not file_path.is_file():
            raise FileNotFoundError(f"Attachment path is not a file: {file_path}")

        boundary = "----TestRailBoundary" + uuid.uuid4().hex
        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        file_bytes = file_path.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )

        req = request.Request(
            self.config.api_base_url + endpoint,
            data=body,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        response = self._open_json(req, timeout=TESTRAIL_UPLOAD_TIMEOUT)
        if not isinstance(response, dict):
            return {}
        return response

    def _open_json(
        self,
        req: request.Request,
        timeout: int = TESTRAIL_REQUEST_TIMEOUT,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """開啟 HTTP request 並把回應內容解析成 JSON。

        TestRail 在 CI 上偶爾會讀取逾時或回 5xx，這裡對這類暫時性錯誤做重試，
        避免單一次網路抖動就讓整個 testcase 中斷。
        """

        for attempt in range(1, TESTRAIL_MAX_ATTEMPTS + 1):
            try:
                with request.urlopen(req, timeout=timeout) as response:
                    body = response.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = f"TestRail HTTP {exc.code}: {detail}"
                if exc.code not in TESTRAIL_RETRY_STATUS_CODES:
                    raise TestRailError(message) from exc
                self._wait_before_retry(attempt, message, exc)
            except TimeoutError as exc:
                self._wait_before_retry(attempt, "TestRail request timed out", exc)
            except error.URLError as exc:
                self._wait_before_retry(
                    attempt, f"Cannot connect to TestRail: {exc}", exc
                )

        if not body:
            return {}
        return json.loads(body)

    @staticmethod
    def _wait_before_retry(attempt: int, message: str, exc: Exception) -> None:
        """重試前印出警告並等待；已用完次數則轉成 TestRailError。"""

        if attempt >= TESTRAIL_MAX_ATTEMPTS:
            raise TestRailError(
                f"{message} (已重試 {TESTRAIL_MAX_ATTEMPTS} 次仍失敗)"
            ) from exc
        wait_seconds = TESTRAIL_RETRY_BACKOFF * attempt
        print(
            f"[TestRail] {message}；{wait_seconds} 秒後重試 "
            f"({attempt}/{TESTRAIL_MAX_ATTEMPTS - 1})"
        )
        sleep(wait_seconds)

    def _auth_header(self) -> str:
        """產生 TestRail Basic Auth header。"""

        token = f"{self.config.user}:{self.config.api_key}".encode("utf-8")
        return "Basic " + base64.b64encode(token).decode("ascii")


def run_step_case(
    case_id: int,
    test_run_id: int,
    build_step_reports: StepReportBuilder,
    report_to_testrail: bool | None = None,
    run_id_override: int | None = None,
    dry_run: bool | None = None,
) -> int:
    """執行 testcase，必要時上傳附件、回報 TestRail，並觸發 Slack。

    這是測試案例最主要的共用入口，負責整合：
    - 讀取 TestRail case / steps
    - 收集 testcase 產生的 StepReport
    - 上傳截圖
    - 生成 custom step results
    - 寫回 TestRail
    - 需要時發送 Slack
    """

    global LAST_CASE_EXECUTION_SUMMARY

    # 若 testcase 沒明確傳入開關，則統一吃 `.env` 的全域設定。
    report_enabled = (
        get_env_bool("TESTRAIL_REPORT_ENABLED", default=True)
        if report_to_testrail is None
        else bool(report_to_testrail)
    )
    blocked_actual = DEFAULT_BLOCKED_ACTUAL
    status_mapping = get_status_mapping()
    LAST_CASE_EXECUTION_SUMMARY = None

    # 支援從 CLI 覆寫 run id / dry-run。即使呼叫端已明確傳入 dry_run，
    # 仍要解析 --run-id，讓 daily/multi 批次執行器建立的新 run 能覆寫
    # testcase 內寫死的 TEST_RUN_ID（否則結果會回報到舊 run）。
    parser = argparse.ArgumentParser(
        description=f"Run C{case_id} and report custom step results to TestRail."
    )
    parser.add_argument(
        "--run-id", type=int, help="Override TEST_RUN_ID in the test case"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not report to TestRail"
    )
    args, _ = parser.parse_known_args()

    run_id = run_id_override or args.run_id or test_run_id
    if dry_run is None:
        dry_run = args.dry_run or not report_enabled
    else:
        dry_run = dry_run or not report_enabled

    if not dry_run and not run_id:
        raise RuntimeError(
            "Fill TEST_RUN_ID in the test case or pass a run id before reporting"
        )

    client: TestRailClient | None = None
    verbose = get_env_bool("TESTRAIL_VERBOSE", default=DEFAULT_VERBOSE)

    started = monotonic()
    try:
        built_step_reports = build_step_reports()
    except AbortRemainingSteps as exc:
        built_step_reports = exc.step_reports
        blocked_actual = exc.blocked_actual

    step_reports = normalize_step_reports(built_step_reports)
    steps = build_local_case_steps(step_reports)
    case_title = f"C{case_id}"
    is_step_case = True

    if not dry_run:
        client = TestRailClient.from_env(PROJECT_ROOT / ".env")
        testrail_case = client.get_case(case_id)
        steps = client.get_case_steps(case_id)
        case_title = str(testrail_case.get("title", "") or f"C{case_id}")
        is_step_case = case_uses_separated_steps(testrail_case)

    if verbose:
        print(f"Case C{case_id}: {case_title}")
        print(f"Resolved steps: {len(steps)}")

    result_attachments = collect_non_image_attachments(step_reports)
    if not dry_run and client is not None:
        step_reports = upload_step_images(client, run_id, step_reports, verbose=verbose)

    result = build_testrail_result(
        case_id=case_id,
        steps=steps,
        step_reports=step_reports,
        started=started,
        status_mapping=status_mapping,
        blocked_actual=blocked_actual,
        include_step_results=is_step_case,
    )
    if verbose and result.step_results is not None:
        print_step_results(result.step_results)

    if dry_run:
        print("Dry run enabled. Skipping TestRail result upload.")
        cleanup_temp_video_attachments(result_attachments, verbose=verbose)
        LAST_CASE_EXECUTION_SUMMARY = CaseExecutionSummary(
            case_id=case_id,
            run_id=run_id,
            case_title=case_title,
            status_id=result.status_id,
            result_id=None,
            reported=False,
            dry_run=True,
        )
        return 0

    created_result = client.add_result_for_case(run_id, case_id, result)
    try:
        upload_result_attachments(
            client=client,
            result_id=int(created_result["id"]),
            attachment_paths=result_attachments,
            verbose=verbose,
        )
    finally:
        cleanup_temp_video_attachments(result_attachments, verbose=verbose)
    print(f"已回報結果到 TestRail，result_id={created_result['id']}")
    LAST_CASE_EXECUTION_SUMMARY = CaseExecutionSummary(
        case_id=case_id,
        run_id=run_id,
        case_title=case_title,
        status_id=result.status_id,
        result_id=int(created_result["id"]),
        reported=True,
        dry_run=False,
    )

    # 若不是由 runner 延後處理 Slack，就在 testcase 完成時立即送出。
    send_slack_notification_for_case_if_needed(LAST_CASE_EXECUTION_SUMMARY)
    return 0


# 提供 runner 讀取最近一次 testcase 執行結果，避免重複解析 console 輸出。
LAST_CASE_EXECUTION_SUMMARY: CaseExecutionSummary | None = None


def get_last_case_execution_summary() -> CaseExecutionSummary | None:
    """回傳最近一次 `run_step_case()` 產生的摘要。"""

    return LAST_CASE_EXECUTION_SUMMARY


def send_slack_notification_for_case_if_needed(summary: CaseExecutionSummary) -> None:
    """若符合條件，為單一 testcase 送出 Slack 通知。"""

    if not summary.reported:
        return
    if os.getenv("SLACK_NOTIFY_DEFERRED", "").strip() == "1":
        return
    if not get_env_bool("SLACK_NOTIFY_ENABLED", default=False):
        return

    client = TestRailClient.from_env(PROJECT_ROOT / ".env")
    run_data = client.get_run(summary.run_id)
    status_mapping = get_status_mapping()
    run_link = SlackRunLink(
        name=str(run_data.get("name") or f"Run {summary.run_id}"),
        url=f"{client.config.base_url.rstrip('/')}/index.php?/runs/view/{summary.run_id}",
    )
    slack_summary = SlackTestSummary(
        passed=1 if summary.status_id == status_mapping.passed else 0,
        failed=1 if summary.status_id == status_mapping.failed else 0,
        skipped=1 if summary.status_id == status_mapping.blocked else 0,
    )
    send_test_run_finished(
        webhook_url=get_slack_webhook_url(),
        run_links=[run_link],
        summary=slack_summary,
    )
    print("已送出 Slack 通知。")


def normalize_step_reports(step_reports: StepReportCollection) -> dict[int, StepReport]:
    """將 list / dict 兩種輸入格式統一轉成 `step_no -> StepReport`。"""

    if isinstance(step_reports, dict):
        return step_reports
    return {index: report for index, report in enumerate(step_reports, start=1)}


def normalize_attachments(attachments: AttachmentInput) -> list[Path]:
    """驗證並轉換 attachments 成絕對路徑清單。"""

    if not isinstance(attachments, list):
        raise RuntimeError("StepReport.attachments must be a list[str | Path]")
    return [resolve_attachment_path(attachment) for attachment in attachments]


def resolve_attachment_path(attachment: str | Path) -> Path:
    """把相對路徑附件轉成以專案根目錄為準的實際路徑。"""

    path = Path(attachment)
    if path.is_absolute():
        return path

    candidate = PROJECT_ROOT / path
    if candidate.exists():
        return candidate

    # CI 內常見狀況是檔案實際落在 IMG/C<case>_<timestamp>/ 之類的子資料夾，
    # 但 step report 只帶了 IMG/<filename>；此時退回到 IMG 下遞迴找同檔名。
    img_dir = PROJECT_ROOT / "IMG"
    if img_dir.exists():
        matches = sorted(
            img_dir.rglob(path.name),
            key=lambda match: match.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]

    return candidate


def upload_step_images(
    client: TestRailClient,
    run_id: int,
    step_reports: dict[int, StepReport],
    verbose: bool = True,
) -> dict[int, StepReport]:
    """Upload image attachments to TestRail and inject HTML into step actual."""

    updated_reports = {}
    for step_index, report in step_reports.items():
        attachment_paths = get_image_attachments(report.attachments)
        if not attachment_paths:
            updated_reports[step_index] = report
            continue

        attachment_html_parts = []
        for attachment_index, attachment_path in enumerate(attachment_paths, start=1):
            # 圖片上傳失敗不該讓整個 testcase 中斷；沒有截圖的結果仍比完全沒回報好。
            try:
                response = client.add_attachment_to_run(run_id, attachment_path)
            except TestRailError as exc:
                print(
                    f"[TestRail] step {step_index} 圖片 {attachment_index} "
                    f"上傳失敗，略過：{attachment_path.name}（{exc}）"
                )
                attachment_html_parts.append(
                    f"AUTOTEST: 截圖上傳失敗（{html.escape(attachment_path.name)}）"
                )
                continue
            attachment_id = int(response["attachment_id"])
            attachment_url = (
                f"{client.config.base_url.rstrip('/')}/"
                f"index.php?/attachments/get/{attachment_id}"
            )
            attachment_html_parts.append(
                build_testrail_attachment_html(
                    attachment_path=attachment_path,
                    attachment_url=attachment_url,
                    attachment_id=attachment_id,
                )
            )
            if verbose:
                print(
                    f"Uploaded step {step_index} image {attachment_index}: "
                    f"id={attachment_id} url={attachment_url}"
                )

        updated_reports[step_index] = StepReport(
            status_id=report.status_id,
            actual=build_actual_with_attachments(report.actual, attachment_html_parts),
            attachments=attachment_paths,
        )

    return updated_reports


def get_image_attachments(attachments: AttachmentInput) -> list[Path]:
    """只保留圖片附件，供 step actual 顯示使用。"""

    return [
        path for path in normalize_attachments(attachments) if is_image_attachment(path)
    ]


def collect_non_image_attachments(step_reports: dict[int, StepReport]) -> list[Path]:
    """收集所有非圖片附件，供整筆 result 附件使用。"""

    attachment_paths: list[Path] = []
    for report in step_reports.values():
        for path in normalize_attachments(report.attachments):
            if not is_image_attachment(path):
                attachment_paths.append(path)
    return attachment_paths


def upload_result_attachments(
    client: TestRailClient,
    result_id: int,
    attachment_paths: list[Path],
    verbose: bool = True,
) -> None:
    """將非圖片附件掛到最終 result，不插入 step actual。"""

    for index, attachment_path in enumerate(attachment_paths, start=1):
        # 與 step 圖片一致：附件上傳失敗只警告，結果本身已經寫回 TestRail。
        try:
            response = client.add_attachment_to_result(result_id, attachment_path)
        except TestRailError as exc:
            print(f"[TestRail] 結果附件 {index} 上傳失敗，略過：{attachment_path}（{exc}）")
            continue
        if verbose:
            print(
                f"已上傳結果附件 {index}："
                f"id={response.get('attachment_id', '')} path={attachment_path}"
            )


def cleanup_temp_video_attachments(
    attachment_paths: list[Path], verbose: bool = True
) -> None:
    temp_video_root = Path(tempfile.gettempdir()) / "qa-test-automation-videos"
    for attachment_path in attachment_paths:
        path = Path(attachment_path)
        try:
            resolved_path = path.resolve()
            resolved_root = temp_video_root.resolve()
        except OSError:
            continue
        if path.suffix.lower() != ".webm":
            continue
        if resolved_root not in resolved_path.parents:
            continue
        try:
            resolved_path.unlink(missing_ok=True)
            cleanup_empty_temp_video_dirs(resolved_path.parent, resolved_root)
            if verbose:
                print(f"Removed temporary video attachment: {resolved_path}")
        except OSError as exc:
            if verbose:
                print(
                    f"Failed to remove temporary video attachment {resolved_path}: {exc}"
                )


def cleanup_empty_temp_video_dirs(start_dir: Path, stop_dir: Path) -> None:
    current = start_dir
    while current != stop_dir and stop_dir in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def is_image_attachment(attachment_path: Path) -> bool:
    """判斷附件是否為圖片。"""

    mime_type = mimetypes.guess_type(str(attachment_path))[0] or ""
    return mime_type.startswith("image/")


def build_actual_with_attachments(actual: str, attachment_html_parts: list[str]) -> str:
    """Append attachment HTML fragments after actual."""

    escaped_actual = html.escape(actual)
    attachments_html = "".join(
        f"<p>{attachment_html}</p>" for attachment_html in attachment_html_parts
    )
    return f"<p>{escaped_actual}</p>{attachments_html}"


def build_testrail_attachment_html(
    attachment_path: Path, attachment_url: str, attachment_id: int
) -> str:
    """Build the proper TestRail HTML for the attachment type."""

    mime_type = mimetypes.guess_type(str(attachment_path))[0] or ""
    if mime_type.startswith("image/"):
        return build_testrail_image_html(
            image_url=attachment_url,
            attachment_id=attachment_id,
            width=RESULT_IMAGE_WIDTH,
        )
    return build_testrail_file_link_html(
        attachment_url=attachment_url,
        attachment_name=attachment_path.name,
    )


def build_testrail_image_html(image_url: str, attachment_id: int, width: int) -> str:
    """產生 TestRail rich text 可顯示的圖片 HTML。"""

    escaped_url = html.escape(image_url, quote=True)
    safe_width = max(1, int(width))
    return (
        f'<img src="{escaped_url}" '
        'class="fr-fic fr-dii markdown-img" '
        f'width="{safe_width}" '
        f'style="max-width: 100%; width: {safe_width}px; height: auto;" '
        f'data-attachment="{attachment_id}" '
        f'data-attachment-id="{attachment_id}" '
        f'data-original-src="{escaped_url}" '
        'onerror="App.Attachments.handleError(this);">'
    )


def build_testrail_file_link_html(attachment_url: str, attachment_name: str) -> str:
    """產生 TestRail rich text 可點擊的附件連結 HTML。"""

    escaped_url = html.escape(attachment_url, quote=True)
    escaped_name = html.escape(attachment_name)
    return f'<a href="{escaped_url}" target="_blank" rel="noopener noreferrer">{escaped_name}</a>'


def build_execution_environment_comment(case_id: int) -> str:
    """產生附在 TestRail 結果最前面的執行環境資訊區塊。

    需要顯示更多欄位（例如受測角色帳號、版本號）時，往 rows 加即可。
    """

    rows = [
        ("環境", resolve_runtime_env()),
        ("測試案例", f"C{case_id}"),
    ]
    items = "".join(
        f"<li><strong>{html.escape(label)}:</strong> {html.escape(value)}</li>"
        for label, value in rows
    )
    return (
        '<div style="border: 3px solid #2f55d4; '
        'background: #f7f8fc; padding: 12px 16px; margin: 4px 0 10px 0;">'
        '<div style="font-weight: 700; margin-bottom: 8px;">'
        "[INFO] 執行環境與參數資訊："
        "</div>"
        '<ul style="margin: 0; padding-left: 20px;">'
        f"{items}"
        "</ul>"
        "</div>"
    )


def resolve_runtime_env() -> str:
    """讀取目前測試環境名稱，供結果註解顯示。

    與 support.test_env.resolve_env() 同一個變數；這裡另外實作一份，
    是為了讓 testrail_client 不反向相依 support 套件。
    """

    return (get_env_str("TEST_ENV", required=False).strip() or "QA").upper()


def build_testrail_result(
    case_id: int,
    steps: list[TestRailStep],
    step_reports: dict[int, StepReport],
    started: float,
    status_mapping: StatusMapping,
    blocked_actual: str = DEFAULT_BLOCKED_ACTUAL,
    include_step_results: bool = True,
) -> TestRailResult:
    """把 testcase 的 step report 組裝成 TestRail 可接受的最終結果。"""

    step_results = []
    for index, step in enumerate(steps, start=1):
        # 若某 step 沒有被 testcase 明確回報，代表未執行，統一補成 blocked。
        report = step_reports.get(
            index, StepReport(status_mapping.blocked, blocked_actual)
        )
        step_results.append(
            {
                "content": step.content,
                "expected": step.expected,
                "actual": report.actual,
                "status_id": report.status_id,
            }
        )

    overall_status = (
        status_mapping.failed
        if any(
            report.status_id == status_mapping.failed
            for report in step_reports.values()
        )
        else (
            status_mapping.blocked
            if step_reports
            and all(
                report.status_id == status_mapping.blocked
                for report in step_reports.values()
            )
            else status_mapping.passed
        )
    )
    result_comment = build_execution_environment_comment(case_id)
    if not include_step_results:
        comment_lines = [result_comment]
        for index in sorted(step_reports.keys()):
            comment_lines.append(f"{index}. {step_reports[index].actual}")
        return TestRailResult(
            status_id=overall_status,
            comment="\n".join(comment_lines),
            elapsed=format_elapsed(monotonic() - started),
            step_results=None,
        )
    return TestRailResult(
        status_id=overall_status,
        comment=result_comment,
        elapsed=format_elapsed(monotonic() - started),
        step_results=step_results,
    )


def print_step_results(step_results: list[dict[str, object]]) -> None:
    """把 step 結果摘要輸出到 console。"""

    for index, step_result in enumerate(step_results, start=1):
        actual = summarize_actual_for_console(str(step_result["actual"]))
        print(f"  步驟 {index}：status={step_result['status_id']}，actual={actual}")


def summarize_actual_for_console(actual: str) -> str:
    """避免 console 印出冗長 HTML，改成簡短摘要。"""

    image_count = actual.count("<img ")
    if image_count:
        return f"[actual contains {image_count} TestRail image(s)]"
    return actual


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """讀取簡易 `.env` 檔案，僅在該 key 尚未存在於環境變數時才載入。"""

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key and (key not in os.environ or not str(os.environ.get(key, "")).strip()):
            os.environ[key] = value


def load_config_from_env() -> TestRailConfig:
    """從環境變數讀取 TestRail API 設定。

    三個值都沒有安全的預設，缺少時直接報錯，避免不小心把憑證寫死在程式碼裡。
    本機放 `.env`，CI 放 masked 的 CI/CD variables。
    """

    return TestRailConfig(
        base_url=get_env_str("TESTRAIL_URL", required=True),
        user=get_env_str("TESTRAIL_USER", required=True),
        api_key=get_env_str("TESTRAIL_API_KEY", required=True),
    )


def get_status_mapping() -> StatusMapping:
    return StatusMapping(
        passed=get_env_int("TESTRAIL_STATUS_PASSED") or PASSED,
        blocked=get_env_int("TESTRAIL_STATUS_BLOCKED") or BLOCKED,
        failed=get_env_int("TESTRAIL_STATUS_FAILED") or FAILED,
    )


def get_env_int(name: str, required: bool = False) -> int | None:
    """讀取整數型環境變數。"""

    load_dotenv(PROJECT_ROOT / ".env")
    value = os.getenv(name)
    if not value:
        if required:
            raise RuntimeError(f"Missing environment variable: {name}")
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def get_env_str(name: str, required: bool = False) -> str:
    """讀取字串型環境變數。"""

    load_dotenv(PROJECT_ROOT / ".env")
    value = os.getenv(name, "").strip()
    if not value and required:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def get_env_bool(name: str, default: bool = False) -> bool:
    """讀取布林型環境變數。

    支援：
    - true / false
    - 1 / 0
    - yes / no
    - on / off
    """

    load_dotenv(PROJECT_ROOT / ".env")
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def extract_case_steps(testrail_case: dict[str, Any]) -> list[TestRailStep]:
    """從 TestRail case payload 提取 step 清單。

    優先使用 separated steps；若 case 不是 step-based，則退回單一步驟模式。
    """

    separated_steps = testrail_case.get("custom_steps_separated")
    if isinstance(separated_steps, list):
        steps = []
        for index, step in enumerate(separated_steps, start=1):
            if not isinstance(step, dict):
                continue
            steps.append(
                TestRailStep(
                    content=str(step.get("content") or f"Step {index}"),
                    expected=str(step.get("expected") or ""),
                )
            )
        if steps:
            return steps

    return [
        TestRailStep(
            content=str(testrail_case.get("custom_steps") or "Automated validation"),
            expected=str(testrail_case.get("custom_expected") or ""),
        )
    ]


def case_uses_separated_steps(testrail_case: dict[str, Any]) -> bool:
    """Return True when the TestRail case uses separated step fields."""

    separated_steps = testrail_case.get("custom_steps_separated")
    return isinstance(separated_steps, list) and bool(separated_steps)


def build_local_case_steps(step_reports: dict[int, StepReport]) -> list[TestRailStep]:
    if not step_reports:
        return [TestRailStep(content="Automated validation", expected="")]
    return [
        TestRailStep(content=f"Step {index}", expected="")
        for index in sorted(step_reports.keys())
    ]


def format_elapsed(seconds: float) -> str:
    """把秒數格式化成 TestRail 可接受的 elapsed 字串。"""

    rounded = max(1, int(round(seconds)))
    return f"{rounded}s"
