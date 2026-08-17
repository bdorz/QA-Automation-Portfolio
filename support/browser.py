from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import Error, Page, sync_playwright


# 統一用 1920x1080 執行，避免不同機器解析度造成定位或截圖差異。
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080

# 暫存尚未能複製的 Playwright video 檔案路徑，等 context 關閉後再保存。
PENDING_VIDEO_SAVES_ATTR = "_qa_pending_video_saves"


@contextmanager
def create_chromium_page(
    headless: bool = False,
    record_video_dir: Path | None = None,
) -> Iterator[Page]:
    """建立 Chromium page，並在離開 context manager 時自動關閉瀏覽器。"""

    launch_args = [
        f"--window-size={VIDEO_WIDTH},{VIDEO_HEIGHT}",
        "--window-position=0,0",
        "--force-device-scale-factor=1",
        "--start-maximized",
    ]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            args=launch_args,
        )
        context_options: dict[str, object] = {
            "viewport": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
            "screen": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
        }
        if record_video_dir is not None:
            record_video_dir.mkdir(parents=True, exist_ok=True)
            context_options["record_video_dir"] = str(record_video_dir)
            context_options["record_video_size"] = {
                "width": VIDEO_WIDTH,
                "height": VIDEO_HEIGHT,
            }

        context = browser.new_context(**context_options, no_viewport=True)
        page = context.new_page()
        try:
            yield page
        finally:
            # Playwright video 需要 context 關閉後才會完成寫入，因此先關 context 再複製影片。
            context.close()
            flush_pending_page_videos(page)
            browser.close()


def build_case_video_dir(project_root: Path, case_id: int) -> Path:
    """建立單一 testcase 的錄影輸出目錄。"""

    return Path(tempfile.gettempdir()) / "qa-test-automation-videos" / f"C{case_id}_video"


def save_page_video(page: Page, video_dir: Path, name: str) -> Path | None:
    """保存目前 page 的錄影；若影片尚未完成寫入，先登記為待保存。"""

    video = page.video
    if video is None:
        return None

    target_path = video_dir / f"{name}.webm"
    try:
        source_path = Path(video.path())
    except Error:
        return None

    if page.is_closed():
        # page 已關閉時，影片檔通常已完成寫入，可以直接複製。
        saved_path = save_video_file(source_path, target_path)
        if saved_path is not None and source_path != target_path:
            remove_video_file(source_path)
        return saved_path

    # page 還沒關閉時，Playwright 可能尚未 flush video，延後到 context close 後保存。
    pending_saves = get_pending_page_videos(page)
    pending_saves.append((source_path, target_path))
    return target_path


def get_pending_page_videos(page: Page) -> list[tuple[Path, Path]]:
    """取得 page 上暫存的待保存影片清單。"""

    pending_saves = getattr(page, PENDING_VIDEO_SAVES_ATTR, None)
    if pending_saves is None:
        pending_saves = []
        setattr(page, PENDING_VIDEO_SAVES_ATTR, pending_saves)
    return pending_saves


def flush_pending_page_videos(page: Page) -> None:
    """將待保存影片複製到目標路徑，並清理已搬移的暫存檔。"""

    pending_saves = get_pending_page_videos(page)
    saved_source_paths: set[Path] = set()
    target_paths: set[Path] = set()

    for source_path, target_path in pending_saves:
        if save_video_file(source_path, target_path):
            saved_source_paths.add(source_path)
            target_paths.add(target_path)

    for source_path in saved_source_paths:
        if source_path not in target_paths:
            remove_video_file(source_path)

    pending_saves.clear()


def save_video_file(source_path: Path, target_path: Path) -> Path | None:
    """複製錄影檔到指定位置；失敗時回傳 None，避免覆蓋原始測試錯誤。"""

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()
        shutil.copy2(source_path, target_path)
        return target_path
    except (Error, OSError):
        return None


def remove_video_file(path: Path) -> None:
    """刪除 Playwright 產生的暫存影片檔；刪除失敗時忽略。"""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
