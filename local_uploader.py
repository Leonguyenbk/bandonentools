"""Upload tile lên WebGIS backend (Flask) chạy trên máy chủ tự lưu trữ —
thay thế storage_uploader.py (Supabase Storage/S3) sau khi hệ thống bỏ
Supabase. Đóng gói toàn bộ tile của 1 tờ thành 1 file zip trong RAM rồi
upload 1 request duy nhất (endpoint POST /api/ban-do-nen/upload-tiles),
tránh hàng nghìn request nhỏ như kiểu S3 cũ.

Giữ đúng chữ ký hàm `upload_tiles()` như storage_uploader.py cũ để
processing.py chỉ cần đổi tên module import, không phải sửa logic gọi.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

from config import ToolConfig


@dataclass
class UploadResult:
    total: int
    uploaded: int
    failed_keys: list[str]


def upload_tiles(
    cfg: ToolConfig,
    key_prefix: str,
    tiles: Iterable[tuple[int, int, int, bytes]],
    on_progress: Callable[[int, int], None] | None = None,
) -> UploadResult:
    tile_list = list(tiles)
    total = len(tile_list)
    if total == 0:
        return UploadResult(total=0, uploaded=0, failed_keys=[])

    # key_prefix luôn có dạng "{ma_xa}/{so_to}/v{version}" (xem processing.py)
    ma_xa, so_to, version_part = key_prefix.split("/")
    tile_version = version_part[1:]  # bỏ chữ "v" ở đầu

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for z, x, y, data in tile_list:
            archive.writestr(f"{z}/{x}/{y}.png", data)
    buffer.seek(0)

    try:
        response = requests.post(
            f"{cfg.webgis_api_url}/api/ban-do-nen/upload-tiles",
            headers={"X-Import-Token": cfg.import_token},
            data={"ma_xa": ma_xa, "so_to": so_to, "tile_version": tile_version},
            files={"tiles_zip": ("tiles.zip", buffer, "application/zip")},
            timeout=600,
        )
    except requests.RequestException as exc:
        return UploadResult(total=total, uploaded=0, failed_keys=[f"Lỗi kết nối: {exc}"])

    if not response.ok:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        return UploadResult(total=total, uploaded=0, failed_keys=[f"Upload thất bại ({response.status_code}): {detail}"])

    saved = response.json().get("saved", 0)
    if on_progress:
        on_progress(saved, total)

    failed = max(0, total - saved)
    failed_keys = [] if failed == 0 else [f"{failed} tile bị bỏ qua (không đúng định dạng z/x/y.png)"]
    return UploadResult(total=total, uploaded=saved, failed_keys=failed_keys)
