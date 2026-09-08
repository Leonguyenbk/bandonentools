"""Rasterize dữ liệu vector đọc được từ kml_vector_reader.py (Polygon/
LineString/Point + màu/độ dày nét theo Style) thành 1 ảnh RGBA trong
EPSG:3857 — dùng khi KMZ không có GroundOverlay raster sẵn (MicroStation
xuất thẳng đường nét/vùng dạng vector, không kèm ảnh — đã gặp thực tế).

Kết quả trả về là GeoreferencedOverlay — CÙNG kiểu dữ liệu
raster_pipeline.georeference_and_reproject() trả ra — nên tái dùng
nguyên hàm cắt tile generate_tiles()/count_tiles() đã có, không viết lại
logic tile.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw
import proj_setup  # Khắc phục xung đột PROJ_LIB / GDAL_DATA
from rasterio.transform import from_bounds

from kml_vector_reader import VectorFeature
from raster_pipeline import GeoreferencedOverlay

EARTH_RADIUS = 6378137.0
# Chặn tràn RAM nếu 1 tờ phủ diện tích lớn bất thường — 15000px/cạnh ứng
# với canvas RGBA tối đa ~860MB (15000*15000*4 byte), đã đo thực tế với 1
# tờ thật (869x1044m, 125K đối tượng): canvas 14343x11940 (dưới ngưỡng
# này) render trong 2.2s, không có dấu hiệu áp lực RAM bất thường trên
# desktop thường. Tờ phủ diện tích lớn hơn nhiều sẽ tự bị hạ zoom vẽ
# xuống (xem _canvas_size) thay vì tràn bộ nhớ.
MAX_CANVAS_DIMENSION = 15000
SUPERSAMPLE = 2  # vẽ ở độ phân giải gấp đôi rồi downsample — khử răng cưa đường/nét

DEFAULT_MIN_ZOOM = 14
DEFAULT_MAX_ZOOM = 20


def _lonlat_to_3857(lon: float, lat: float) -> tuple[float, float]:
    x = lon * (math.pi / 180.0 * EARTH_RADIUS)
    clamped_lat = max(min(lat, 85.05112878), -85.05112878)
    y = math.log(math.tan(math.pi / 4.0 + clamped_lat * (math.pi / 360.0))) * EARTH_RADIUS
    return x, y


def _meters_per_pixel(zoom: int) -> float:
    return (2 * math.pi * EARTH_RADIUS) / (256 * 2**zoom)


def _canvas_size(min_x, min_y, max_x, max_y, target_zoom: int) -> tuple[int, int, float, int]:
    """Trả (width, height, resolution_m_per_px, supersample_thực_dùng) —
    tự hạ supersample rồi hạ luôn zoom hiệu dụng nếu diện tích quá lớn,
    tránh cấp phát ảnh khổng lồ làm crash Tool."""
    supersample = SUPERSAMPLE
    zoom = target_zoom
    while True:
        resolution = _meters_per_pixel(zoom) / supersample
        width = max(1, int(round((max_x - min_x) / resolution)))
        height = max(1, int(round((max_y - min_y) / resolution)))
        if width <= MAX_CANVAS_DIMENSION and height <= MAX_CANVAS_DIMENSION:
            return width, height, resolution, supersample
        if supersample > 1:
            supersample = 1
            continue
        if zoom <= 5:
            return width, height, resolution, supersample
        zoom -= 1


def rasterize_to_overlay(
    features: list[VectorFeature],
    bbox_wgs84: tuple[float, float, float, float],
    target_zoom: int,
    on_progress=None,
) -> GeoreferencedOverlay:
    west, south, east, north = bbox_wgs84
    min_x, min_y = _lonlat_to_3857(west, south)
    max_x, max_y = _lonlat_to_3857(east, north)

    width, height, resolution, supersample = _canvas_size(min_x, min_y, max_x, max_y, target_zoom)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")

    # Tối ưu hóa tính toán tọa độ pixel (giảm chi phí gọi hàm lặp lại)
    inv_res = 1.0 / resolution
    scale_x = (math.pi / 180.0 * EARTH_RADIUS) * inv_res
    min_x_scaled = min_x * inv_res
    max_y_scaled = max_y * inv_res
    earth_rad_scaled = EARTH_RADIUS * inv_res
    deg_to_rad_half = math.pi / 360.0
    quarter_pi = math.pi / 4.0

    def to_px(lon: float, lat: float) -> tuple[float, float]:
        px = lon * scale_x - min_x_scaled
        clamped_lat = 85.05112878 if lat > 85.05112878 else (-85.05112878 if lat < -85.05112878 else lat)
        py = max_y_scaled - math.log(math.tan(quarter_pi + clamped_lat * deg_to_rad_half)) * earth_rad_scaled
        return px, py

    total = len(features)
    for index, feature in enumerate(features):
        style = feature.style

        if feature.kind == "Polygon":
            outer_px = [to_px(lon, lat) for lon, lat in feature.rings[0]]
            if len(outer_px) >= 3:
                width_px = max(1, round(style.line_width * supersample)) if style.has_outline else 0
                draw.polygon(
                    outer_px,
                    fill=style.fill_color,
                    outline=style.line_color if style.has_outline else None,
                    width=width_px or 1,
                )
                # Lỗ trong polygon (đảo/khoảng trống) — vẽ đè trong suốt để "khoét".
                for hole in feature.rings[1:]:
                    hole_px = [to_px(lon, lat) for lon, lat in hole]
                    if len(hole_px) >= 3:
                        draw.polygon(hole_px, fill=(0, 0, 0, 0))

        elif feature.kind == "LineString":
            points_px = [to_px(lon, lat) for lon, lat in feature.rings[0]]
            if len(points_px) >= 2:
                width_px = max(1, round(style.line_width * supersample))
                draw.line(points_px, fill=style.line_color, width=width_px, joint="curve")

        elif feature.kind == "Point" and feature.label:
            px, py = to_px(*feature.rings[0][0])
            draw.text((px, py), feature.label, fill=style.line_color)

        if on_progress and index % 5000 == 0:
            on_progress(index, total)

    if supersample > 1:
        image = image.resize((max(1, width // supersample), max(1, height // supersample)), Image.BILINEAR)

    array = np.array(image)  # H, W, 4
    bands = np.transpose(array, (2, 0, 1))
    transform = from_bounds(min_x, min_y, max_x, max_y, bands.shape[2], bands.shape[1])
    return GeoreferencedOverlay(bands, transform)

