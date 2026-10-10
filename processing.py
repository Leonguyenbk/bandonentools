"""Điều phối 1 file KMZ: đọc -> georeference/tile -> upload -> đăng ký
HOẶC xuất trực tiếp XYZ tiles ra thư mục ổ đĩa máy tính.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import geotiff_reader
import kml_vector_reader
import kmz_reader
import local_uploader
import raster_pipeline
import vector_rasterizer
import webgis_client
from config import ToolConfig

TIFF_EXTS = (".tif", ".tiff")

# Backend (ban_do_nen_service.py::_PATH_SEGMENT_RE) chỉ nhận ma_xa/so_to
# gồm chữ không dấu, số, gạch dưới, gạch ngang — dùng thẳng làm tên thư
# mục trên đĩa server. Số tờ lấy từ TÊN FILE (infer_so_to_from_filename)
# nên rất hay dính dấu tiếng Việt/khoảng trắng/ngoặc — trước đây gửi
# nguyên văn lên, bị backend từ chối (400) nhưng người dùng chỉ thấy dòng
# chung chung "Upload lỗi — chưa đăng ký", không biết lý do thật.
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_path_segment(value: str) -> str:
    """Bỏ dấu tiếng Việt, thay mọi ký tự không phải chữ/số/_/- bằng "_",
    gộp "_" liên tiếp, cắt bớt nếu dài hơn 64 ký tự — khớp đúng
    _PATH_SEGMENT_RE phía backend. Dùng cho số tờ (tự suy từ tên file nên
    hay lẫn dấu/khoảng trắng); KHÔNG dùng để tự sửa mã xã (mã hành chính
    chính thức, sai 1 ký tự là sai xã — phải báo lỗi rõ cho người dùng tự
    sửa, không tự đoán)."""
    # "đ"/"Đ" không có dạng phân rã NFKD về "d" (Unicode coi là chữ cái
    # riêng, không phải "d" + dấu) — phải thay tay trước khi NFKD, nếu
    # không sẽ bị loại bỏ thành "_" thay vì "d".
    value = value.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", value)
    no_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", no_diacritics).strip("_-")
    safe = re.sub(r"_{2,}", "_", safe)
    return safe[:64] or "1"


def is_valid_path_segment(value: str) -> bool:
    return bool(_PATH_SEGMENT_RE.match(value or ""))


def is_geotiff_path(path: str) -> bool:
    return path.lower().endswith(TIFF_EXTS)


def infer_so_to_from_filename(filename: str) -> str:
    """Số tờ = lấy đúng tên file (bỏ phần .kmz/.kml/.tif) — không tách suy
    ra số riêng như trước (dễ nhầm/lấy sai số khi tên file có nhiều số).
    Giữ nguyên cả tên để người vận hành tra lại đúng file gốc dễ dàng."""
    return filename.rsplit(".", 1)[0]


@dataclass
class FileJob:
    path: str
    filename: str
    ma_xa: str = ""
    so_to: str = ""
    ten_hien_thi: str = ""
    status: str = "Chờ"  # Chờ | Đang đọc KMZ | Đang tạo tile | Đang upload | Đang đăng ký | Hoàn thành | Lỗi
    message: str = ""
    tile_count: int = 0
    uploaded_count: int = 0
    error: str = ""


@dataclass
class ProcessOptions:
    tile_version: int = 1
    min_zoom: int | None = None
    max_zoom: int | None = None
    export_local_dir: str | None = None  # Nếu có giá trị -> xuất ra ổ đĩa máy tính, không upload lên WebGIS
    geotiff_src_crs: str | None = None  # Ghi đè CRS nguồn cho file .tif (vd "EPSG:9218" hoặc "Đắk Nông" hoặc "108.5")
    geotiff_force_override: bool = True  # Ép buộc áp dụng CRS đã chọn thay vì dùng metadata trong file TIF


def _export_tiles_to_disk(target_dir: str, tiles, on_progress=None) -> int:
    """Lưu danh sách tile (z, x, y, png_bytes) trực tiếp ra thư mục ổ đĩa theo cấu trúc {z}/{x}/{y}.png."""
    tile_list = list(tiles)
    total = len(tile_list)
    if total == 0:
        return 0

    saved = 0

    def _write_one(item):
        z, x, y, data = item
        col_dir = os.path.join(target_dir, str(z), str(x))
        os.makedirs(col_dir, exist_ok=True)
        with open(os.path.join(col_dir, f"{y}.png"), "wb") as f:
            f.write(data)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(_write_one, item): item for item in tile_list}
        for future in as_completed(futures):
            future.result()
            saved += 1
            if on_progress:
                on_progress(saved, total)

    return saved


def process_one(cfg: ToolConfig | None, job: FileJob, options: ProcessOptions, on_status=None) -> None:
    """Xử lý 1 FileJob, cập nhật job.status/message/error tại chỗ.
    on_status(job) gọi mỗi lần trạng thái đổi để cập nhật UI."""

    def _set(status: str, message: str = ""):
        job.status = status
        job.message = message
        if on_status:
            on_status(job)

    # Hỗ trợ số tờ linh hoạt: cả chuỗi (vd 1A, 01, 1-1, phu_02) lẫn số nguyên
    so_to_raw = (job.so_to or "").strip() or infer_so_to_from_filename(job.filename) or "1"
    # Số tờ tự suy ra từ TÊN FILE nên rất hay dính dấu tiếng Việt/khoảng
    # trắng/ngoặc (vd "Tờ 12 (chính).kmz") — backend chỉ nhận chữ không
    # dấu/số/_/- (dùng thẳng làm tên thư mục trên đĩa). Chuẩn hoá NGAY ở
    # đây và ghi lại vào job.so_to để người dùng THẤY giá trị thật sự sẽ
    # gửi đi (trước đây gửi nguyên văn, bị backend âm thầm từ chối 400 mà
    # tool chỉ báo chung chung "Upload lỗi — chưa đăng ký").
    so_to_safe = normalize_path_segment(so_to_raw)
    job.so_to = so_to_safe

    is_local_export = bool(options.export_local_dir)
    if not is_local_export:
        if not job.ma_xa.strip():
            job.error = "Thiếu mã xã"
            _set("Lỗi", job.error)
            return
        if not (job.so_to or "").strip():
            job.error = "Thiếu số tờ"
            _set("Lỗi", job.error)
            return
        # Mã xã là mã hành chính chính thức — KHÔNG tự sửa (sai 1 ký tự là
        # sai sang xã khác), chỉ báo lỗi rõ để người dùng tự kiểm tra lại.
        if not is_valid_path_segment(job.ma_xa.strip()):
            job.error = (
                f"Mã xã '{job.ma_xa.strip()}' chứa ký tự không hợp lệ "
                "(chỉ được chữ không dấu, số, gạch dưới, gạch ngang) — sửa lại ô Mã xã"
            )
            _set("Lỗi", job.error)
            return

    try:
        is_tiff = is_geotiff_path(job.path)
        _set("Đang đọc GeoTIFF" if is_tiff else "Đang đọc KMZ")
        with open(job.path, "rb") as handle:
            data = handle.read()

        if is_tiff:
            overlay_3857, bbox = geotiff_reader.read_geotiff_as_overlay(
                data,
                override_crs=options.geotiff_src_crs,
                file_path=job.path,
                force_override=options.geotiff_force_override,
                ma_xa=job.ma_xa,
            )
            overlays_3857 = [overlay_3857]
            auto_min_zoom, auto_max_zoom = raster_pipeline.compute_zoom_range(overlays_3857)
            _set("Đang tính zoom/tạo tile", "ảnh raster GeoTIFF độc lập")
        elif (overlays := kmz_reader.read_kmz(data)):
            bbox = kmz_reader.union_bbox(overlays)
            _set("Đang tính zoom/tạo tile", "ảnh có sẵn (GroundOverlay)")
            overlays_3857 = [raster_pipeline.georeference_and_reproject(o) for o in overlays]
            auto_min_zoom, auto_max_zoom = raster_pipeline.compute_zoom_range(overlays_3857)
        else:
            _set("Đang đọc vector KML")
            features = kml_vector_reader.read_kml_vector(data)
            bbox = kml_vector_reader.union_bbox(features)
            auto_min_zoom = vector_rasterizer.DEFAULT_MIN_ZOOM
            auto_max_zoom = vector_rasterizer.DEFAULT_MAX_ZOOM

            def _vector_progress(done, total):
                _set("Đang vẽ vector thành ảnh", f"{done}/{total} đối tượng")

            _set("Đang vẽ vector thành ảnh", f"0/{len(features)} đối tượng")
            target_render_zoom = options.max_zoom if options.max_zoom is not None else auto_max_zoom
            overlay = vector_rasterizer.rasterize_to_overlay(
                features, bbox, target_render_zoom, on_progress=_vector_progress
            )
            overlays_3857 = [overlay]

        min_zoom = options.min_zoom if options.min_zoom is not None else auto_min_zoom
        max_zoom = options.max_zoom if options.max_zoom is not None else auto_max_zoom

        west, south, east, north = bbox
        _set("Đang cắt tile (đa luồng)")
        t_gen_start = time.perf_counter()
        tiles = list(raster_pipeline.generate_tiles(overlays_3857, bbox, min_zoom, max_zoom))
        t_gen_elapsed = time.perf_counter() - t_gen_start
        job.tile_count = len(tiles)

        if not tiles:
            job.error = "Không sinh được tile nào (toàn bộ trong suốt?) — kiểm tra lại KMZ"
            _set("Lỗi", job.error)
            return

        # -------------------------------------------------------------
        # Chế độ 1: Xuất trực tiếp XYZ ra thư mục ổ đĩa máy tính (cực nhanh)
        # -------------------------------------------------------------
        if is_local_export:
            sub_folder_name = (
                f"{job.ma_xa.strip()}_{so_to_safe}" if job.ma_xa.strip() else f"to_{so_to_safe}"
            )
            out_folder = os.path.join(options.export_local_dir, sub_folder_name)
            os.makedirs(out_folder, exist_ok=True)

            t_write_start = time.perf_counter()

            def _save_prog(saved, total):
                _set("Đang ghi tile ra ổ đĩa", f"{saved}/{total} tile")

            saved_count = _export_tiles_to_disk(out_folder, tiles, on_progress=_save_prog)
            t_write_elapsed = time.perf_counter() - t_write_start
            total_elapsed = t_gen_elapsed + t_write_elapsed

            _set(
                "Hoàn thành",
                f"Đã xuất {saved_count} tile (z{min_zoom}-z{max_zoom}) vào {sub_folder_name} ({total_elapsed:.1f}s)",
            )
            return

        # -------------------------------------------------------------
        # Chế độ 2: Upload lên WebGIS backend (máy chủ tự lưu trữ) + Đăng ký
        # -------------------------------------------------------------
        if cfg is None:
            job.error = "Chưa có cấu hình kết nối WebGIS"
            _set("Lỗi", job.error)
            return

        _set("Đang upload", f"0/{len(tiles)} (Cắt xong {len(tiles)} tile trong {t_gen_elapsed:.1f}s)")
        key_prefix = f"{job.ma_xa.strip()}/{so_to_safe}/v{options.tile_version}"

        t_upload_start = time.perf_counter()

        def _progress(uploaded, total):
            job.uploaded_count = uploaded
            elapsed = max(0.01, time.perf_counter() - t_upload_start)
            speed = uploaded / elapsed
            _set("Đang upload", f"{uploaded}/{total} tile ({speed:.0f} tile/s)")

        result = local_uploader.upload_tiles(
            cfg,
            key_prefix,
            tiles,
            on_progress=_progress,
        )
        if result.failed_keys:
            # Hiện NGUYÊN VĂN lý do thật (failed_keys chứa thông điệp dễ hiểu
            # sẵn — vd "Upload thất bại (400): ma_xa/so_to chỉ được chứa
            # chữ, số..." hoặc "Lỗi kết nối: ...") — trước đây chỉ đếm số
            # tile lỗi, không cho biết lý do, người dùng không tự sửa được.
            ly_do = "; ".join(result.failed_keys[:3])
            job.error = f"Upload lỗi {len(result.failed_keys)}/{result.total} tile — chưa đăng ký. {ly_do}"
            _set("Lỗi", job.error)
            return

        t_total_upload = max(0.01, time.perf_counter() - t_upload_start)
        _set("Đang đăng ký", f"Đã upload {result.uploaded} tile ({t_total_upload:.1f}s)")
        tile_url = f"{cfg.tile_public_base_url}/{key_prefix}/{{z}}/{{x}}/{{y}}.png"
        geom = webgis_client.make_rectangle_geojson(west, south, east, north)
        # PHẢI dùng so_to_safe (đã chuẩn hoá, giống hệt giá trị trong
        # key_prefix/tile_url ở trên) — dùng nhầm so_to_raw ở đây sẽ khiến
        # số tờ lưu trong CSDL lệch với đường dẫn tile thật trên server.
        try:
            so_to_api: int | str = int(so_to_safe)
        except ValueError:
            so_to_api = so_to_safe

        webgis_client.register_sheet(
            cfg, job.ma_xa.strip(), so_to_api, geom, tile_url,
            options.tile_version, min_zoom, max_zoom,
            ghi_chu=job.ten_hien_thi.strip() or None,
        )

        total_time = t_gen_elapsed + t_total_upload
        _set("Hoàn thành", f"{result.uploaded} tile (z{min_zoom}-z{max_zoom} trong {total_time:.1f}s)")
    except (kmz_reader.KmzError, geotiff_reader.GeoTiffError) as exc:
        job.error = str(exc)
        _set("Lỗi", job.error)
    except webgis_client.RegisterError as exc:
        job.error = str(exc)
        _set("Lỗi", job.error)
    except Exception as exc:  # noqa: BLE001 - Tool chạy 1 mình, phải luôn báo lỗi rõ thay vì crash im lặng
        job.error = f"Lỗi không xác định: {exc}"
        _set("Lỗi", job.error)


