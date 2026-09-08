"""Bản Đồ Nền Pro — Desktop Tool (CustomTkinter)
Chuyển đổi KMZ / KML -> XYZ Tile -> Upload Supabase Storage -> Đăng ký WebGIS
Hoặc Xuất trực tiếp ra thư mục ổ đĩa máy tính (Local Tiles).
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import proj_setup  # Khắc phục xung đột PROJ_LIB / GDAL_DATA (PostgreSQL, PostGIS, QGIS)
import customtkinter as ctk
from PIL import Image

from config import ToolConfig, load_config
from processing import FileJob, ProcessOptions, infer_so_to_from_filename, process_one
from vn2000_crs import VN_PROVINCES_DATA, find_province_by_code

# Cấu hình giao diện CustomTkinter
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLUMNS = ("stt", "file", "ma_xa", "so_to", "status", "detail")
COLUMN_LABELS = {
    "stt": "STT",
    "file": "Tên File KMZ / KML / GeoTIFF",
    "ma_xa": "Mã xã",
    "so_to": "Số tờ",
    "status": "Trạng thái",
    "detail": "Chi tiết / Tiến độ",
}


def get_resource_path(relative_path: str) -> str:
    """Trả về đường dẫn tuyệt đối cho tài nguyên (hỗ trợ cả dev và PyInstaller --onefile)"""
    if getattr(sys, "frozen", False):
        base_path = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


class App(ctk.CTk):
    def __init__(self, cfg: Optional[ToolConfig]):
        super().__init__()
        self.title("BẢN ĐỒ NỀN PRO — KMZ / KML / GeoTIFF → XYZ Tile Converter")
        self.geometry("1140x750")
        self.minsize(1000, 640)

        self.cfg = cfg
        self.jobs: list[FileJob] = []
        self.row_id_by_job: dict[int, str] = {}
        self.update_queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_requested = False

        self.local_export_dir: str = ""

        # Set Icon
        self._set_app_icon()

        # Build UI
        self._build_header()
        self._build_control_card()
        self._build_table_section()
        self._build_edit_card()
        self._build_bottom_section()

        # Start queue polling
        self._poll_queue()

        if cfg is None:
            self.after(300, self._warn_missing_config)

    def _set_app_icon(self) -> None:
        ico_path = get_resource_path(os.path.join("assets", "icon.ico"))
        png_path = get_resource_path(os.path.join("assets", "icon.png"))

        if os.path.exists(ico_path):
            try:
                self.iconbitmap(default=ico_path)
            except Exception:
                try:
                    self.iconbitmap(ico_path)
                except Exception:
                    pass

        if os.path.exists(png_path):
            try:
                from PIL import ImageTk
                img = Image.open(png_path)
                self._icon_photo = ImageTk.PhotoImage(img)
                self.iconphoto(True, self._icon_photo)
            except Exception:
                pass

    # ---------------------------------------------------------------
    # UI Components
    # ---------------------------------------------------------------

    def _build_header(self) -> None:
        header_frame = ctk.CTkFrame(self, corner_radius=10, fg_color="#1a1c23")
        header_frame.pack(fill="x", padx=14, pady=(12, 6))

        left_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        left_box.pack(side="left", padx=12, pady=10)

        # App Logo / Icon
        png_path = get_resource_path(os.path.join("assets", "icon.png"))
        if os.path.exists(png_path):
            try:
                logo_img = Image.open(png_path)
                logo_ctk = ctk.CTkImage(light_image=logo_img, dark_image=logo_img, size=(42, 42))
                logo_lbl = ctk.CTkLabel(left_box, image=logo_ctk, text="")
                logo_lbl.pack(side="left", padx=(0, 10))
            except Exception:
                pass

        title_box = ctk.CTkFrame(left_box, fg_color="transparent")
        title_box.pack(side="left")

        title_lbl = ctk.CTkLabel(
            title_box,
            text="BẢN ĐỒ NỀN PRO",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color="#38bdf8",
        )
        title_lbl.pack(anchor="w")

        sub_lbl = ctk.CTkLabel(
            title_box,
            text="KMZ / KML → XYZ Tile Cutter & S3 Sync Engine",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color="#94a3b8",
        )
        sub_lbl.pack(anchor="w")

        # Right status badge
        right_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        right_box.pack(side="right", padx=14, pady=10)

        if self.cfg is not None:
            self.cfg_badge = ctk.CTkLabel(
                right_box,
                text=f"🟢 WebGIS & S3: Sẵn sàng ({self.cfg.s3_bucket})",
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                text_color="#34d399",
                fg_color="#064e3b",
                corner_radius=8,
                padx=12,
                pady=6,
            )
            self.cfg_badge.pack(side="left")
        else:
            self.cfg_badge = ctk.CTkLabel(
                right_box,
                text="🟡 Chưa nạp config.local.json (Chỉ xuất Local)",
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                text_color="#fbbf24",
                fg_color="#451a03",
                corner_radius=8,
                padx=12,
                pady=6,
            )
            self.cfg_badge.pack(side="left", padx=(0, 8))

            reload_btn = ctk.CTkButton(
                right_box,
                text="🔄 Nạp lại Config",
                width=110,
                height=30,
                font=ctk.CTkFont(size=11),
                command=self.on_reload_config,
            )
            reload_btn.pack(side="left")

    def _build_control_card(self) -> None:
        card = ctk.CTkFrame(self, corner_radius=10, fg_color="#1e2029")
        card.pack(fill="x", padx=14, pady=6)

        # Row 1: Parameters & Modes
        r1 = ctk.CTkFrame(card, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(10, 6))

        # Mã xã
        ctk.CTkLabel(r1, text="Mã xã mặc định:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 6))
        self.ma_xa_entry = ctk.CTkEntry(r1, placeholder_text="vd: 24169", width=95, height=32)
        self.ma_xa_entry.pack(side="left", padx=(0, 14))

        # Max Zoom
        ctk.CTkLabel(r1, text="Max Zoom:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 6))
        self.zoom_menu = ctk.CTkOptionMenu(
            r1,
            values=["Tự động", "18", "19", "20", "21", "22"],
            width=100,
            height=32,
            fg_color="#334155",
            button_color="#475569",
        )
        self.zoom_menu.set("Tự động")
        self.zoom_menu.pack(side="left", padx=(0, 14))

        # Luồng Upload
        ctk.CTkLabel(r1, text="Luồng upload:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 6))
        self.workers_menu = ctk.CTkOptionMenu(
            r1,
            values=["16 luồng", "32 luồng", "48 luồng", "64 luồng"],
            width=110,
            height=32,
            fg_color="#334155",
            button_color="#475569",
        )
        self.workers_menu.set("32 luồng")
        self.workers_menu.pack(side="left", padx=(0, 14))

        # Mode Selector
        ctk.CTkLabel(r1, text="Chế độ:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 6))
        self.mode_selector = ctk.CTkSegmentedButton(
            r1,
            values=["☁️ Cloud (S3 + WebGIS)", "💾 Xuất Ổ Đĩa (Local XYZ)"],
            command=self.on_mode_change,
            selected_color="#0284c7",
            selected_hover_color="#0369a1",
            height=32,
        )
        self.mode_selector.set("☁️ Cloud (S3 + WebGIS)")
        self.mode_selector.pack(side="left")

        # Row 2: Actions
        r2 = ctk.CTkFrame(card, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(4, 10))

        btn_files = ctk.CTkButton(
            r2,
            text="📁 + Chọn File KMZ / KML / TIF",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            height=32,
            command=self.on_choose_files,
        )
        btn_files.pack(side="left", padx=(0, 8))

        btn_folder = ctk.CTkButton(
            r2,
            text="📂 + Chọn Thư Mục",
            font=ctk.CTkFont(size=12),
            fg_color="#3b82f6",
            hover_color="#2563eb",
            height=32,
            command=self.on_choose_folder,
        )
        btn_folder.pack(side="left", padx=(0, 8))

        self.btn_export_dir = ctk.CTkButton(
            r2,
            text="💾 Chọn Thư Mục Xuất Local",
            font=ctk.CTkFont(size=12),
            fg_color="#475569",
            hover_color="#64748b",
            height=32,
            command=self.on_choose_export_folder,
        )
        self.btn_export_dir.pack(side="left", padx=(0, 8))
        self.btn_export_dir.configure(state="disabled")

        self.lbl_export_info = ctk.CTkLabel(
            r2,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#38bdf8",
        )
        self.lbl_export_info.pack(side="left", padx=(0, 8))

        btn_clear = ctk.CTkButton(
            r2,
            text="🧹 Xóa Danh Sách",
            font=ctk.CTkFont(size=12),
            fg_color="#991b1b",
            hover_color="#7f1d1d",
            height=32,
            width=120,
            command=self.on_clear,
        )
        btn_clear.pack(side="right")

        # Row 3: Tuỳ chọn Hệ toạ độ VN-2000 / CRS (cho file GeoTIFF .tif)
        r3 = ctk.CTkFrame(card, fg_color="transparent")
        r3.pack(fill="x", padx=12, pady=(0, 10))

        ctk.CTkLabel(
            r3,
            text="🗺️ Tỉnh / Kinh tuyến trục (KTT):",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=(0, 6))

        province_options = ["🎯 Tự động theo File / Mã xã"] + [
            f"{p[1]} — KTT {p[3]} ({p[4]})" for p in VN_PROVINCES_DATA
        ]
        self.province_menu = ctk.CTkOptionMenu(
            r3,
            values=province_options,
            command=self.on_province_change,
            width=240,
            height=32,
            fg_color="#334155",
            button_color="#475569",
        )
        self.province_menu.set("🎯 Tự động theo File / Mã xã")
        self.province_menu.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(
            r3,
            text="EPSG / KTT tuỳ chỉnh:",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=(0, 6))
        self.tif_crs_entry = ctk.CTkEntry(
            r3,
            placeholder_text="vd: 108.5 hoặc 9218",
            width=150,
            height=32,
        )
        self.tif_crs_entry.pack(side="left", padx=(0, 14))

        self.force_crs_var = tk.BooleanVar(value=True)
        self.force_crs_cb = ctk.CTkCheckBox(
            r3,
            text="Ép ghi đè CRS (khuyên dùng khi file CAD/TIF sai thẻ)",
            variable=self.force_crs_var,
            font=ctk.CTkFont(size=11),
            text_color="#38bdf8",
        )
        self.force_crs_cb.pack(side="left")

    def on_province_change(self, value: str) -> None:
        if value.startswith("🎯"):
            self.tif_crs_entry.delete(0, "end")
        else:
            prov_name = value.split("—")[0].strip()
            self.tif_crs_entry.delete(0, "end")
            self.tif_crs_entry.insert(0, prov_name)

    def _build_table_section(self) -> None:
        table_container = ctk.CTkFrame(self, corner_radius=10, fg_color="#181a20")
        table_container.pack(fill="both", expand=True, padx=14, pady=6)

        # Style ttk.Treeview to look ultra modern in dark theme
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Custom.Treeview",
            background="#121318",
            foreground="#f1f5f9",
            rowheight=30,
            fieldbackground="#121318",
            bordercolor="#27273a",
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.map(
            "Custom.Treeview",
            background=[("selected", "#0369a1")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Custom.Treeview.Heading",
            background="#1e2029",
            foreground="#38bdf8",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padding=6,
        )
        style.map(
            "Custom.Treeview.Heading",
            background=[("active", "#2a2d3d")],
            foreground=[("active", "#7dd3fc")],
        )

        tree_frame = tk.Frame(table_container, bg="#181a20")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=8)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=COLUMNS,
            show="headings",
            selectmode="browse",
            style="Custom.Treeview",
        )

        self.tree.heading("stt", text="#")
        self.tree.column("stt", width=45, anchor="center", stretch=False)

        self.tree.heading("file", text="Tên File")
        self.tree.column("file", width=220, anchor="w")

        self.tree.heading("ma_xa", text="Mã Xã")
        self.tree.column("ma_xa", width=90, anchor="center")

        self.tree.heading("so_to", text="Số Tờ")
        self.tree.column("so_to", width=80, anchor="center")

        self.tree.heading("status", text="Trạng Thái")
        self.tree.column("status", width=140, anchor="w")

        self.tree.heading("detail", text="Chi Tiết / Tiến Độ")
        self.tree.column("detail", width=360, anchor="w")

        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_row)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _build_edit_card(self) -> None:
        self.edit_card = ctk.CTkFrame(self, corner_radius=10, fg_color="#1e2029", height=48)
        self.edit_card.pack(fill="x", padx=14, pady=(2, 6))

        inner = ctk.CTkFrame(self.edit_card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=6)

        ctk.CTkLabel(
            inner,
            text="✏️ Sửa dòng đang chọn:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#cbd5e1",
        ).pack(side="left", padx=(0, 10))

        self.lbl_selected_file = ctk.CTkLabel(
            inner,
            text="(Chưa chọn file)",
            font=ctk.CTkFont(size=11, slant="italic"),
            text_color="#94a3b8",
            width=200,
            anchor="w",
        )
        self.lbl_selected_file.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(inner, text="Mã xã:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 4))
        self.edit_ma_xa_entry = ctk.CTkEntry(inner, width=100, height=28)
        self.edit_ma_xa_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(inner, text="Số tờ:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 4))
        self.edit_so_to_entry = ctk.CTkEntry(inner, width=80, height=28)
        self.edit_so_to_entry.pack(side="left", padx=(0, 12))

        btn_update = ctk.CTkButton(
            inner,
            text="💾 Lưu Sửa Đổi",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#059669",
            hover_color="#047857",
            height=28,
            width=110,
            command=self.on_update_row,
        )
        btn_update.pack(side="left")

    def _build_bottom_section(self) -> None:
        bottom_card = ctk.CTkFrame(self, corner_radius=10, fg_color="#1a1c23")
        bottom_card.pack(fill="x", padx=14, pady=(2, 12))

        # Row 1: Actions & Status text
        r1 = ctk.CTkFrame(bottom_card, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(10, 6))

        self.btn_check = ctk.CTkButton(
            r1,
            text="🔍 Kiểm Tra File",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#475569",
            hover_color="#334155",
            height=36,
            width=130,
            command=self.on_check_all,
        )
        self.btn_check.pack(side="left", padx=(0, 8))

        self.btn_run = ctk.CTkButton(
            r1,
            text="⚡ Bắt Đầu Xử Lý",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#059669",
            hover_color="#047857",
            height=36,
            width=150,
            command=self.on_run_all,
        )
        self.btn_run.pack(side="left", padx=(0, 8))

        self.btn_stop = ctk.CTkButton(
            r1,
            text="⏹️ Dừng",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#dc2626",
            hover_color="#b91c1c",
            height=36,
            width=90,
            state="disabled",
            command=self.on_stop,
        )
        self.btn_stop.pack(side="left", padx=(0, 16))

        self.lbl_progress_status = ctk.CTkLabel(
            r1,
            text="Trạng thái: Sẵn sàng",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e2e8f0",
        )
        self.lbl_progress_status.pack(side="left", padx=(0, 8))

        self.lbl_stats = ctk.CTkLabel(
            r1,
            text="Tổng: 0 file",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8",
        )
        self.lbl_stats.pack(side="right")

        # Row 2: Progress bar
        self.progress_bar = ctk.CTkProgressBar(
            bottom_card,
            height=10,
            progress_color="#0ea5e9",
            fg_color="#334155",
        )
        self.progress_bar.pack(fill="x", padx=12, pady=(0, 10))
        self.progress_bar.set(0.0)

    # ---------------------------------------------------------------
    # Event Handlers & Helpers
    # ---------------------------------------------------------------

    def _warn_missing_config(self) -> None:
        messagebox.showwarning(
            "Cảnh Báo Cấu Hình",
            "Chưa tìm thấy file config.local.json hợp lệ!\n\n"
            "• Chế độ Cloud (Upload S3 & Đăng ký WebGIS) sẽ không khả dụng.\n"
            "• Bạn vẫn có thể sử dụng chế độ 'Xuất Ổ Đĩa (Local XYZ)' để tạo tile ra máy tính.",
        )

    def on_reload_config(self) -> None:
        try:
            self.cfg = load_config()
            self.cfg_badge.configure(
                text=f"🟢 WebGIS & S3: Sẵn sàng ({self.cfg.s3_bucket})",
                text_color="#34d399",
                fg_color="#064e3b",
            )
            messagebox.showinfo("Thành Công", "Đã nạp thành công config.local.json!")
        except Exception as exc:
            messagebox.showerror("Lỗi Nạp Config", f"Không thể đọc config:\n{exc}")

    def on_mode_change(self, mode: str) -> None:
        if "Local" in mode:
            self.btn_export_dir.configure(state="normal", fg_color="#0284c7", hover_color="#0369a1")
            if not self.local_export_dir:
                self.lbl_export_info.configure(text="⚠️ Hãy chọn thư mục lưu tile")
            else:
                self.lbl_export_info.configure(text=f"📁 Thư mục: {os.path.basename(self.local_export_dir)}")
        else:
            self.btn_export_dir.configure(state="disabled", fg_color="#475569", hover_color="#64748b")
            self.lbl_export_info.configure(text="")

    def on_choose_export_folder(self) -> None:
        folder = filedialog.askdirectory(title="Chọn thư mục xuất XYZ tiles")
        if folder:
            self.local_export_dir = folder
            self.lbl_export_info.configure(text=f"📁 Thư mục: {os.path.basename(folder)}")

    def _add_file(self, path: str) -> None:
        filename = os.path.basename(path)
        default_ma_xa = self.ma_xa_entry.get().strip()
        job = FileJob(
            path=path,
            filename=filename,
            ma_xa=default_ma_xa,
            so_to=infer_so_to_from_filename(filename),
        )
        self.jobs.append(job)
        idx = len(self.jobs)
        row_id = self.tree.insert("", "end", values=self._row_values(job, idx))
        self.row_id_by_job[id(job)] = row_id
        self._update_stats()

    def _row_values(self, job: FileJob, idx: Optional[int] = None) -> tuple:
        if idx is None:
            try:
                idx = self.jobs.index(job) + 1
            except ValueError:
                idx = 1
        detail = job.error or job.message
        return (str(idx), job.filename, job.ma_xa, job.so_to, job.status, detail)

    def _refresh_row(self, job: FileJob) -> None:
        row_id = self.row_id_by_job.get(id(job))
        if row_id and self.tree.exists(row_id):
            self.tree.item(row_id, values=self._row_values(job))

    def _update_stats(self) -> None:
        total = len(self.jobs)
        completed = sum(1 for j in self.jobs if j.status == "Hoàn thành")
        errors = sum(1 for j in self.jobs if j.status == "Lỗi")
        self.lbl_stats.configure(text=f"Tổng: {total} file | ✅ {completed} | ❌ {errors}")

    def on_choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Chọn file KMZ / KML / GeoTIFF",
            filetypes=[
                ("Bản đồ nền (KMZ/KML/TIF)", "*.kmz *.kml *.tif *.tiff"),
                ("KMZ / KML", "*.kmz *.kml"),
                ("GeoTIFF", "*.tif *.tiff"),
                ("Tất cả", "*.*"),
            ],
        )
        for path in paths:
            self._add_file(path)

    def on_choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Chọn thư mục chứa file KMZ / KML / GeoTIFF")
        if not folder:
            return
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith((".kmz", ".kml", ".tif", ".tiff")):
                self._add_file(os.path.join(folder, name))

    def on_clear(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Đang Xử Lý", "Vui lòng dừng tiến trình trước khi xóa danh sách.")
            return
        self.jobs.clear()
        self.row_id_by_job.clear()
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        self.lbl_selected_file.configure(text="(Chưa chọn file)")
        self.edit_ma_xa_entry.delete(0, "end")
        self.edit_so_to_entry.delete(0, "end")
        self.progress_bar.set(0.0)
        self.lbl_progress_status.configure(text="Trạng thái: Sẵn sàng")
        self._update_stats()

    def on_select_row(self, _event=None) -> None:
        job = self._selected_job()
        if job:
            self.lbl_selected_file.configure(text=job.filename)
            self.edit_ma_xa_entry.delete(0, "end")
            self.edit_ma_xa_entry.insert(0, job.ma_xa)
            self.edit_so_to_entry.delete(0, "end")
            self.edit_so_to_entry.insert(0, job.so_to)

    def _selected_job(self) -> Optional[FileJob]:
        selection = self.tree.selection()
        if not selection:
            return None
        row_id = selection[0]
        index = self.tree.index(row_id)
        return self.jobs[index] if 0 <= index < len(self.jobs) else None

    def on_update_row(self) -> None:
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Chọn dòng", "Hãy chọn 1 file trong bảng để cập nhật.")
            return
        job.ma_xa = self.edit_ma_xa_entry.get().strip()
        job.so_to = self.edit_so_to_entry.get().strip()
        self._refresh_row(job)

    # ---------------------------------------------------------------
    # Processing Logic
    # ---------------------------------------------------------------

    def on_check_all(self) -> None:
        if not self.jobs:
            messagebox.showinfo("Chưa có file", "Hãy chọn ít nhất 1 file KMZ / KML / GeoTIFF để kiểm tra.")
            return

        import geotiff_reader
        import kml_vector_reader
        import kmz_reader
        from processing import is_geotiff_path

        tif_crs = self.tif_crs_entry.get().strip() or None
        force_crs = bool(self.force_crs_var.get())

        for job in self.jobs:
            try:
                with open(job.path, "rb") as handle:
                    data = handle.read()
                if is_geotiff_path(job.path):
                    bbox, desc = geotiff_reader.describe_geotiff(
                        data,
                        override_crs=tif_crs,
                        file_path=job.path,
                        force_override=force_crs,
                        ma_xa=job.ma_xa,
                    )
                    job.message = desc
                elif (overlays := kmz_reader.read_kmz(data)):
                    bbox = kmz_reader.union_bbox(overlays)
                    job.message = f"{len(overlays)} GroundOverlay (ảnh raster), bbox {tuple(round(v, 4) for v in bbox)}"
                else:
                    features = kml_vector_reader.read_kml_vector(data)
                    bbox = kml_vector_reader.union_bbox(features)
                    job.message = f"{len(features)} đối tượng vector KML, bbox {tuple(round(v, 4) for v in bbox)}"
                job.status = "Đã kiểm tra"
                job.error = ""
            except (kmz_reader.KmzError, geotiff_reader.GeoTiffError) as exc:
                job.status = "Lỗi"
                job.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                job.status = "Lỗi"
                job.error = f"Lỗi: {exc}"
            self._refresh_row(job)
        self._update_stats()

    def on_run_all(self) -> None:
        if not self.jobs:
            messagebox.showinfo("Chưa có file", "Hãy chọn ít nhất 1 file KMZ / KML / GeoTIFF trước.")
            return

        mode = self.mode_selector.get()
        is_local = "Local" in mode

        if is_local:
            if not self.local_export_dir or not os.path.exists(self.local_export_dir):
                messagebox.showwarning("Thư Mục Xuất", "Vui lòng bấm '💾 Chọn Thư Mục Xuất Local' trước khi bắt đầu!")
                return
        else:
            if self.cfg is None:
                self._warn_missing_config()
                return

        if self.worker_thread and self.worker_thread.is_alive():
            return

        self.stop_requested = False
        self.btn_run.configure(state="disabled")
        self.btn_check.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress_bar.set(0.0)

        self.worker_thread = threading.Thread(target=self._run_all_worker, daemon=True)
        self.worker_thread.start()

    def on_stop(self) -> None:
        self.stop_requested = True
        self.lbl_progress_status.configure(text="Đang dừng tiến trình...")

    def _run_all_worker(self) -> None:
        max_zoom_val = self.zoom_menu.get().strip()
        max_zoom = int(max_zoom_val) if max_zoom_val.isdigit() else None

        workers_val = self.workers_menu.get().split()[0]
        upload_workers = int(workers_val) if workers_val.isdigit() else 32

        mode = self.mode_selector.get()
        export_local_dir = self.local_export_dir if "Local" in mode else None

        tif_crs = self.tif_crs_entry.get().strip() or None
        force_crs = bool(self.force_crs_var.get())

        options = ProcessOptions(
            tile_version=1,
            max_zoom=max_zoom,
            upload_workers=upload_workers,
            export_local_dir=export_local_dir,
            geotiff_src_crs=tif_crs,
            geotiff_force_override=force_crs,
        )

        total_jobs = len(self.jobs)
        for index, job in enumerate(self.jobs):
            if self.stop_requested:
                break

            def _on_status(current_job: FileJob = job) -> None:
                self.update_queue.put(("row", current_job))

            process_one(self.cfg, job, options, on_status=_on_status)
            self.update_queue.put(("progress", index + 1, total_jobs))

        self.update_queue.put(("done", None))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.update_queue.get_nowait()
                kind = item[0]
                if kind == "row":
                    self._refresh_row(item[1])
                    self._update_stats()
                elif kind == "progress":
                    done, total = item[1], item[2]
                    fraction = done / total if total > 0 else 0.0
                    self.progress_bar.set(fraction)
                    self.lbl_progress_status.configure(text=f"Đang xử lý: {done}/{total} tờ ({fraction * 100:.0f}%)")
                    self._update_stats()
                elif kind == "done":
                    self.btn_run.configure(state="normal")
                    self.btn_check.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.lbl_progress_status.configure(text="Đã hoàn thành phiên xử lý!")
                    self._update_stats()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("bandonentool.pro.app")
        except Exception:
            pass

    try:
        cfg = load_config()
    except SystemExit as exc:
        print(exc)
        cfg = None
    app = App(cfg)
    app.mainloop()


if __name__ == "__main__":
    main()
