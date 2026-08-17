from __future__ import annotations

from dataclasses import dataclass

from testrail_client import get_env_str


@dataclass(frozen=True)
class PlatformCredentials:
    """平台後台登入資料，來源為 .env 的 platform_QA_* 或 platform_STAGE_*。"""

    login_url: str
    account: str
    password: str
    mode: str


@dataclass(frozen=True)
class MerchantCredentials:
    """商戶後台登入資料，來源為 .env 的 merchant_QA_* 或 merchant_STAGE_*。"""

    login_url: str
    name: str
    account: str
    password: str
    mode: str


def get_platform_credentials() -> PlatformCredentials:
    """依 ADMIN_ENV_MODE 讀取平台後台登入 URL、帳號與密碼。"""

    mode = resolve_env_mode("ADMIN_ENV_MODE")
    platform_prefix = env_prefix("platform", mode)
    return PlatformCredentials(
        login_url=get_env_str(f"{platform_prefix}_LOGIN_URL", required=True),
        account=get_env_str(f"{platform_prefix}_ACCOUNT", required=True),
        password=get_env_str(f"{platform_prefix}_PASSWORD", required=True),
        mode=mode,
    )


def get_merchant_credentials() -> MerchantCredentials:
    """依 ADMIN_ENV_MODE 讀取商戶後台登入 URL、代理名稱、帳號與密碼。"""

    mode = resolve_env_mode("ADMIN_ENV_MODE")
    merchant_prefix = env_prefix("merchant", mode)
    return MerchantCredentials(
        login_url=get_env_str(f"{merchant_prefix}_LOGIN_URL", required=True),
        name=get_env_str(f"{merchant_prefix}_NAME", required=True),
        account=get_env_str(f"{merchant_prefix}_ACCOUNT", required=True),
        password=get_env_str(f"{merchant_prefix}_PASSWORD", required=True),
        mode=mode,
    )


def resolve_env_mode(name: str) -> str:
    """讀取環境模式；未設定時預設使用 QA。"""

    value = get_env_str(name, required=False).strip()
    if value:
        return normalize_mode(value, env_name=name)
    return "QA"


def normalize_mode(value: str, env_name: str) -> str:
    """將 ADMIN_ENV_MODE 的 0/1/QA/STAGE 正規化成 QA 或 STAGE。"""

    normalized = value.strip().upper()
    if normalized in {"0", "QA"}:
        return "QA"
    if normalized in {"1", "STAGE"}:
        return "STAGE"
    raise RuntimeError(f"{env_name} must be 0/1 or QA/STAGE")


def env_prefix(base: str, mode: str) -> str:
    """依後台類型與環境模式組出 .env 變數前綴。"""

    if mode == "QA":
        return f"{base}_QA"
    if mode == "STAGE":
        return f"{base}_STAGE"
    raise RuntimeError(f"Unsupported environment mode: {mode}")
