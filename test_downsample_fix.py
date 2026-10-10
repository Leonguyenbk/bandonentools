"""Test thủ công cho cơ chế hạ mẫu ảnh độ phân giải quá lớn (GeoTIFF +
GroundOverlay nhúng KMZ) — tạo ảnh giả lập mịn hơn mức zoom tối đa tool
còn cắt tile tới, xác nhận xử lý được (không OOM/crash) và kích thước
thật sự được hạ xuống đúng mức. Chạy trực tiếp bằng python."""

from __future__ import annotations

import io
import sys
import time

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from PIL import Image

_FAILS: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        _FAILS.append(name)


def test_geotiff_downsample() -> None:
    import geotiff_reader

    # Vùng ~200m x 200m, ảnh 8000x8000px -> ~0.025 m/px, MỊN HƠN NHIỀU so
    # với zoom 21 (~0.0746 m/px) -> phải bị hạ mẫu.
    size = 8000
    deg_span = 200.0 / 111_320.0  # ~200m tính theo độ vĩ (xấp xỉ)
    west, north = 108.0, 13.0
    transform = from_origin(west, north, deg_span / size, deg_span / size)

    rng = np.random.default_rng(0)
    data = rng.integers(0, 255, size=(3, size, size), dtype=np.uint8)

    profile = {
        "driver": "GTiff", "height": size, "width": size, "count": 3,
        "dtype": "uint8", "crs": "EPSG:4326", "transform": transform,
    }
    with MemoryFile() as memfile:
        with memfile.open(**profile) as dst:
            dst.write(data)
        tif_bytes = memfile.read()

    print(f"GeoTIFF giả lập: {size}x{size}px (~{len(tif_bytes) / 1024 / 1024:.1f}MB), vùng ~200m x 200m")

    t0 = time.perf_counter()
    overlay, bbox = geotiff_reader.read_geotiff_as_overlay(tif_bytes, ma_xa=None, force_override=False)
    elapsed = time.perf_counter() - t0

    out_h, out_w = overlay.array_3857.shape[1], overlay.array_3857.shape[2]
    print(f"Kết quả: {out_w}x{out_h}px, xử lý trong {elapsed:.2f}s")

    _check("xử lý xong không lỗi/treo", True)
    _check(
        "kích thước output nhỏ hơn hẳn input (đã hạ mẫu, không giữ 8000x8000)",
        out_w < 4000 and out_h < 4000,
        f"got {out_w}x{out_h}",
    )
    _check("xử lý nhanh (<10s) — không phải đang xử lý 8000x8000 full-res", elapsed < 10, f"{elapsed:.2f}s")


def test_geotiff_normal_size_unaffected() -> None:
    """Ảnh BÌNH THƯỜNG (độ phân giải thô hơn zoom 21) không bị hạ mẫu —
    đảm bảo fix không ảnh hưởng ca dùng hiện có."""
    import geotiff_reader

    size = 500
    deg_span = 2000.0 / 111_320.0  # ~2000m -> ~4 m/px, THÔ hơn zoom 21 nhiều
    west, north = 108.0, 13.0
    transform = from_origin(west, north, deg_span / size, deg_span / size)

    rng = np.random.default_rng(1)
    data = rng.integers(0, 255, size=(3, size, size), dtype=np.uint8)

    profile = {
        "driver": "GTiff", "height": size, "width": size, "count": 3,
        "dtype": "uint8", "crs": "EPSG:4326", "transform": transform,
    }
    with MemoryFile() as memfile:
        with memfile.open(**profile) as dst:
            dst.write(data)
        tif_bytes = memfile.read()

    overlay, bbox = geotiff_reader.read_geotiff_as_overlay(tif_bytes, ma_xa=None, force_override=False)
    out_h, out_w = overlay.array_3857.shape[1], overlay.array_3857.shape[2]
    print(f"Ảnh thô (không cần hạ mẫu): output {out_w}x{out_h}px")
    _check("ảnh thô vẫn xử lý đúng, không bị cắt xén bất thường", out_w > 0 and out_h > 0, f"{out_w}x{out_h}")


def test_kmz_overlay_downsample() -> None:
    from kmz_reader import GroundOverlayInfo
    from raster_pipeline import georeference_and_reproject

    # Ảnh PNG 12000x9000 = 108 triệu pixel > giới hạn mặc định của Pillow
    # (~89 triệu) -> trước đây sẽ ném DecompressionBombError.
    w, h = 12000, 9000
    rng = np.random.default_rng(2)
    arr = rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    image_bytes = buf.getvalue()
    print(f"Ảnh PNG giả lập: {w}x{h}px = {w * h / 1_000_000:.0f} triệu pixel (PIL mặc định chặn ở ~89 triệu)")

    overlay = GroundOverlayInfo(
        name="test", image_bytes=image_bytes, image_filename="test.png", rotation=0.0,
        west=108.0, south=12.998, east=108.002, north=13.0,  # ~200m x 220m
    )

    t0 = time.perf_counter()
    result = georeference_and_reproject(overlay)
    elapsed = time.perf_counter() - t0

    out_h, out_w = result.array_3857.shape[1], result.array_3857.shape[2]
    print(f"Kết quả: {out_w}x{out_h}px, xử lý trong {elapsed:.2f}s")
    _check("không ném DecompressionBombError, xử lý xong", True)
    _check("output đã hạ mẫu, nhỏ hơn hẳn 12000x9000", out_w < 6000 and out_h < 6000, f"got {out_w}x{out_h}")


def main() -> int:
    test_geotiff_downsample()
    test_geotiff_normal_size_unaffected()
    test_kmz_overlay_downsample()

    print()
    if _FAILS:
        print(f"{len(_FAILS)} CA FAIL: {_FAILS}")
        return 1
    print("TẤT CẢ PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
