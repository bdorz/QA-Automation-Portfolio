"""多步驟測試案例範例。

示範這套框架比較完整的用法：每個步驟各自截圖、各自 append 一筆 StepReport、
以及失敗時如何回報 FAILED 並中止後續步驟。

實際使用時把 CASE_ID / TEST_RUN_ID 換成 TestRail 上的真實 ID，
並把 TODO 標記的 selector 換成受測站台的實際元素。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import expect

from support.case_runner import run_case
from support.login import login_backoffice
from support.screenshots import save_step_screenshot
from support.test_env import get_merchant_credentials, resolve_env_mode
from testrail_client import FAILED, PASSED, StepReport, abort_remaining_steps

# TestRail 上的 case id 與預設回報的 run id。
# 兩者維持 0 時會被視為 dry run，方便本機先跑通流程再接 TestRail。
CASE_ID = 0
TEST_RUN_ID = 0


def steps(page, screenshot_dir, step_reports):
    """撰寫測試步驟。

    - page：Playwright page（runner 已設好 15s 預設 timeout）
    - screenshot_dir：截圖輸出目錄，用 save_step_screenshot(page, screenshot_dir, 步驟, 名稱)
    - step_reports：把每步的 StepReport append 進去（不要 return，中途失敗才留得住已完成結果）

    step_reports 的第 N 筆會對應到 TestRail case 的第 N 個步驟，順序要一致。
    """

    # 目前執行環境（QA / STAGE）與該環境的商戶資料，都由 .env 的 ADMIN_ENV_MODE 決定。
    env_mode = resolve_env_mode("ADMIN_ENV_MODE")
    merchant = get_merchant_credentials()
    print(f"[前置] 執行環境={env_mode}，商戶={merchant.name}")

    # ------------------------------------------------------------------
    # 步驟 1：登入商戶後台
    # ------------------------------------------------------------------
    print("[1] 登入商戶後台")
    login_backoffice(page, "merchant")

    # 截圖存成 IMG/C{CASE_ID}_{時間戳}/step_01_merchant_login.png，
    # 回報時會自動上傳並內嵌到 TestRail 的該步驟結果裡。
    screenshot = save_step_screenshot(page, screenshot_dir, 1, "merchant_login")
    step_reports.append(
        StepReport(
            PASSED,
            f"AUTOTEST: passed - Step 1: 以 {merchant.account} 登入商戶後台（{env_mode}）",
            [screenshot],
        )
    )

    # ------------------------------------------------------------------
    # 步驟 2：進入功能頁並驗證畫面
    # ------------------------------------------------------------------
    print("[2] 進入側邊欄第一個功能頁")
    page.locator(".sidebar__item-label").first.click()  # TODO: 換成實際的選單 selector

    # expect() 會自動等待，比 sleep 穩定；逾時會拋例外，
    # 由 case_runner 統一截圖並補上 FAILED，不需要自己 try/except。
    expect(page.locator("table")).to_be_visible()  # TODO: 換成實際要驗證的元素

    screenshot = save_step_screenshot(page, screenshot_dir, 2, "list_page")
    step_reports.append(
        StepReport(
            PASSED,
            "AUTOTEST: passed - Step 2: 功能頁列表正確顯示",
            [screenshot],
        )
    )

    # ------------------------------------------------------------------
    # 步驟 3：自行判斷結果，示範主動回報 FAILED
    # ------------------------------------------------------------------
    print("[3] 檢查列表是否有資料")
    row_count = page.locator("table tbody tr").count()
    screenshot = save_step_screenshot(page, screenshot_dir, 3, "row_count")

    if row_count > 0:
        step_reports.append(
            StepReport(
                PASSED,
                f"AUTOTEST: passed - Step 3: 列表共 {row_count} 筆資料",
                [screenshot],
            )
        )
    else:
        step_reports.append(
            StepReport(
                FAILED,
                "AUTOTEST: failed - Step 3: 列表沒有任何資料",
                [screenshot],
            )
        )
        # 後續步驟沒有意義時，用 abort_remaining_steps 中止，
        # 未執行的步驟會自動以 BLOCKED 回報，而不是留白。
        abort_remaining_steps(step_reports)

    # ------------------------------------------------------------------
    # 步驟 4：不需要截圖的步驟，attachments 傳空 list 即可
    # ------------------------------------------------------------------
    print("[4] 確認頁面標題")
    step_reports.append(
        StepReport(
            PASSED,
            f"AUTOTEST: passed - Step 4: 頁面標題為 {page.title()}",
            [],
        )
    )


if __name__ == "__main__":
    raise SystemExit(run_case(CASE_ID, TEST_RUN_ID, steps))
