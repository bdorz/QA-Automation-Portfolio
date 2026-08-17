import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from support.case_runner import run_case
from support.login import login_backoffice
from testrail_client import PASSED, StepReport

# 需要截圖時：from support.screenshots import save_step_screenshot

CASE_ID = 0
TEST_RUN_ID = 0


def steps(page, screenshot_dir, step_reports):
    """撰寫測試步驟。

    - page：Playwright page（runner 已設好 15s 預設 timeout）
    - screenshot_dir：截圖輸出目錄，用 save_step_screenshot(page, screenshot_dir, 步驟, 名稱)
    - step_reports：把每步的 StepReport append 進去（不要 return，中途失敗才留得住已完成結果）
    """
    login_backoffice(page, "merchant")
    login_backoffice(page, "platform")
    step_reports.append(
        StepReport(PASSED, "AUTOTEST: passed - backoffice login", [])
    )


if __name__ == "__main__":
    raise SystemExit(run_case(CASE_ID, TEST_RUN_ID, steps))
