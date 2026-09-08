"""Gọi API WebGIS để đăng ký 1 tờ bản đồ nền — CHỈ gọi sau khi đã upload
xong 100% tile lên Storage.
"""

from __future__ import annotations

import requests

from config import ToolConfig


class RegisterError(Exception):
    pass


def make_rectangle_geojson(west: float, south: float, east: float, north: float) -> dict:
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}


def register_sheet(
    cfg: ToolConfig,
    ma_xa: str,
    so_to: int | str,
    geom_geojson: dict,
    tile_url: str,
    tile_version: int,
    min_zoom: int,
    max_zoom: int,
) -> dict:
    response = requests.post(
        f"{cfg.webgis_api_url}/api/ban-do-nen/register",
        headers={"X-Import-Token": cfg.import_token, "Content-Type": "application/json"},
        json={
            "ma_xa": ma_xa,
            "so_to": so_to,
            "geom": geom_geojson,
            "tile_url": tile_url,
            "tile_version": tile_version,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
        },
        timeout=30,
    )
    if not response.ok:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise RegisterError(f"Đăng ký thất bại ({response.status_code}): {detail}")
    return response.json()
