from __future__ import annotations

from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error, Page


def build_case_screenshot_dir(project_root: Path, case_id: int) -> Path:
    """建立單一 testcase 的截圖輸出目錄，目錄名稱會帶執行時間。"""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return project_root / "IMG" / f"C{case_id}_{timestamp}"


def save_step_screenshot(
    page: Page,
    screenshot_dir: Path,
    step_no: int,
    name: str,
) -> Path | None:
    """保存指定步驟的 full-page 截圖；若 page 已關閉或截圖失敗則回傳 None。"""

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / f"step_{step_no:02d}_{name}.png"

    try:
        if page.is_closed():
            return None
        page.screenshot(path=screenshot_path, full_page=True)
        return screenshot_path
    except Error:
        # 截圖只是輔助附件，失敗時不應蓋掉原本的 testcase exception。
        return None
