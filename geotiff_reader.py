# -*- coding: utf-8 -*-
"""Đọc và chuẩn hoá file GeoTIFF (.tif / .tiff) ĐỘC LẬP.
Tự động nắn tuyến, reproject sang EPSG:3857, hỗ trợ hệ toạ độ VN-2000 63 tỉnh thành,
đọc World file (.tfw), ghi đè CRS linh hoạt và phát hiện toạ độ chuẩn xác.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import proj_setup  # Khắc phục xung đột PROJ_LIB / GDAL_DATA
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.errors import CRSError, RasterioIOError
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

from raster_pipeline import (
    DEFAULT_MAX_ZOOM_CEIL,
    TILE_SIZE,
    WEB_MERCATOR_EQUATOR_CIRCUMFERENCE,
    GeoreferencedOverlay,
)
from vn2000_crs import (
    find_province_by_code,
    make_vn2000_crs,
    read_world_file_transform,
    resolve_crs_input,
)

MAX_TIF_BYTES = 1024 * 1024 * 1024  # 1GB
MAX_REPROJECT_PIXELS = 200_000_000

DST_CRS = "EPSG:3857"

# Độ phân giải (m/pixel, xấp xỉ ở xích đạo — cùng công thức
# compute_zoom_range dùng) ứng với zoom tối đa tool còn cắt tile tới
# (DEFAULT_MAX_ZOOM_CEIL). Ảnh nguồn mịn hơn mức này không hiển thị thêm
# chi tiết nào cả — đọc hạ mẫu ngay (decimated read) thay vì tải full-res
# vào RAM rồi mới biết là thừa.
_RES_AT_MAX_ZOOM = WEB_MERCATOR_EQUATOR_CIRCUMFERENCE / (TILE_SIZE * (2 ** DEFAULT_MAX_ZOOM_CEIL))


class GeoTiffError(Exception):
    pass


def _stretch_to_uint8(arr: np.ndarray) -> np.ndarray:
    """Kéo dãn tuyến tính min..max về 0..255 cho ảnh không phải 8-bit."""
    if arr.dtype == np.uint8:
        return arr
    data = arr.astype("float64")
    lo = float(np.nanmin(data))
    hi = float(np.nanmax(data))
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)
    return (((data - lo) / (hi - lo)) * 255.0).clip(0, 255).astype(np.uint8)


def _read_rgba(
    src, transform: Affine, out_height: int | None = None, out_width: int | None = None
) -> Tuple[np.ndarray, Affine]:
    """Chuẩn hoá dataset rasterio về mảng (4, H, W) uint8 RGBA. Nếu
    out_height/out_width khác kích thước gốc, dùng decimated read của
    rasterio/GDAL (hạ mẫu NGAY lúc đọc, không tải full-res vào RAM trước)
    — trả kèm transform đã co đúng theo tỉ lệ hạ mẫu."""
    count = src.count
    src_height, src_width = src.height, src.width
    out_height = out_height or src_height
    out_width = out_width or src_width
    downsample = (out_height, out_width) != (src_height, src_width)

    colormap = None
    if count == 1:
        try:
            colormap = src.colormap(1)
        except (ValueError, KeyError):
            colormap = None

    if downsample:
        # Dữ liệu palette (colormap) là CHỈ SỐ màu — nội suy song tuyến
        # tính sẽ pha trộn chỉ số thành giá trị rác, bắt buộc dùng nearest.
        read_resampling = Resampling.nearest if colormap else Resampling.bilinear
        raw = src.read(out_shape=(count, out_height, out_width), resampling=read_resampling)
        out_transform = transform * Affine.scale(src_width / out_width, src_height / out_height)
    else:
        raw = src.read()  # (count, H, W)
        out_transform = transform

    height, width = out_height, out_width

    if colormap:
        lut = np.zeros((256, 4), dtype=np.uint8)
        for key, value in colormap.items():
            if 0 <= key < 256:
                r, g, b = value[0], value[1], value[2]
                a = value[3] if len(value) > 3 else 255
                lut[key] = (r, g, b, a)
        band = np.clip(raw[0], 0, 255).astype(np.uint8)
        rgba = np.transpose(lut[band], (2, 0, 1))  # (4, H, W)
    elif count >= 4:
        rgb = _stretch_to_uint8(raw[:3])
        alpha = _stretch_to_uint8(raw[3:4])
        rgba = np.concatenate([rgb, alpha], axis=0)
    elif count == 3:
        rgb = _stretch_to_uint8(raw[:3])
        alpha = np.full((1, height, width), 255, dtype=np.uint8)
        rgba = np.concatenate([rgb, alpha], axis=0)
    elif count == 2:
        gray = _stretch_to_uint8(raw[0:1])
        alpha = _stretch_to_uint8(raw[1:2])
        rgba = np.concatenate([gray, gray, gray, alpha], axis=0)
    else:  # 1 band
        gray = _stretch_to_uint8(raw[0:1])
        alpha = np.full((1, height, width), 255, dtype=np.uint8)
        rgba = np.concatenate([gray, gray, gray, alpha], axis=0)

    try:
        if downsample:
            mask = src.dataset_mask(out_shape=(out_height, out_width), resampling=Resampling.nearest)
        else:
            mask = src.dataset_mask()
        rgba[3] = np.minimum(rgba[3], mask)
    except Exception:
        pass

    return np.ascontiguousarray(rgba), out_transform


def _open_dataset(
    data: bytes,
    override_crs: Optional[str] = None,
    file_path: Optional[str] = None,
    force_override: bool = True,
    ma_xa: Optional[str] = None,
):
    """Mở dataset TIF trong bộ nhớ, kiểm tra transform (kèm fallback .tfw),
    và giải quyết hệ toạ độ chính xác."""
    if len(data) > MAX_TIF_BYTES:
        raise GeoTiffError(f"File TIF vượt quá dung lượng tối đa ({MAX_TIF_BYTES // (1024 * 1024)}MB)")

    try:
        memfile = MemoryFile(data)
        src = memfile.open()
    except RasterioIOError as exc:
        raise GeoTiffError(f"Không đọc được định dạng GeoTIFF: {exc}") from exc

    # 1. Kiểm tra Transform toạ độ
    transform = src.transform
    if (transform is None or transform.is_identity) and file_path:
        world_tf = read_world_file_transform(file_path)
        if world_tf is not None:
            transform = world_tf

    if transform is None or transform.is_identity:
        src.close()
        memfile.close()
        raise GeoTiffError(
            "File TIF không có toạ độ thực địa (transform identity) và không tìm thấy file .tfw. "
            "Vui lòng kiểm tra lại file bản đồ đã được georeference chưa."
        )

    file_crs = src.crs
    src_crs: Optional[CRS] = None
    crs_desc: str = ""

    # 2. Xử lý ghi đè / nhận diện CRS — GIỮ NGUYÊN transform (số XY), chỉ
    #    thay "nhãn" hệ toạ độ rồi reproject sang Web Mercator.

    # (a) Người dùng nhập tay tỉnh / KTT / EPSG (ưu tiên cao nhất)
    manual: Optional[Tuple[CRS, str]] = None
    if override_crs and override_crs.strip():
        try:
            parsed_crs, desc = resolve_crs_input(override_crs)
        except Exception as e:
            src.close()
            memfile.close()
            raise GeoTiffError(f"Lỗi cấu hình CRS: {e}") from e
        if parsed_crs is not None:
            manual = (parsed_crs, f"{desc} (Ép ghi đè)")

    # (b) Suy KTT từ mã xã (tự động)
    auto_prov: Optional[Tuple[CRS, str]] = None
    if ma_xa and ma_xa.strip():
        p_info = find_province_by_code(ma_xa)
        if p_info:
            auto_prov = (
                make_vn2000_crs(p_info[2], zone_3=True),
                f"VN-2000 {p_info[1]} (KTT {p_info[3]}, tự động theo mã xã {ma_xa})",
            )

    if manual is not None:
        src_crs, crs_desc = manual
    elif force_override and auto_prov is not None:
        # "Ép ghi đè" bật + không nhập tay -> tin mã xã hơn thẻ CRS trong file
        src_crs, crs_desc = auto_prov
        crs_desc += " [Ép ghi đè]"
    elif file_crs is not None:
        src_crs = file_crs
        crs_desc = f"{file_crs.to_string()} (Theo file)"
    elif auto_prov is not None:
        src_crs, crs_desc = auto_prov

    if src_crs is None:
        src.close()
        memfile.close()
        raise GeoTiffError(
            "File TIF không chứa hệ toạ độ (CRS). Vui lòng chọn Tỉnh / Kinh tuyến trục (KTT) "
            "hoặc nhập mã EPSG nguồn trên giao diện."
        )

    # Tính bounds theo transform thực tế
    width, height = src.width, src.height
    xs = (0, width, width, 0)
    ys = (0, 0, height, height)
    proj_xs, proj_ys = rasterio.transform.xy(transform, ys, xs)
    left, right = min(proj_xs), max(proj_xs)
    bottom, top = min(proj_ys), max(proj_ys)
    bounds = (left, bottom, right, top)

    return memfile, src, transform, bounds, file_crs, src_crs, crs_desc


def describe_geotiff(
    data: bytes,
    override_crs: Optional[str] = None,
    file_path: Optional[str] = None,
    force_override: bool = True,
    ma_xa: Optional[str] = None,
) -> Tuple[Tuple[float, float, float, float], str]:
    """Đọc nhanh metadata, tính toạ độ tâm WGS84 và kiểm tra độ hợp lệ."""
    memfile, src, transform, bounds, file_crs, src_crs, crs_desc = _open_dataset(
        data, override_crs, file_path, force_override, ma_xa
    )
    try:
        bbox_wgs84 = transform_bounds(src_crs, "EPSG:4326", *bounds, densify_pts=21)
        west, south, east, north = bbox_wgs84
        center_lon = (west + east) / 2.0
        center_lat = (south + north) / 2.0

        # Kiểm tra xem toạ độ có rơi vào lãnh thổ Việt Nam không (Lat: 8..24, Lon: 102..110)
        is_in_vn = (8.0 <= center_lat <= 24.0) and (102.0 <= center_lon <= 110.5)
        loc_warning = "" if is_in_vn else " ⚠️ LỆCH NGOÀI VN - Kiểm tra lại KTT!"

        res_m = abs(transform.a)
        desc = (
            f"GeoTIFF {src.width}x{src.height}px (~{res_m:.2f}m/px) | "
            f"Tâm: [{center_lat:.4f}°N, {center_lon:.4f}°E] | {crs_desc}{loc_warning}"
        )
        return bbox_wgs84, desc
    finally:
        src.close()
        memfile.close()


def read_geotiff_as_overlay(
    data: bytes,
    override_crs: Optional[str] = None,
    file_path: Optional[str] = None,
    force_override: bool = True,
    ma_xa: Optional[str] = None,
) -> Tuple[GeoreferencedOverlay, Tuple[float, float, float, float]]:
    """Đọc file GeoTIFF và chuyển đổi sang GeoreferencedOverlay (EPSG:3857 RGBA)."""
    memfile, src, transform, bounds, _file_crs, src_crs, _crs_desc = _open_dataset(
        data, override_crs, file_path, force_override, ma_xa
    )
    try:
        # Tính độ phân giải SAU reproject Ở ĐỘ PHÂN GIẢI GỐC — chỉ cần
        # metadata (width/height/bounds), KHÔNG đọc pixel — để biết ảnh có
        # mịn hơn mức zoom tối đa tool còn dùng tới hay không, TRƯỚC KHI
        # đọc. Ảnh mịn hơn thì hạ mẫu ngay lúc đọc (decimated read), tránh
        # tải full-res vào RAM một cách vô ích (và tránh chạm trần
        # MAX_REPROJECT_PIXELS chỉ vì mật độ pixel nguồn quá cao chứ không
        # phải vì phạm vi địa lý thật sự lớn).
        native_transform, _native_w, _native_h = calculate_default_transform(
            src_crs, DST_CRS, src.width, src.height, *bounds
        )
        native_res = abs(native_transform.a)

        read_h, read_w = src.height, src.width
        if native_res < _RES_AT_MAX_ZOOM:
            scale = native_res / _RES_AT_MAX_ZOOM
            read_h = max(1, round(src.height * scale))
            read_w = max(1, round(src.width * scale))

        rgba, transform = _read_rgba(src, transform, out_height=read_h, out_width=read_w)

        dst_transform, dst_w, dst_h = calculate_default_transform(
            src_crs, DST_CRS, read_w, read_h, *bounds
        )

        if dst_w * dst_h > MAX_REPROJECT_PIXELS:
            raise GeoTiffError(
                f"Ảnh sau khi reproject quá lớn ({dst_w}x{dst_h}px). "
                "Vui lòng chia nhỏ file hoặc giảm độ phân giải trước khi xử lý."
            )

        dst_array = np.zeros((4, dst_h, dst_w), dtype=np.uint8)
        for band_idx in range(4):
            reproject(
                source=rgba[band_idx],
                destination=dst_array[band_idx],
                src_transform=transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=DST_CRS,
                resampling=Resampling.bilinear,
            )

        bbox_wgs84 = transform_bounds(src_crs, "EPSG:4326", *bounds, densify_pts=21)
    finally:
        src.close()
        memfile.close()

    west, south, east, north = bbox_wgs84
    return GeoreferencedOverlay(np.ascontiguousarray(dst_array), dst_transform), (
        west,
        south,
        east,
        north,
    )
