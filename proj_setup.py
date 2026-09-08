# -*- coding: utf-8 -*-
"""Tự động phát hiện và cấu hình môi trường PROJ & GDAL cho Bản Đồ Nền Pro.

Khắc phục triệt để lỗi:
'The EPSG code is unknown. PROJ: proj_create_from_database: .../proj.db contains DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 5 is expected'
xảy ra khi máy tính có cài đặt PostgreSQL / PostGIS, QGIS, ArcGIS hoặc OSGeo4W
dẫn đến xung đột biến môi trường PROJ_LIB / PROJ_DATA / GDAL_DATA toàn cục trên Windows.
"""

from __future__ import annotations

import os
import sys


def setup_proj_gdal_env() -> None:
    """Định cấu hình PROJ_DATA, PROJ_LIB và GDAL_DATA luôn trỏ về thư mục nội bộ
    của rasterio, ghi đè hoặc loại bỏ mọi biến môi trường toàn cục ngoài hệ điều hành.
    """
    proj_dir = None
    gdal_dir = None

    # 1. Nếu đang chạy từ file exe đóng gói bởi PyInstaller (onefile / onedir)
    if getattr(sys, "frozen", False):
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))

        candidate_proj = os.path.join(base_dir, "rasterio", "proj_data")
        if os.path.isdir(candidate_proj):
            proj_dir = candidate_proj

        candidate_gdal = os.path.join(base_dir, "rasterio", "gdal_data")
        if os.path.isdir(candidate_gdal):
            gdal_dir = candidate_gdal

    # 2. Nếu đang chạy trong môi trường Python dev (virtualenv hoặc global)
    if not proj_dir:
        try:
            import importlib.util

            spec = importlib.util.find_spec("rasterio")
            if spec and spec.submodule_search_locations:
                pkg_dir = list(spec.submodule_search_locations)[0]

                candidate_proj = os.path.join(pkg_dir, "proj_data")
                if os.path.isdir(candidate_proj):
                    proj_dir = candidate_proj

                candidate_gdal = os.path.join(pkg_dir, "gdal_data")
                if os.path.isdir(candidate_gdal):
                    gdal_dir = candidate_gdal
        except Exception:
            pass

    # Áp dụng cho PROJ
    if proj_dir:
        os.environ["PROJ_DATA"] = proj_dir
        os.environ["PROJ_LIB"] = proj_dir
    else:
        # Nếu không tìm thấy thư mục cụ thể, xoá biến xung đột từ PostgreSQL / PostGIS ngoài hệ thống
        for var in ("PROJ_DATA", "PROJ_LIB"):
            val = os.environ.get(var, "")
            if "postgres" in val.lower() or "postgis" in val.lower() or not os.path.exists(val):
                os.environ.pop(var, None)

    # Áp dụng cho GDAL
    if gdal_dir:
        os.environ["GDAL_DATA"] = gdal_dir
    else:
        val = os.environ.get("GDAL_DATA", "")
        if "postgres" in val.lower() or "postgis" in val.lower() or not os.path.exists(val):
            os.environ.pop("GDAL_DATA", None)

    # Đặt trực tiếp vào rasterio runtime nếu có thể
    try:
        from rasterio._env import set_proj_data_search_path

        if proj_dir:
            set_proj_data_search_path(proj_dir)
    except Exception:
        pass


# Tự động thực thi ngay khi file này được import
setup_proj_gdal_env()
