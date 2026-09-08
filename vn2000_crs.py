# -*- coding: utf-8 -*-
"""Module hỗ trợ Hệ quy chiếu VN-2000 chuẩn 63 Tỉnh Thành Việt Nam.
Bao gồm 7 tham số chuyển đổi Datum sang WGS84 theo Quyết định 05/2007/QĐ-BTNMT.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Optional, Tuple

import proj_setup  # Khắc phục xung đột PROJ_LIB / GDAL_DATA (PostgreSQL, PostGIS, QGIS)
from rasterio.crs import CRS
from rasterio.transform import Affine

# 7 tham số chuyển đổi VN-2000 -> WGS84 toàn quốc (QĐ 05/2007/QĐ-BTNMT)
VN2000_TOWGS84 = "-191.90441429,-39.30318279,-111.45032835,-0.00928836,0.01975479,-0.00427372,0.252906278"

# Danh mục 63 Tỉnh Thành: (Mã TCTK, Tên Tỉnh, KTT float, KTT text, Mã EPSG múi 3°)
VN_PROVINCES_DATA = [
    ("01", "Hà Nội", 105.0, "105°00'", 9212),
    ("02", "Hà Giang", 105.5, "105°30'", 9198),
    ("04", "Cao Bằng", 105.75, "105°45'", 9199),
    ("06", "Bắc Kạn", 106.5, "106°30'", 9200),
    ("08", "Tuyên Quang", 106.0, "106°00'", 9200),
    ("10", "Lào Cai", 104.75, "104°45'", 9197),
    ("11", "Điện Biên", 103.0, "103°00'", 9201),
    ("12", "Lai Châu", 103.0, "103°00'", 9201),
    ("14", "Sơn La", 104.0, "104°00'", 9201),
    ("15", "Yên Bái", 104.75, "104°45'", 9197),
    ("17", "Hòa Bình", 106.0, "106°00'", 9202),
    ("19", "Thái Nguyên", 106.25, "106°15'", 9205),
    ("20", "Lạng Sơn", 107.25, "107°15'", 9204),
    ("22", "Quảng Ninh", 107.75, "107°45'", 9203),
    ("24", "Bắc Giang", 107.0, "107°00'", 9205),
    ("25", "Phú Thọ", 104.75, "104°45'", 9206),
    ("26", "Vĩnh Phúc", 105.0, "105°00'", 9207),
    ("27", "Bắc Ninh", 105.5, "105°30'", 9207),
    ("30", "Hải Dương", 105.75, "105°45'", 9209),
    ("31", "Hải Phòng", 105.75, "105°45'", 9208),
    ("33", "Hưng Yên", 105.5, "105°30'", 9210),
    ("34", "Thái Bình", 105.5, "105°30'", 9209),
    ("35", "Hà Nam", 105.0, "105°00'", 9211),
    ("36", "Nam Định", 105.5, "105°30'", 9211),
    ("37", "Ninh Bình", 105.0, "105°00'", 9211),
    ("38", "Thanh Hóa", 105.0, "105°00'", 9212),
    ("40", "Nghệ An", 104.75, "104°45'", 9214),
    ("42", "Hà Tĩnh", 105.5, "105°30'", 9213),
    ("44", "Quảng Bình", 106.0, "106°00'", 9213),
    ("45", "Quảng Trị", 106.25, "106°15'", 9213),
    ("46", "Thừa Thiên Huế", 107.0, "107°00'", 9219),
    ("48", "Đà Nẵng", 107.75, "107°45'", 9219),
    ("49", "Quảng Nam", 107.75, "107°45'", 9219),
    ("51", "Quảng Ngãi", 108.0, "108°00'", 9216),
    ("52", "Bình Định", 108.25, "108°15'", 9217),
    ("54", "Phú Yên", 108.5, "108°30'", 9218),
    ("56", "Khánh Hòa", 108.25, "108°15'", 9217),
    ("58", "Ninh Thuận", 108.25, "108°15'", 9217),
    ("60", "Bình Thuận", 107.75, "107°45'", 9220),
    ("62", "Kon Tum", 107.75, "107°45'", 9215),
    ("64", "Gia Lai", 108.25, "108°15'", 9216),
    ("66", "Đắk Lắk", 108.5, "108°30'", 9218),
    ("67", "Đắk Nông", 108.5, "108°30'", 9218),
    ("68", "Lâm Đồng", 107.75, "107°45'", 9226),
    ("70", "Bình Phước", 106.25, "106°15'", 9221),
    ("72", "Tây Ninh", 105.75, "105°45'", 9222),
    ("74", "Bình Dương", 105.75, "105°45'", 9222),
    ("75", "Đồng Nai", 107.75, "107°45'", 9223),
    ("77", "Bà Rịa - Vũng Tàu", 107.25, "107°15'", 9225),
    ("79", "TP. Hồ Chí Minh", 105.75, "105°45'", 9224),
    ("80", "Long An", 105.75, "105°45'", 9227),
    ("82", "Tiền Giang", 105.75, "105°45'", 9229),
    ("83", "Bến Tre", 106.0, "106°00'", 9229),
    ("84", "Trà Vinh", 105.75, "105°45'", 9231),
    ("86", "Vĩnh Long", 105.5, "105°30'", 9230),
    ("87", "Đồng Tháp", 105.0, "105°00'", 9230),
    ("89", "An Giang", 104.5, "104°30'", 9232),
    ("91", "Kiên Giang", 104.5, "104°30'", 9233),
    ("92", "Cần Thơ", 105.0, "105°00'", 9228),
    ("93", "Hậu Giang", 105.0, "105°00'", 9231),
    ("94", "Sóc Trăng", 105.5, "105°30'", 9234),
    ("95", "Bạc Liêu", 105.0, "105°00'", 9234),
    ("96", "Cà Mau", 104.5, "104°30'", 9235),
]


def strip_accents(s: str) -> str:
    """Loại bỏ dấu tiếng Việt để so sánh tìm kiếm dễ dàng."""
    s = s.replace("đ", "d").replace("Đ", "d")
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def build_vn2000_proj(lon_0: float, zone_3: bool = True) -> str:
    """Tạo chuỗi PROJ chuẩn VN-2000 kèm 7 tham số Datum chuyển đổi sang WGS84."""
    scale = 0.9999 if zone_3 else 0.9996
    return (
        f"+proj=tmerc +lat_0=0 +lon_0={lon_0} +k={scale} "
        f"+x_0=500000 +y_0=0 +ellps=WGS84 +towgs84={VN2000_TOWGS84} "
        f"+units=m +no_defs"
    )


def make_vn2000_crs(lon_0: float, zone_3: bool = True) -> CRS:
    return CRS.from_string(build_vn2000_proj(lon_0, zone_3))


def find_province_by_name(name_query: str) -> Optional[Tuple[str, str, float, str, int]]:
    """Tìm kiếm tỉnh theo tên có dấu hoặc không dấu."""
    q = strip_accents(name_query).replace("tp.", "").replace("tinh", "").strip()
    if not q:
        return None
    for item in VN_PROVINCES_DATA:
        p_name = strip_accents(item[1])
        if q == p_name or q in p_name or p_name in q:
            return item
    return None


def find_province_by_code(code: str) -> Optional[Tuple[str, str, float, str, int]]:
    """Tìm kiếm tỉnh theo mã 2 chữ số TCTK (hoặc lấy từ mã xã)."""
    clean_code = str(code).strip()
    code_2 = clean_code[:2] if len(clean_code) >= 2 else clean_code
    # Hỗ trợ mã xã Đắk Nông 24xxx (theo mã cũ)
    if clean_code.startswith("241") or clean_code.startswith("242") or clean_code.startswith("243") or clean_code.startswith("244"):
        for item in VN_PROVINCES_DATA:
            if item[0] == "67":  # Đắk Nông
                return item
    for item in VN_PROVINCES_DATA:
        if item[0] == code_2:
            return item
    return None


def parse_ktt_string(text: str) -> Optional[float]:
    """Phân tích các dạng nhập Kinh tuyến trục:
    - '108.5' -> 108.5
    - '108°30', '108-30', '108d30', '108 30' -> 108.5
    - '105°45', '105-45' -> 105.75
    - '108.25', '108°15' -> 108.25
    """
    text = text.strip()
    # Dạng số thập phân trực tiếp (vd: 108.5, 105.75, 106.25)
    try:
        val = float(text)
        if 100.0 <= val <= 115.0:
            return val
    except ValueError:
        pass

    # Dạng độ-phút (vd: 108-30, 108°30', 108d30, 108 30)
    match = re.search(r"(\d{3})[^\d]+(\d{1,2})", text)
    if match:
        deg = float(match.group(1))
        minute = float(match.group(2))
        return deg + minute / 60.0

    return None


def resolve_crs_input(raw: Optional[str]) -> Tuple[Optional[CRS], str]:
    """Phân giải chuỗi nhập từ người dùng hoặc cấu hình thành CRS chuẩn kèm mô tả."""
    if not raw or not str(raw).strip():
        return None, ""

    text = str(raw).strip()

    # 1. Tên tỉnh (vd: "Đắk Nông", "Hà Nội", "TPHCM")
    p_info = find_province_by_name(text)
    if p_info:
        crs = make_vn2000_crs(p_info[2], zone_3=True)
        return crs, f"VN-2000 {p_info[1]} (KTT {p_info[3]}, Múi 3°)"

    # 2. Kinh tuyến trục số (vd: 108.5, 108°30', 108-30)
    ktt = parse_ktt_string(text)
    if ktt is not None:
        crs = make_vn2000_crs(ktt, zone_3=True)
        matching = [p[1] for p in VN_PROVINCES_DATA if abs(p[2] - ktt) < 0.001]
        prov_hint = f" ({', '.join(matching[:2])})" if matching else ""
        return crs, f"VN-2000 KTT {ktt:.2f}°{prov_hint} (Múi 3°)"

    # 3. Mã EPSG số
    epsg_num = None
    if text.isdigit():
        epsg_num = int(text)
    elif text.upper().startswith("EPSG:") and text[5:].strip().isdigit():
        epsg_num = int(text[5:].strip())

    if epsg_num is not None:
        if epsg_num in (4326, 4756):
            return CRS.from_epsg(epsg_num), f"WGS84 / VN-2000 Địa lý (EPSG:{epsg_num})"
        if epsg_num in (3857, 900913):
            return CRS.from_epsg(3857), "Web Mercator (EPSG:3857)"
        if epsg_num in (5897, 3405):
            return make_vn2000_crs(105.0, zone_3=False), "VN-2000 Múi 6° KTT 105° (Zone 48N)"
        if epsg_num in (5898, 3406):
            return make_vn2000_crs(111.0, zone_3=False), "VN-2000 Múi 6° KTT 111° (Zone 49N)"
        if epsg_num == 9210:
            return make_vn2000_crs(108.0, zone_3=False), "VN-2000 Múi 6° KTT 108° (Toàn quốc)"

        for p in VN_PROVINCES_DATA:
            if p[4] == epsg_num:
                crs = make_vn2000_crs(p[2], zone_3=True)
                return crs, f"VN-2000 {p[1]} (KTT {p[3]}, EPSG:{epsg_num})"

        try:
            return CRS.from_epsg(epsg_num), f"EPSG:{epsg_num}"
        except Exception:
            pass

    # 4. PROJ / WKT
    try:
        crs = CRS.from_user_input(text)
        return crs, "Hệ toạ độ tuỳ chỉnh"
    except Exception as exc:
        raise ValueError(f"Không nhận diện được hệ toạ độ '{text}': {exc}")


def read_world_file_transform(file_path: str) -> Optional[Affine]:
    """Tìm và đọc file World (.tfw, .tifw, .wld) nếu có cùng thư mục."""
    if not file_path or not os.path.isfile(file_path):
        return None

    base, _ = os.path.splitext(file_path)
    candidates = [
        f"{base}.tfw",
        f"{base}.TFW",
        f"{base}.tifw",
        f"{base}.TIFW",
        f"{base}.wld",
        f"{base}.WLD",
    ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                if len(lines) >= 6:
                    a = float(lines[0])  # Pixel size X
                    d = float(lines[1])  # Rotation term 1
                    b = float(lines[2])  # Rotation term 2
                    e = float(lines[3])  # Pixel size Y (negative)
                    c = float(lines[4])  # Upper-left X
                    f = float(lines[5])  # Upper-left Y
                    return Affine(a, b, c, d, e, f)
            except Exception:
                pass
    return None
