# playwright-testrail-template

Python + Playwright 的 E2E 測試範本，整合 TestRail 結果回報、截圖／錄影附件與 Slack 通知。

測試案例只需要寫「測試步驟」本身，開瀏覽器、建立截圖目錄、錄影、統一錯誤處理、
把結果連同截圖寫回 TestRail、以及執行進度通知，全部由框架處理。

## 特色

- **案例只寫 `steps()`** — 開瀏覽器／截圖／錄影／try-except／TestRail 回報都由 `run_case()` 承擔
- **截圖直接內嵌 TestRail** — 圖片上傳後以 `<img>` 嵌進對應步驟的實際結果欄位
- **未執行步驟自動補 BLOCKED** — 中途失敗時剩餘步驟不會留白
- **單一變數切換環境** — `ADMIN_ENV_MODE` 一改，整組 QA / STAGE 登入資料跟著換
- **Slack 進度就地更新** — 執行期間持續更新同一則訊息，中斷時明確標示未執行數量
- **零機敏資料進版控** — 所有憑證由環境變數提供，程式碼內沒有任何預設值

## 專案結構

| 路徑 | 說明 |
| --- | --- |
| `run_qa_tests.py` | 批次執行器：依 `.env` 挑選 testcase、建立 TestRail run、推送 Slack 進度。 |
| `testrail_client.py` | TestRail API client、step result 組裝、附件上傳與單支 testcase 回報。 |
| `slack.py` | Slack 進度與結果通知（Webhook + Bot Token 兩種發送方式）。 |
| `TEST/` | 測試案例。每支提供 `CASE_ID`、`TEST_RUN_ID` 與 `steps()`，並以 `run_case()` 執行。 |
| `TEST/test_case_sample.py` | 最小範例：登入後回報一個步驟。 |
| `TEST/test_case_example_multi_step.py` | 完整範例：多步驟、逐步截圖、FAILED 與中止後續步驟。 |
| `support/case_runner.py` | 案例共用執行骨架 `run_case()`。 |
| `support/browser.py` | 建立 Chromium page、錄影與影片保存。 |
| `support/login.py` | 後台登入 helper，支援 `platform` / `merchant`。 |
| `support/test_env.py` | 依 `ADMIN_ENV_MODE` 從 `.env` 解析登入設定。 |
| `support/screenshots.py` | 截圖路徑建立與截圖保存。 |
| `IMG/` | 截圖輸出目錄（執行時自動產生，已 gitignore）。 |

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

建立自己的 `.env`：

```powershell
Copy-Item .env.example .env
```

## `.env` 設定

### TestRail 連線（必填）

程式碼內沒有任何預設值，缺少時會直接報錯：

```env
TESTRAIL_URL=https://your-org.testrail.io/
TESTRAIL_USER=your-testrail-account@example.com
TESTRAIL_API_KEY=your-testrail-api-key
TESTRAIL_PROJECT_ID=1
```

### Slack 通知（`SLACK_NOTIFY_ENABLED=true` 時才需要）

```env
SLACK_WEBHOOK_URL=          # 最終結果通知
SLACK_BOT_TOKEN=            # 進度訊息就地更新用（xoxb-...）
SLACK_CHANNEL_ID=           # 進度訊息發送的頻道
SLACK_PROGRESS_INTERVAL_SECONDS=10
```

只填 `SLACK_WEBHOOK_URL` 也能運作，差別在於每次更新會是一則新訊息；
補上 `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` 才會就地更新同一則。

### 執行設定

```env
TESTRAIL_RUN_MODE=3
TESTRAIL_MULTI_TESTS=["test_case_sample"]
TESTRAIL_DAILY_TESTS=["test_case_sample"]
TESTRAIL_VERBOSE=false
TESTRAIL_REPORT_ENABLED=true
SLACK_NOTIFY_ENABLED=false
TESTRAIL_RECORD_VIDEO=false
```

| 變數 | 說明 |
| --- | --- |
| `TESTRAIL_RUN_MODE` | `2` 使用 `TESTRAIL_MULTI_TESTS`（沿用案例自己的 run），`3` 使用 `TESTRAIL_DAILY_TESTS`（每次建立新 run）。 |
| `TESTRAIL_MULTI_TESTS` / `TESTRAIL_DAILY_TESTS` | 要執行的 testcase，JSON array。 |
| `TESTRAIL_REPORT_ENABLED` | `true` 會回報 TestRail；本機驗證流程可設 `false`。 |
| `TESTRAIL_RECORD_VIDEO` | `true` 時 Playwright 會錄影，失敗時附到結果。 |
| `SLACK_NOTIFY_ENABLED` | `true` 時會送 Slack 通知。 |

`TESTRAIL_MULTI_TESTS` / `TESTRAIL_DAILY_TESTS` 三種寫法都可以：

- `CASE_ID`，例如 `6540`
- `TEST_RUN_ID`
- 測試檔名，例如 `"test_case_sample"` 或 `"test_case_sample.py"`

### 受測站台 QA / STAGE 切換

`ADMIN_ENV_MODE` 控制登入資料來源：

| 值 | 使用設定 |
| --- | --- |
| `0` 或 `QA` | `platform_QA_*`、`merchant_QA_*` |
| `1` 或 `STAGE` | `platform_STAGE_*`、`merchant_STAGE_*` |

```env
ADMIN_ENV_MODE=0

platform_QA_LOGIN_URL=https://qa.example.com/platform/login
platform_QA_ACCOUNT=your-qa-platform-account
platform_QA_PASSWORD=your-qa-platform-password
merchant_QA_LOGIN_URL=https://qa.example.com/merchant/<merchant-id>/login
merchant_QA_NAME=your-qa-merchant-name
merchant_QA_ACCOUNT=your-qa-merchant-account
merchant_QA_PASSWORD=your-qa-merchant-password
```

`STAGE` 同樣有一組 `platform_STAGE_*` / `merchant_STAGE_*`。把 `ADMIN_ENV_MODE`
從 `0` 改成 `1` 重新執行，就會整組切到 STAGE。

## 執行

```powershell
# 依 .env 設定批次執行
.\.venv\Scripts\python.exe run_qa_tests.py

# 只跑流程不回報 TestRail
.\.venv\Scripts\python.exe run_qa_tests.py --dry-run

# 指定建立的 run 名稱
.\.venv\Scripts\python.exe run_qa_tests.py --name "Daily Auto Check - 2026-01-01"

# 單獨執行某一支案例
.\.venv\Scripts\python.exe TEST\test_case_sample.py --dry-run
```

## 新增測試案例

複製 `TEST/test_case_sample.py`，改成自己的 `CASE_ID` / `TEST_RUN_ID`，
在 `steps()` 裡寫測試流程：

```python
from support.case_runner import run_case
from support.login import login_backoffice
from support.screenshots import save_step_screenshot
from testrail_client import PASSED, StepReport

CASE_ID = 0
TEST_RUN_ID = 0


def steps(page, screenshot_dir, step_reports):
    login_backoffice(page, "merchant")
    screenshot = save_step_screenshot(page, screenshot_dir, 1, "login")
    step_reports.append(
        StepReport(PASSED, "AUTOTEST: passed - 登入成功", [screenshot])
    )


if __name__ == "__main__":
    raise SystemExit(run_case(CASE_ID, TEST_RUN_ID, steps))
```

重點：

- `steps()` 收到 `(page, screenshot_dir, step_reports)`，直接往 `step_reports` **append** 而非
  `return` —— 這樣即使中途丟出例外，runner 仍保有已完成的 `StepReport` 可回報。
- `step_reports` 的第 N 筆對應 TestRail case 的第 N 個步驟，順序要一致。
- 例外處理不用自己寫，`run_case()` 會統一截圖、（開啟時）錄影並補上 `FAILED` 結果。
- 需要提前中止時呼叫 `abort_remaining_steps(step_reports)`，未執行步驟會自動補 `BLOCKED`。
- `TEST_RUN_ID <= 0` 預設視為 dry run。

`StepReport` 欄位：

| 欄位 | 說明 |
| --- | --- |
| `status_id` | TestRail status id：`PASSED`、`FAILED`、`BLOCKED`。 |
| `actual` | 寫入 TestRail 的執行結果文字。 |
| `attachments` | 要上傳的截圖或影片路徑 list，不附檔就傳 `[]`。圖片會內嵌到步驟結果，其他檔案掛在整筆 result。 |

完整用法可參考 [TEST/test_case_example_multi_step.py](TEST/test_case_example_multi_step.py)。

## Playwright Codegen

錄製操作產生 selector：

```powershell
.\.venv\Scripts\playwright.exe codegen --viewport-size="1920,1080" "https://example.com/login"
```

## CI

[.gitlab-ci.yml](.gitlab-ci.yml) 提供 GitLab CI 參考範例，只在 schedule 或手動觸發時執行。
（本範本託管於 GitHub，這份設定保留作為 GitLab runner 的寫法參考。）

**所有憑證都要放在 Settings > CI/CD > Variables 並勾選 Masked / Protected，不要寫進 yml。**
需要建立的變數清單寫在 `.gitlab-ci.yml` 的註解裡。

CI 流程：

1. 安裝 Python dependencies
2. 安裝 Playwright Chromium
3. 以 `xvfb-run` 執行 `python run_qa_tests.py`（headed 模式需要虛擬顯示器）
4. 保存 `IMG/` 截圖 artifact

## 常見問題

### Playwright 找不到 Chromium

```text
BrowserType.launch: Executable doesn't exist
```

執行 `.\.venv\Scripts\python.exe -m playwright install chromium`。

### `Missing environment variable: TESTRAIL_URL`

`.env` 沒建立或沒填 TestRail 連線資訊。本機只想跑流程不回報時，
可以用 `--dry-run` 或設 `TESTRAIL_REPORT_ENABLED=false`。

### `TEST_RUN_ID = 0` 無法回報 TestRail

不是 dry-run 時 `run_step_case()` 需要有效的 run id。解法：

- 把 testcase 的 `TEST_RUN_ID` 改成實際 run id
- 或執行時加 `--run-id <id>`
- 或本機驗證時使用 `--dry-run` / `TESTRAIL_REPORT_ENABLED=false`

### 修改 `.env` 後沒有生效

`.env` 只在該 key 尚未存在於環境變數時才載入（讓 CI variables 優先）。
修改後請重新執行測試程式。
