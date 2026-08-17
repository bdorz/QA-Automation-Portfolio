"""登入頁操作。

把「進登入頁 → 填帳密 → 送出 → 確認真的登入成功」這段流程收在一處，
讓每支測試案例不必重寫，也不必各自處理登入失敗的判斷。

帳密不寫在這裡，而是依角色從 `.env` 讀取（見 support/test_env.py）。

--------------------------------------------------------------------
需要依受測站台調整的地方只有下方三個 selector 常數。
預設用 input type 與 submit button 定位，適用多數表單式登入頁，
且不受登入頁語系（中／英）影響；站台結構不同時改這三行即可，
測試案例完全不用動。
--------------------------------------------------------------------
"""

from __future__ import annotations

from playwright.sync_api import Page, TimeoutError

from support.test_env import get_credentials

USERNAME_INPUT = 'input[type="text"]'
PASSWORD_INPUT = 'input[type="password"]'
SUBMIT_BUTTON = 'button[type="submit"]'

TIMEOUT_MS = 15_000


def login(page: Page, role: str = "admin") -> Page:
    """以指定角色登入，成功後回傳同一個已登入的 Page。

    role 對應 `.env` 的變數前綴，例如 "admin" -> ADMIN_{TEST_ENV}_*。
    """

    credentials = get_credentials(role)

    page.goto(credentials.login_url)
    page.wait_for_load_state("domcontentloaded")

    # 先確認帳號欄位真的出現，避免 URL 設錯或被導去其他頁時，
    # 仍盲目往下填帳密、最後才在莫名其妙的地方失敗。
    page.locator(USERNAME_INPUT).wait_for(state="visible", timeout=TIMEOUT_MS)

    page.locator(USERNAME_INPUT).fill(credentials.username, timeout=TIMEOUT_MS)
    page.locator(PASSWORD_INPUT).fill(credentials.password, timeout=TIMEOUT_MS)
    page.locator(SUBMIT_BUTTON).click(timeout=TIMEOUT_MS)

    wait_for_login_redirect(page, credentials.login_url, role)
    return page


def wait_for_login_redirect(page: Page, login_url: str, role: str) -> None:
    """確認送出後 URL 已離開登入頁。

    只按下按鈕不代表登入成功——帳密錯誤時頁面通常停在原地並顯示錯誤訊息。
    這裡以「URL path 已改變」作為成功依據，適用多數導向式登入。

    若受測站台登入後 path 不變（例如純前端切換畫面的 SPA），
    改成等待登入後才會出現的元素即可，例如：

        page.locator("#user-menu").wait_for(state="visible", timeout=TIMEOUT_MS)
    """

    try:
        page.wait_for_function(
            """(loginUrl) => {
                const current = new URL(window.location.href);
                const login = new URL(loginUrl);
                return current.pathname !== login.pathname;
            }""",
            arg=login_url,
            timeout=TIMEOUT_MS,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"{role} login did not leave the login page after submit. "
            f"Current URL: {page.url}"
        ) from exc

    # 登入後常有背景 API 仍在載入；等不到 networkidle 不影響登入結果，故忽略。
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except TimeoutError:
        pass
