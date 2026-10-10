"""Upload tile lên WebGIS backend (Flask) chạy trên máy chủ tự lưu trữ —
thay thế storage_uploader.py (Supabase Storage/S3) sau khi hệ thống bỏ
Supabase. Đóng gói tile của 1 tờ thành các file zip ~40MB trong RAM rồi
upload lần lượt (endpoint POST /api/ban-do-nen/upload-tiles), tránh hàng
nghìn request nhỏ như kiểu S3 cũ mà vẫn dưới giới hạn 100MB của Cloudflare.

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
from webgis_client import mo_ta_loi_http, mo_ta_loi_ket_noi


@dataclass
class UploadResult:
    total: int
    uploaded: int
    failed_keys: list[str]


# Cloudflare (đứng trước server) chặn request > 100MB: gửi cả tờ lớn trong 1
# zip sẽ bị cắt kết nối giữa chừng (WinError 10053). Chia thành nhiều gói
# nhỏ, mỗi gói tối đa ~40MB, gửi lần lượt vào CÙNG ma_xa/so_to/tile_version.
MAX_BATCH_BYTES = 40 * 1024 * 1024
SO_LAN_THU_LAI = 2


def _chia_goi(tile_list: list[tuple[int, int, int, bytes]]) -> list[list[tuple[int, int, int, bytes]]]:
    goi_list: list[list[tuple[int, int, int, bytes]]] = []
    goi: list[tuple[int, int, int, bytes]] = []
    kich_thuoc = 0
    for tile in tile_list:
        if goi and kich_thuoc + len(tile[3]) > MAX_BATCH_BYTES:
            goi_list.append(goi)
            goi, kich_thuoc = [], 0
        goi.append(tile)
        kich_thuoc += len(tile[3])
    if goi:
        goi_list.append(goi)
    return goi_list


def _dong_zip(goi: list[tuple[int, int, int, bytes]]) -> io.BytesIO:
    buffer = io.BytesIO()
    # PNG đã nén sẵn — ZIP_STORED nhanh hơn, kích thước gần như không đổi
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for z, x, y, data in goi:
            archive.writestr(f"{z}/{x}/{y}.png", data)
    buffer.seek(0)
    return buffer


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

    url = f"{cfg.webgis_api_url}/api/ban-do-nen/upload-tiles"
    goi_list = _chia_goi(tile_list)
    tong_saved = 0
    skipped = 0

    for so_goi, goi in enumerate(goi_list, start=1):
        nhan_goi = f"[gói {so_goi}/{len(goi_list)}, đã lên {tong_saved}/{total} tile]"
        response = None
        for lan in range(SO_LAN_THU_LAI + 1):
            try:
                response = requests.post(
                    url,
                    headers={"X-Import-Token": cfg.import_token},
                    data={"ma_xa": ma_xa, "so_to": so_to, "tile_version": tile_version},
                    files={"tiles_zip": ("tiles.zip", _dong_zip(goi), "application/zip")},
                    timeout=600,
                )
                break
            except requests.RequestException as exc:
                if lan == SO_LAN_THU_LAI:
                    return UploadResult(
                        total=total, uploaded=tong_saved,
                        failed_keys=[f"{nhan_goi} {mo_ta_loi_ket_noi(exc, url)}"],
                    )

        if not response.ok:
            return UploadResult(
                total=total, uploaded=tong_saved,
                failed_keys=[f"{nhan_goi} {mo_ta_loi_http(response, 'Upload tile')}"],
            )

        saved = response.json().get("saved", 0)
        tong_saved += saved
        skipped += max(0, len(goi) - saved)
        if on_progress:
            on_progress(tong_saved, total)

    failed_keys = [] if skipped == 0 else [f"{skipped} tile bị bỏ qua (không đúng định dạng z/x/y.png)"]
    return UploadResult(total=total, uploaded=tong_saved, failed_keys=failed_keys)
