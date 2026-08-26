"""Đọc file KMZ (MicroStation xuất từ DGN) — tìm GroundOverlay, lấy ảnh +
tọa độ WGS84 (LatLonBox hoặc gx:LatLonQuad). Không đoán/ép hình chữ nhật
nếu ảnh bị xoay — giữ đúng 4 góc thật để georeference chính xác.
"""

from __future__ import annotations

import posixpath
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

MAX_KMZ_BYTES = 300 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 500


class KmzError(Exception):
    pass


@dataclass
class GroundOverlayInfo:
    name: str
    image_bytes: bytes
    image_filename: str
    rotation: float
    # Trường hợp LatLonBox đơn giản (không xoay hoặc rotation != 0 nhưng
    # vẫn là hình chữ nhật xoay quanh tâm):
    west: float | None = None
    south: float | None = None
    east: float | None = None
    north: float | None = None
    # Trường hợp gx:LatLonQuad — 4 góc thật (lon, lat), thứ tự theo KML:
    # dưới-trái, dưới-phải, trên-phải, trên-trái. Có giá trị thì ƯU TIÊN
    # dùng thay cho west/south/east/north.
    quad_lonlat: list[tuple[float, float]] | None = None

    @property
    def is_quad(self) -> bool:
        return self.quad_lonlat is not None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_all(node: ET.Element, name: str):
    for child in node.iter():
        if _local_name(child.tag) == name:
            yield child


def _child(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if _local_name(child.tag) == name:
            return child
    return None


def _text(node: ET.Element | None, default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _safe_extract_path(name: str) -> str:
    """Chuẩn hóa path trong zip và chặn Zip Slip (path traversal)."""
    normalized = posixpath.normpath(name.replace("\\", "/"))
    if normalized.startswith("..") or normalized.startswith("/") or ":" in normalized:
        raise KmzError(f"File KMZ chứa đường dẫn không an toàn: {name!r}")
    return normalized


def _open_safe_zip(data: bytes) -> zipfile.ZipFile:
    if len(data) > MAX_KMZ_BYTES:
        raise KmzError(f"File KMZ vượt quá {MAX_KMZ_BYTES // (1024 * 1024)}MB")
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise KmzError(f"File không phải .kmz/.zip hợp lệ: {exc}") from exc

    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise KmzError(f"File KMZ có quá nhiều file bên trong ({len(infos)} > {MAX_MEMBERS})")

    total_uncompressed = 0
    for info in infos:
        _safe_extract_path(info.filename)
        total_uncompressed += info.file_size
    if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
        raise KmzError("File KMZ giải nén ra quá lớn — nghi ngờ zip bomb, từ chối xử lý")

    return archive


def _find_kml_member(archive: zipfile.ZipFile) -> str:
    names = [info.filename for info in archive.infolist() if info.filename.lower().endswith(".kml")]
    if not names:
        raise KmzError("Không tìm thấy file .kml bên trong KMZ")
    for name in names:
        if posixpath.basename(name).lower() == "doc.kml":
            return name
    return names[0]


def _resolve_image_member(archive: zipfile.ZipFile, kml_member: str, href: str) -> str:
    href = href.strip().replace("\\", "/")
    if href.lower().startswith(("http://", "https://")):
        raise KmzError(f"GroundOverlay dùng ảnh từ URL bên ngoài, không phải file trong KMZ: {href}")

    kml_dir = posixpath.dirname(kml_member)
    candidate = _safe_extract_path(posixpath.join(kml_dir, href) if kml_dir else href)

    names_lower = {info.filename.lower(): info.filename for info in archive.infolist()}
    if candidate.lower() in names_lower:
        return names_lower[candidate.lower()]

    # fallback: khớp theo tên file (bỏ qua thư mục) nếu đường dẫn tương đối
    # không khớp tuyệt đối (một số KMZ xuất từ phần mềm khác lồng thư mục
    # khác với dự kiến).
    basename = posixpath.basename(href).lower()
    for lower_name, real_name in names_lower.items():
        if posixpath.basename(lower_name) == basename:
            return real_name

    raise KmzError(f"Không tìm thấy ảnh {href!r} bên trong KMZ")


def _parse_lat_lon_box(node: ET.Element):
    def _float(name: str, default: float = 0.0) -> float:
        raw = _text(_child(node, name))
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    return {
        "north": _float("north"),
        "south": _float("south"),
        "east": _float("east"),
        "west": _float("west"),
        "rotation": _float("rotation", 0.0),
    }


def _parse_lat_lon_quad(node: ET.Element) -> list[tuple[float, float]]:
    coords_node = _child(node, "coordinates")
    raw = _text(coords_node)
    points = []
    for pair in raw.split():
        parts = pair.split(",")
        if len(parts) < 2:
            continue
        lon, lat = float(parts[0]), float(parts[1])
        points.append((lon, lat))
    if len(points) != 4:
        raise KmzError(f"gx:LatLonQuad phải có đúng 4 điểm, tìm thấy {len(points)}")
    return points


def read_kmz(data: bytes) -> list[GroundOverlayInfo]:
    """Đọc toàn bộ GroundOverlay trong 1 file KMZ (bytes). Trả về danh sách
    RỖNG nếu KMZ không có GroundOverlay nào (caller — processing.py — nên
    thử tiếp kml_vector_reader.read_kml_vector() cho trường hợp KMZ vector
    thuần, đã gặp thực tế: MicroStation xuất Placemark/Polygon/LineString
    không kèm ảnh). Vẫn ném KmzError cho lỗi thật (file hỏng/không an
    toàn/GroundOverlay thiếu tọa độ)."""
    archive = _open_safe_zip(data)
    kml_member = _find_kml_member(archive)

    try:
        kml_root = ET.fromstring(archive.read(kml_member))
    except ET.ParseError as exc:
        raise KmzError(f"File .kml trong KMZ không parse được: {exc}") from exc

    overlays: list[GroundOverlayInfo] = []
    for ground_overlay in _find_all(kml_root, "GroundOverlay"):
        name = _text(_child(ground_overlay, "name"), "GroundOverlay")

        icon = _child(ground_overlay, "Icon")
        href = _text(_child(icon, "href")) if icon is not None else ""
        if not href:
            continue

        image_member = _resolve_image_member(archive, kml_member, href)
        image_bytes = archive.read(image_member)

        quad_node = _child(ground_overlay, "LatLonQuad")
        lat_lon_box = _child(ground_overlay, "LatLonBox")

        if quad_node is not None:
            overlays.append(
                GroundOverlayInfo(
                    name=name,
                    image_bytes=image_bytes,
                    image_filename=posixpath.basename(image_member),
                    rotation=0.0,
                    quad_lonlat=_parse_lat_lon_quad(quad_node),
                )
            )
        elif lat_lon_box is not None:
            box = _parse_lat_lon_box(lat_lon_box)
            overlays.append(
                GroundOverlayInfo(
                    name=name,
                    image_bytes=image_bytes,
                    image_filename=posixpath.basename(image_member),
                    rotation=box["rotation"],
                    west=box["west"],
                    south=box["south"],
                    east=box["east"],
                    north=box["north"],
                )
            )
        else:
            raise KmzError(f"GroundOverlay {name!r} thiếu LatLonBox/gx:LatLonQuad")

    return overlays


def union_bbox(overlays: list[GroundOverlayInfo]):
    """Trả (west, south, east, north) hợp nhất tất cả overlay — dùng cho
    danh sách tile cần sinh và bbox lưu vào WebGIS."""
    lons: list[float] = []
    lats: list[float] = []
    for overlay in overlays:
        if overlay.is_quad:
            for lon, lat in overlay.quad_lonlat:
                lons.append(lon)
                lats.append(lat)
        else:
            lons.extend([overlay.west, overlay.east])
            lats.extend([overlay.south, overlay.north])
    return min(lons), min(lats), max(lons), max(lats)
