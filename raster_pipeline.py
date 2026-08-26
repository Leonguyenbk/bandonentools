"""Georeference GroundOverlay (ảnh + tọa độ từ KMZ) -> reproject EPSG:3857
-> sinh tile XYZ {z}/{x}/{y}.png (RGBA, nền trong suốt ngoài phạm vi ảnh).

Tối ưu hóa:
1. Georeference + reproject ảnh gốc về EPSG:3857 (dùng rasterio/GDAL 1 lần duy nhất).
2. Sinh tile song song đa luồng (ThreadPoolExecutor): Cắt và phóng to/thu nhỏ
   trực tiếp bằng Pillow (C-level sub-pixel resample) vì cả ảnh nguồn lẫn tile
   đều đã nằm trong cùng hệ phẳng EPSG:3857 (Web Mercator).
3. Nén PNG tốc độ cao (compress_level=1, optimize=False), bỏ qua tile rỗng
   ngay từ bước tính bounding box.
"""

from __future__ import annotations

import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

import mercantile
import numpy as np
import rasterio
from PIL import Image
from rasterio.control import GroundControlPoint
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

from kmz_reader import GroundOverlayInfo

TILE_SIZE = 256
WEB_MERCATOR_EQUATOR_CIRCUMFERENCE = 2 * math.pi * 6378137.0
DEFAULT_MIN_ZOOM_FLOOR = 10
DEFAULT_MAX_ZOOM_CEIL = 21


class GeoreferencedOverlay:
    __slots__ = ("array_3857", "transform_3857")

    def __init__(self, array_3857: np.ndarray, transform_3857):
        self.array_3857 = array_3857  # (4, H, W) uint8 RGBA
        self.transform_3857 = transform_3857


def _load_rgba_array(image_bytes: bytes) -> np.ndarray:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    arr = np.array(image)  # H, W, 4
    return np.transpose(arr, (2, 0, 1))  # 4, H, W


def georeference_and_reproject(overlay: GroundOverlayInfo) -> GeoreferencedOverlay:
    bands = _load_rgba_array(overlay.image_bytes)
    _, height, width = bands.shape

    with MemoryFile() as memfile:
        if overlay.is_quad:
            # 4 góc KML: dưới-trái, dưới-phải, trên-phải, trên-trái —
            # tương ứng pixel (0,H), (W,H), (W,0), (0,0).
            (blx, bly), (brx, bry), (trx, try_), (tlx, tly) = overlay.quad_lonlat
            gcps = [
                GroundControlPoint(row=height, col=0, x=blx, y=bly),
                GroundControlPoint(row=height, col=width, x=brx, y=bry),
                GroundControlPoint(row=0, col=width, x=trx, y=try_),
                GroundControlPoint(row=0, col=0, x=tlx, y=tly),
            ]
            with memfile.open(
                driver="GTiff", height=height, width=width, count=4,
                dtype=bands.dtype, crs="EPSG:4326", gcps=(gcps, "EPSG:4326"),
            ) as dataset:
                dataset.write(bands)
            with memfile.open() as src:
                dst_crs = "EPSG:3857"
                transform, dst_w, dst_h = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, gcps=src.gcps[0]
                )
                dst_array = np.zeros((4, dst_h, dst_w), dtype=bands.dtype)
                for band_index in range(1, 5):
                    reproject(
                        source=rasterio.band(src, band_index),
                        destination=dst_array[band_index - 1],
                        src_crs=src.crs,
                        gcps=src.gcps[0],
                        dst_transform=transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.bilinear,
                    )
        else:
            src_transform = from_bounds(overlay.west, overlay.south, overlay.east, overlay.north, width, height)
            with memfile.open(
                driver="GTiff", height=height, width=width, count=4,
                dtype=bands.dtype, crs="EPSG:4326", transform=src_transform,
            ) as dataset:
                dataset.write(bands)
            with memfile.open() as src:
                dst_crs = "EPSG:3857"
                transform, dst_w, dst_h = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )
                dst_array = np.zeros((4, dst_h, dst_w), dtype=bands.dtype)
                for band_index in range(1, 5):
                    reproject(
                        source=rasterio.band(src, band_index),
                        destination=dst_array[band_index - 1],
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.bilinear,
                    )

    return GeoreferencedOverlay(dst_array, transform)


def _resolution_meters_per_pixel(transform) -> float:
    return abs(transform.a)


def compute_zoom_range(overlays_3857: list[GeoreferencedOverlay]) -> tuple[int, int]:
    """Suy min/max zoom từ độ phân giải gốc (mét/pixel) — không phóng to
    quá độ chi tiết thật của ảnh nguồn."""
    best_resolution = min(_resolution_meters_per_pixel(o.transform_3857) for o in overlays_3857)
    tile_resolution_at_zoom0 = WEB_MERCATOR_EQUATOR_CIRCUMFERENCE / TILE_SIZE
    raw_zoom = math.log2(tile_resolution_at_zoom0 / best_resolution)
    max_zoom = max(DEFAULT_MIN_ZOOM_FLOOR, min(DEFAULT_MAX_ZOOM_CEIL, round(raw_zoom)))
    min_zoom = max(DEFAULT_MIN_ZOOM_FLOOR, max_zoom - 4)
    return min_zoom, max_zoom


def _render_tile_png(
    tile_coords: tuple[int, int, int],
    prepared_overlays: list[tuple[Image.Image, tuple[float, float, float, float]]],
) -> tuple[int, int, int, bytes] | None:
    """Render 1 tile XYZ (256x256 RGBA PNG) từ các overlay EPSG:3857 đã chuẩn bị.
    Trả về None nếu tile hoàn toàn trong suốt."""
    z, tx, ty = tile_coords
    tb = mercantile.xy_bounds(tx, ty, z)
    tw = tb.right - tb.left
    th = tb.top - tb.bottom

    composite_tile: Image.Image | None = None

    for pil_img, (src_xmin, src_ymin, src_xmax, src_ymax) in prepared_overlays:
        # Kiểm tra giao thoa bounding box trong EPSG:3857
        ix_min = max(src_xmin, tb.left)
        ix_max = min(src_xmax, tb.right)
        iy_min = max(src_ymin, tb.bottom)
        iy_max = min(src_ymax, tb.top)

        if ix_min >= ix_max or iy_min >= iy_max:
            continue

        # Tọa độ pixel đích trong tile 256x256
        dst_x0 = int(round((ix_min - tb.left) / tw * TILE_SIZE))
        dst_x1 = int(round((ix_max - tb.left) / tw * TILE_SIZE))
        dst_y0 = int(round((tb.top - iy_max) / th * TILE_SIZE))
        dst_y1 = int(round((tb.top - iy_min) / th * TILE_SIZE))

        # Tọa độ pixel nguồn trong ảnh overlay
        src_w, src_h = pil_img.size
        sw = src_xmax - src_xmin
        sh = src_ymax - src_ymin
        src_x0 = max(0, min(src_w, int(round((ix_min - src_xmin) / sw * src_w))))
        src_x1 = max(0, min(src_w, int(round((ix_max - src_xmin) / sw * src_w))))
        src_y0 = max(0, min(src_h, int(round((src_ymax - iy_max) / sh * src_h))))
        src_y1 = max(0, min(src_h, int(round((src_ymax - iy_min) / sh * src_h))))

        dst_w_box = dst_x1 - dst_x0
        dst_h_box = dst_y1 - dst_y0
        src_w_box = src_x1 - src_x0
        src_h_box = src_y1 - src_y0

        if dst_w_box <= 0 or dst_h_box <= 0 or src_w_box <= 0 or src_h_box <= 0:
            continue

        crop = pil_img.crop((src_x0, src_y0, src_x1, src_y1))
        if crop.getbbox() is None:
            continue

        resized = crop.resize((dst_w_box, dst_h_box), Image.BILINEAR)

        if composite_tile is None:
            composite_tile = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
            composite_tile.paste(resized, (dst_x0, dst_y0))
        else:
            layer = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
            layer.paste(resized, (dst_x0, dst_y0))
            composite_tile = Image.alpha_composite(composite_tile, layer)

    if composite_tile is None or composite_tile.getbbox() is None:
        return None

    buf = io.BytesIO()
    composite_tile.save(buf, format="PNG", compress_level=1, optimize=False)
    return z, tx, ty, buf.getvalue()


def generate_tiles(
    overlays_3857: list[GeoreferencedOverlay],
    bbox_wgs84: tuple[float, float, float, float],
    min_zoom: int,
    max_zoom: int,
    max_workers: int | None = None,
) -> Iterator[tuple[int, int, int, bytes]]:
    """Sinh tile XYZ siêu tốc đa luồng cho toàn bộ overlay EPSG:3857.
    Bỏ qua (không yield) tile hoàn toàn trong suốt."""
    west, south, east, north = bbox_wgs84

    # Chuẩn bị trước PIL Image và bounding box 3857 cho từng overlay
    prepared = []
    for overlay in overlays_3857:
        h, w = overlay.array_3857.shape[1], overlay.array_3857.shape[2]
        transform = overlay.transform_3857
        xmin = transform.c
        ymax = transform.f
        xmax = xmin + transform.a * w
        ymin = ymax + transform.e * h
        pil_img = Image.fromarray(np.transpose(overlay.array_3857, (1, 2, 0)), mode="RGBA")
        prepared.append((
            pil_img,
            (min(xmin, xmax), min(ymin, ymax), max(xmin, xmax), max(ymin, ymax)),
        ))

    all_tile_coords = []
    for z in range(min_zoom, max_zoom + 1):
        for t in mercantile.tiles(west, south, east, north, [z]):
            all_tile_coords.append((z, t.x, t.y))

    if not all_tile_coords:
        return

    if max_workers is None:
        cpu = os.cpu_count() or 4
        max_workers = min(16, cpu * 2)

    workers = max(1, min(max_workers, len(all_tile_coords)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for res in executor.map(lambda c: _render_tile_png(c, prepared), all_tile_coords, chunksize=32):
            if res is not None:
                yield res


def count_tiles(bbox_wgs84: tuple[float, float, float, float], min_zoom: int, max_zoom: int) -> int:
    west, south, east, north = bbox_wgs84
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        total += len(list(mercantile.tiles(west, south, east, north, [z])))
    return total

