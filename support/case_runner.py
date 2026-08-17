from __future__ import annotations

import traceback
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page

from support.browser import (
    build_case_video_dir,
    create_chromium_page,
    save_page_video,
)
from support.screenshots import build_case_screenshot_dir, save_step_screenshot
from testrail_client import FAILED, StepReport, get_env_bool, run_step_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TIMEOUT_MS = 15_000

# steps 收到 (page, screenshot_dir, step_reports)，直接往 step_reports append 而非
# return——這樣即使中途丟出例外，runner 仍保有已完成的 StepReport 可回報。
CaseSteps = Callable[[Page, Path, list[StepReport]], None]


def run_case(
    case_id: int,
    test_run_id: int,
    steps: CaseSteps,
    *,
    headless: bool = False,
    dry_run: bool | None = None,
) -> int:
    """各測試案例的共用執行骨架。

    負責建立截圖／錄影目錄、開瀏覽器、設定預設 timeout、統一的 try/except 錯誤
    處理（截圖＋錄影＋FAILED），以及最後回報 TestRail，讓個別案例只需在 steps()
    內專注撰寫測試流程。

    dry_run 未指定（None）時沿用「TEST_RUN_ID<=0 即 dry run」的預設，供案例單獨
    執行時使用；由批次執行器（run_qa_tests.py）呼叫時會顯式帶入其 --dry-run 值，
    讓新樣板案例與舊 main() 案例的 dry-run 行為一致。
    """

    resolved_dry_run = (test_run_id <= 0) if dry_run is None else dry_run
    screenshot_dir = build_case_screenshot_dir(PROJECT_ROOT, case_id)
    video_dir = build_case_video_dir(PROJECT_ROOT, case_id)
    record_video = get_env_bool("TESTRAIL_RECORD_VIDEO", default=False)
    step_reports: list[StepReport] = []

    with create_chromium_page(
        headless=headless,
        record_video_dir=video_dir if record_video else None,
    ) as page:
        try:
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)
            steps(page, screenshot_dir, step_reports)
        except Exception as exc:
            print(f"[FAILED] 發生例外，目前 URL: {page.url}")
            print(f"[FAILED] 錯誤訊息: {exc}")
            print(f"[FAILED] 詳細 traceback:\n{traceback.format_exc()}")
            screenshot = save_step_screenshot(
                page, screenshot_dir, 1, "unexpected_error"
            )
            video = (
                save_page_video(page, video_dir, "unexpected_error")
                if record_video
                else None
            )
            step_reports.append(
                StepReport(
                    FAILED,
                    f"AUTOTEST: failed - unexpected error: {exc}",
                    [path for path in [screenshot, video] if path is not None],
                )
            )

    return run_step_case(
        case_id,
        test_run_id,
        lambda: step_reports,
        dry_run=resolved_dry_run,
    )
