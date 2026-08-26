"""Đọc phần VECTOR (Placemark/Polygon/LineString/Point + Style màu/nét)
trong KMZ khi file KHÔNG có GroundOverlay raster sẵn — trường hợp
MicroStation xuất thẳng đường nét/vùng dạng vector (đã gặp thực tế: KMZ
chỉ có <Style>/<Placemark><MultiGeometry>, không có ảnh nào bên trong).
Dùng để tự rasterize thành ảnh trước khi cắt tile XYZ — xem
vector_rasterizer.py.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from kmz_reader import KmzError, _find_kml_member, _local_name, _open_safe_zip


@dataclass
class VectorStyle:
    line_color: tuple[int, int, int, int] = (230, 30, 30, 255)  # RGBA, mặc định nếu KMZ không khai báo
    line_width: float = 1.0
    fill_color: tuple[int, int, int, int] | None = None
    has_outline: bool = True


@dataclass
class VectorFeature:
    kind: str  # "Polygon" | "LineString" | "Point"
    rings: list[list[tuple[float, float]]] = field(default_factory=list)
    style: VectorStyle = field(default_factory=VectorStyle)
    label: str = ""


def _parse_kml_color(raw: str | None) -> tuple[int, int, int, int]:
    # KML color: aabbggrr (khác thứ tự RGB thường gặp).
    raw = (raw or "").strip()
    if len(raw) != 8:
        return (230, 30, 30, 255)
    try:
        aa, bb, gg, rr = raw[0:2], raw[2:4], raw[4:6], raw[6:8]
        return (int(rr, 16), int(gg, 16), int(bb, 16), int(aa, 16))
    except ValueError:
        return (230, 30, 30, 255)


def _find_child(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if _local_name(child.tag) == name:
            return child
    return None


def _parse_styles(root: ET.Element) -> dict[str, VectorStyle]:
    styles: dict[str, VectorStyle] = {}
    for style_node in root.iter():
        if _local_name(style_node.tag) != "Style":
            continue
        style_id = style_node.get("id")
        if not style_id:
            continue

        style = VectorStyle()
        line_style = _find_child(style_node, "LineStyle")
        if line_style is not None:
            color_node = _find_child(line_style, "color")
            if color_node is not None:
                style.line_color = _parse_kml_color(color_node.text)
            width_node = _find_child(line_style, "width")
            if width_node is not None and width_node.text:
                try:
                    style.line_width = float(width_node.text)
                except ValueError:
                    pass

        poly_style = _find_child(style_node, "PolyStyle")
        if poly_style is not None:
            fill_on = True
            outline_on = True
            color = None
            color_node = _find_child(poly_style, "color")
            if color_node is not None:
                color = _parse_kml_color(color_node.text)
            fill_node = _find_child(poly_style, "fill")
            if fill_node is not None and (fill_node.text or "").strip() == "0":
                fill_on = False
            outline_node = _find_child(poly_style, "outline")
            if outline_node is not None and (outline_node.text or "").strip() == "0":
                outline_on = False
            style.has_outline = outline_on
            if fill_on and color:
                style.fill_color = color

        styles[f"#{style_id}"] = style

    return styles


def _parse_coords(text: str | None) -> list[tuple[float, float]]:
    points = []
    for pair in (text or "").split():
        parts = pair.split(",")
        if len(parts) >= 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return points


def _iter_geometries(node: ET.Element, style: VectorStyle, label: str, out: list[VectorFeature]) -> None:
    name = _local_name(node.tag)

    if name == "Polygon":
        rings: list[list[tuple[float, float]]] = []
        for child in node:
            child_name = _local_name(child.tag)
            if child_name not in ("outerBoundaryIs", "innerBoundaryIs"):
                continue
            linear_ring = _find_child(child, "LinearRing")
            if linear_ring is None:
                continue
            coords_node = _find_child(linear_ring, "coordinates")
            if coords_node is None:
                continue
            ring = _parse_coords(coords_node.text)
            if len(ring) < 3:
                continue
            if child_name == "outerBoundaryIs":
                rings.insert(0, ring)
            else:
                rings.append(ring)
        if rings:
            out.append(VectorFeature(kind="Polygon", rings=rings, style=style, label=label))

    elif name == "LineString":
        coords_node = _find_child(node, "coordinates")
        if coords_node is not None:
            points = _parse_coords(coords_node.text)
            if len(points) >= 2:
                out.append(VectorFeature(kind="LineString", rings=[points], style=style, label=label))

    elif name == "Point":
        coords_node = _find_child(node, "coordinates")
        if coords_node is not None:
            points = _parse_coords(coords_node.text)
            if points:
                out.append(VectorFeature(kind="Point", rings=[points], style=style, label=label))

    elif name in ("MultiGeometry", "Folder", "Document"):
        for child in node:
            _iter_geometries(child, style, label, out)


def read_kml_vector(data: bytes) -> list[VectorFeature]:
    """Đọc toàn bộ Placemark dạng vector trong 1 KMZ. Ném KmzError nếu file
    hỏng hoặc không có dữ liệu vector nào (caller nên thử read_kmz() —
    đường GroundOverlay — trước, chỉ gọi hàm này khi đó trả về rỗng)."""
    archive = _open_safe_zip(data)
    kml_member = _find_kml_member(archive)

    try:
        root = ET.fromstring(archive.read(kml_member))
    except ET.ParseError as exc:
        raise KmzError(f"File .kml trong KMZ không parse được: {exc}") from exc

    styles = _parse_styles(root)
    default_style = VectorStyle()

    features: list[VectorFeature] = []
    for placemark in root.iter():
        if _local_name(placemark.tag) != "Placemark":
            continue

        style_url = None
        label = ""
        for child in placemark:
            child_name = _local_name(child.tag)
            if child_name == "styleUrl":
                style_url = (child.text or "").strip()
            elif child_name == "name":
                label = (child.text or "").strip()

        style = styles.get(style_url, default_style) if style_url else default_style

        for child in placemark:
            if _local_name(child.tag) in ("Polygon", "LineString", "Point", "MultiGeometry"):
                _iter_geometries(child, style, label, features)

    if not features:
        raise KmzError(
            "KMZ này không có GroundOverlay raster lẫn dữ liệu vector "
            "(Placemark/LineString/Polygon/Point) — không rõ nội dung để xử lý."
        )

    return features


def union_bbox(features: list[VectorFeature]) -> tuple[float, float, float, float]:
    min_lon = float("inf")
    min_lat = float("inf")
    max_lon = float("-inf")
    max_lat = float("-inf")
    found = False

    for feature in features:
        for ring in feature.rings:
            for lon, lat in ring:
                found = True
                if lon < min_lon:
                    min_lon = lon
                if lon > max_lon:
                    max_lon = lon
                if lat < min_lat:
                    min_lat = lat
                if lat > max_lat:
                    max_lat = lat

    if not found:
        return 0.0, 0.0, 0.0, 0.0
    return min_lon, min_lat, max_lon, max_lat

