"""測試環境與登入帳密解析。

`.env` 變數採 `{角色}_{環境}_{欄位}` 命名，例如：

    TEST_ENV=QA

    ADMIN_QA_LOGIN_URL=https://qa.example.com/login
    ADMIN_QA_USERNAME=...
    ADMIN_QA_PASSWORD=...

    ADMIN_STAGE_LOGIN_URL=https://stage.example.com/login
    ADMIN_STAGE_USERNAME=...
    ADMIN_STAGE_PASSWORD=...

角色與環境名稱都不寫死在程式碼裡：

- 切換環境 → 改 `TEST_ENV` 一個變數，整組帳密跟著換
- 新增角色（例如 `USER`、`OPERATOR`）→ 只要在 `.env` 補一組同前綴的變數，
  測試案例呼叫 `get_credentials("user")` 即可，共用模組不用改
"""

from __future__ import annotations

from dataclasses import dataclass

from testrail_client import get_env_str

# 未設定 TEST_ENV 時使用的環境名稱。
DEFAULT_ENV = "QA"


@dataclass(frozen=True)
class Credentials:
    """單一角色在目前環境下的登入資料。"""

    role: str
    env: str
    login_url: str
    username: str
    password: str


def resolve_env() -> str:
    """讀取目前測試環境名稱，未設定時回傳 DEFAULT_ENV。"""

    return (get_env_str("TEST_ENV", required=False).strip() or DEFAULT_ENV).upper()


def env_prefix(role: str, env: str | None = None) -> str:
    """組出 `.env` 變數前綴，例如 ("admin", "QA") -> "ADMIN_QA"。"""

    return f"{role.upper()}_{env or resolve_env()}"


def get_credentials(role: str) -> Credentials:
    """讀取指定角色在目前環境下的登入資料。

    三個欄位都是必填，缺少時直接報出變數名稱，避免帶著空字串去填表單。
    """

    env = resolve_env()
    prefix = env_prefix(role, env)
    return Credentials(
        role=role,
        env=env,
        login_url=get_env_str(f"{prefix}_LOGIN_URL", required=True),
        username=get_env_str(f"{prefix}_USERNAME", required=True),
        password=get_env_str(f"{prefix}_PASSWORD", required=True),
    )
