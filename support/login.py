from __future__ import annotations

from typing import Literal

from playwright.sync_api import Page, TimeoutError

from support.test_env import get_merchant_credentials, get_platform_credentials


# 後台類型只能是平台後台或商戶後台，避免呼叫端傳入錯誤字串。
BackofficeKind = Literal["platform", "merchant"]

# 用 type/結構定位而非 placeholder 文字，避免登入頁語系（中/英）不同導致 selector 失效。
ACCOUNT_INPUT = 'input[type="text"]'
PASSWORD_INPUT = 'input[type="password"]'
LOGIN_BUTTON = 'button[type="submit"]'
TIMEOUT_MS = 15_000


def login_backoffice(page: Page, kind: BackofficeKind) -> Page:
    """登入指定後台，成功後回傳同一個已登入的 Page。"""

    if kind == "platform":
        credentials = get_platform_credentials()
    elif kind == "merchant":
        credentials = get_merchant_credentials()
    else:
        raise ValueError(f"Unsupported backoffice kind: {kind}")

    login_url = credentials.login_url

    # 進入登入頁後先確認頁面標題，避免 URL 設錯卻繼續填帳密。
    page.goto(login_url)
    page.wait_for_load_state("domcontentloaded")
    page.get_by_text(f"{kind.title()} Backoffice", exact=True).wait_for(
        state="visible",
        timeout=TIMEOUT_MS,
    )

    # 填入 .env 依 ADMIN_ENV_MODE 解析出的帳號密碼。
    page.locator(ACCOUNT_INPUT).fill(credentials.account, timeout=TIMEOUT_MS)
    page.locator(PASSWORD_INPUT).fill(credentials.password, timeout=TIMEOUT_MS)
    page.locator(LOGIN_BUTTON).click(timeout=TIMEOUT_MS)

    wait_for_login_redirect(page, login_url, kind)
    return page


def wait_for_login_redirect(page: Page, login_url: str, kind: BackofficeKind) -> None:
    """確認送出登入後 URL 已離開 /login，避免只按了按鈕卻誤判成功。"""

    try:
        page.wait_for_function(
            r"""([loginUrl, kind]) => {
                const current = new URL(window.location.href);
                const login = new URL(loginUrl);
                const loginPath = new RegExp(`/${kind}/(?:[^/]+/)?login/?$`);
                return current.href !== login.href && !loginPath.test(current.pathname);
            }""",
            arg=[login_url, kind],
            timeout=TIMEOUT_MS,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"{kind} login did not redirect after submit. Current URL: {page.url}"
        ) from exc

    # 登入後部分 API 可能仍在背景載入；等待 networkidle 失敗不影響登入結果。
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except TimeoutError:
        pass
