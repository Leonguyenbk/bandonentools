"""Gọi API WebGIS để đăng ký 1 tờ bản đồ nền — CHỈ gọi sau khi đã upload
xong 100% tile lên Storage.
"""

from __future__ import annotations

import requests

from config import ToolConfig


class RegisterError(Exception):
    pass


_GOI_Y_HTTP = {
    401: "Sai import_token trong config.local.json (phải trùng IMPORT_TOKEN trên backend).",
    403: "Sai import_token trong config.local.json (phải trùng IMPORT_TOKEN trên backend).",
    404: "Backend chưa có endpoint này — sai webgis_api_url hoặc backend chưa cập nhật bản mới.",
    413: "Gói upload vượt giới hạn của server/Cloudflare (100MB) hoặc nginx client_max_body_size.",
    502: "Server/proxy không phản hồi — backend đang tắt hoặc khởi động lại.",
    503: "Server tạm thời không phục vụ — thử lại sau.",
    504: "Server xử lý quá lâu, proxy cắt kết nối — giảm Max Zoom hoặc thử lại.",
}


def mo_ta_loi_ket_noi(exc: requests.RequestException, url: str) -> str:
    """Diễn giải lỗi requests (không tới được server) thành câu dễ hiểu + gợi ý sửa."""
    if isinstance(exc, requests.exceptions.SSLError):
        loai = "Lỗi chứng chỉ HTTPS (SSL) — kiểm tra lại webgis_api_url hoặc giờ hệ thống"
    elif isinstance(exc, requests.exceptions.ConnectTimeout):
        loai = "Hết thời gian chờ kết nối — server không phản hồi hoặc mạng chặn"
    elif isinstance(exc, requests.exceptions.ReadTimeout):
        loai = "Server nhận kết nối nhưng xử lý quá lâu không trả lời"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        loai = ("Không kết nối được tới server — kiểm tra mạng/Internet, "
                "webgis_api_url có đúng không, backend có đang chạy không")
    elif isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema)):
        loai = "webgis_api_url trong config sai định dạng (phải bắt đầu bằng https://)"
    else:
        loai = "Lỗi mạng"
    return f"Lỗi kết nối: {loai}. URL: {url} | Chi tiết: {type(exc).__name__}: {exc}"


def mo_ta_loi_http(response: requests.Response, hanh_dong: str) -> str:
    """Diễn giải phản hồi lỗi HTTP từ backend thành câu dễ hiểu + gợi ý sửa."""
    try:
        detail = response.json().get("error", response.text)
    except ValueError:
        detail = response.text
    detail = " ".join(str(detail).split())[:300]  # trang lỗi HTML có thể rất dài
    goi_y = _GOI_Y_HTTP.get(response.status_code)
    if goi_y is None and response.status_code >= 500:
        goi_y = "Lỗi phía server — gửi nguyên thông báo này cho người quản trị backend."
    msg = f"{hanh_dong} thất bại (HTTP {response.status_code}): {detail}"
    return f"{msg} → {goi_y}" if goi_y else msg


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
    ghi_chu: str | None = None,
) -> dict:
    url = f"{cfg.webgis_api_url}/api/ban-do-nen/register"
    try:
        response = requests.post(
            url,
            headers={"X-Import-Token": cfg.import_token, "Content-Type": "application/json"},
            json={
                "ma_xa": ma_xa,
                "so_to": so_to,
                "geom": geom_geojson,
                "tile_url": tile_url,
                "tile_version": tile_version,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
                "ghi_chu": ghi_chu,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RegisterError(
            "Đã upload tile xong nhưng ĐĂNG KÝ tờ lên WebGIS lỗi — "
            + mo_ta_loi_ket_noi(exc, url)
        ) from exc
    if not response.ok:
        raise RegisterError(
            "Đã upload tile xong nhưng " + mo_ta_loi_http(response, "Đăng ký tờ lên WebGIS")
        )
    return response.json()
