"""Tool desktop (Tkinter): chọn KMZ (1 file hoặc cả thư mục) -> đọc
GroundOverlay -> georeference -> EPSG:3857 -> XYZ tile -> upload Supabase
Storage -> đăng ký với WebGIS. Chạy xử lý trong thread nền, cập nhật UI
qua hàng đợi (queue) để không treo giao diện.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config import ToolConfig, load_config
from processing import FileJob, ProcessOptions, infer_so_to_from_filename, process_one

COLUMNS = ("file", "ma_xa", "so_to", "status", "detail")
COLUMN_LABELS = {
    "file": "File",
    "ma_xa": "Mã xã",
    "so_to": "Số tờ",
    "status": "Trạng thái",
    "detail": "Chi tiết",
}


class App(tk.Tk):
    def __init__(self, cfg: ToolConfig | None):
        super().__init__()
        self.title("Bản đồ nền — KMZ → XYZ Tool (Tối ưu hóa)")
        self.geometry("960x600")

        self.cfg = cfg
        self.jobs: list[FileJob] = []
        self.row_id_by_job: dict[int, str] = {}
        self.update_queue: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_requested = False

        self._build_widgets()
        self._poll_queue()

        if cfg is None:
            self.after(200, self._warn_missing_config)

    # ---------------------------------------------------------------
    # UI construction
    # ---------------------------------------------------------------

    def _build_widgets(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Mã xã mặc định:").pack(side="left")
        self.ma_xa_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.ma_xa_var, width=10).pack(side="left", padx=(4, 10))

        ttk.Label(top, text="Max Zoom:").pack(side="left")
        self.max_zoom_var = tk.StringVar(value="Tự động")
        zoom_combo = ttk.Combobox(
            top,
            textvariable=self.max_zoom_var,
            values=["Tự động", "18", "19", "20", "21"],
            width=9,
            state="readonly",
        )
        zoom_combo.pack(side="left", padx=(4, 10))

        ttk.Label(top, text="Luồng upload:").pack(side="left")
        self.workers_var = tk.StringVar(value="32 luồng")
        workers_combo = ttk.Combobox(
            top,
            textvariable=self.workers_var,
            values=["16 luồng", "32 luồng", "48 luồng", "64 luồng"],
            width=10,
            state="readonly",
        )
        workers_combo.pack(side="left", padx=(4, 12))

        ttk.Button(top, text="Chọn KMZ...", command=self.on_choose_files).pack(side="left", padx=3)
        ttk.Button(top, text="Chọn thư mục...", command=self.on_choose_folder).pack(side="left", padx=3)
        ttk.Button(top, text="Xóa danh sách", command=self.on_clear).pack(side="left", padx=3)

        table_frame = ttk.Frame(self, padding=(8, 0))
        table_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(table_frame, columns=COLUMNS, show="headings", selectmode="browse")
        for col in COLUMNS:
            self.tree.heading(col, text=COLUMN_LABELS[col])
            self.tree.column(col, width=150 if col != "file" else 220, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_row)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="left", fill="y")

        edit_frame = ttk.LabelFrame(self, text="Sửa dòng đang chọn", padding=8)
        edit_frame.pack(fill="x", padx=8, pady=(4, 0))

        ttk.Label(edit_frame, text="Mã xã:").grid(row=0, column=0, sticky="w")
        self.edit_ma_xa_var = tk.StringVar()
        ttk.Entry(edit_frame, textvariable=self.edit_ma_xa_var, width=12).grid(row=0, column=1, padx=4)

        ttk.Label(edit_frame, text="Số tờ:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.edit_so_to_var = tk.StringVar()
        ttk.Entry(edit_frame, textvariable=self.edit_so_to_var, width=10).grid(row=0, column=3, padx=4)

        ttk.Button(edit_frame, text="Cập nhật dòng", command=self.on_update_row).grid(row=0, column=4, padx=12)

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")

        self.check_button = ttk.Button(bottom, text="Kiểm tra tất cả", command=self.on_check_all)
        self.check_button.pack(side="left", padx=4)

        self.run_button = ttk.Button(bottom, text="Tạo XYZ + Upload", command=self.on_run_all)
        self.run_button.pack(side="left", padx=4)

        self.stop_button = ttk.Button(bottom, text="Dừng", command=self.on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)

        self.overall_progress_var = tk.StringVar(value="Chưa xử lý")
        ttk.Label(bottom, textvariable=self.overall_progress_var).pack(side="left", padx=12)

        self.overall_progress = ttk.Progressbar(self, mode="determinate")
        self.overall_progress.pack(fill="x", padx=8, pady=(0, 8))

    def _warn_missing_config(self) -> None:
        messagebox.showwarning(
            "Thiếu cấu hình",
            "Chưa có config.local.json hợp lệ — sao chép config.example.json thành "
            "config.local.json rồi điền thông tin thật (xem README.md), sau đó mở lại Tool.",
        )

    # ---------------------------------------------------------------
    # File selection
    # ---------------------------------------------------------------

    def _add_file(self, path: str) -> None:
        filename = os.path.basename(path)
        job = FileJob(
            path=path,
            filename=filename,
            ma_xa=self.ma_xa_var.get().strip(),
            so_to=infer_so_to_from_filename(filename),
        )
        self.jobs.append(job)
        row_id = self.tree.insert("", "end", values=self._row_values(job))
        self.row_id_by_job[id(job)] = row_id

    def _row_values(self, job: FileJob) -> tuple:
        detail = job.error or job.message
        return (job.filename, job.ma_xa, job.so_to, job.status, detail)

    def _refresh_row(self, job: FileJob) -> None:
        row_id = self.row_id_by_job.get(id(job))
        if row_id and self.tree.exists(row_id):
            self.tree.item(row_id, values=self._row_values(job))

    def on_choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Chọn file KMZ", filetypes=[("KMZ", "*.kmz")])
        for path in paths:
            self._add_file(path)

    def on_choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Chọn thư mục chứa KMZ")
        if not folder:
            return
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".kmz"):
                self._add_file(os.path.join(folder, name))

    def on_clear(self) -> None:
        self.jobs.clear()
        self.row_id_by_job.clear()
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)

    def on_select_row(self, _event=None) -> None:
        job = self._selected_job()
        if job:
            self.edit_ma_xa_var.set(job.ma_xa)
            self.edit_so_to_var.set(job.so_to)

    def _selected_job(self) -> FileJob | None:
        selection = self.tree.selection()
        if not selection:
            return None
        row_id = selection[0]
        index = self.tree.index(row_id)
        return self.jobs[index] if 0 <= index < len(self.jobs) else None

    def on_update_row(self) -> None:
        job = self._selected_job()
        if not job:
            return
        job.ma_xa = self.edit_ma_xa_var.get().strip()
        job.so_to = self.edit_so_to_var.get().strip()
        self._refresh_row(job)

    # ---------------------------------------------------------------
    # Processing
    # ---------------------------------------------------------------

    def on_check_all(self) -> None:
        import kml_vector_reader
        import kmz_reader

        for job in self.jobs:
            try:
                with open(job.path, "rb") as handle:
                    data = handle.read()
                overlays = kmz_reader.read_kmz(data)
                if overlays:
                    bbox = kmz_reader.union_bbox(overlays)
                    job.message = f"{len(overlays)} GroundOverlay (ảnh có sẵn), bbox {tuple(round(v, 5) for v in bbox)}"
                else:
                    features = kml_vector_reader.read_kml_vector(data)
                    bbox = kml_vector_reader.union_bbox(features)
                    job.message = f"{len(features)} đối tượng vector (sẽ tự vẽ thành ảnh), bbox {tuple(round(v, 5) for v in bbox)}"
                job.status = "Đã kiểm tra"
                job.error = ""
            except kmz_reader.KmzError as exc:
                job.status = "Lỗi"
                job.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                job.status = "Lỗi"
                job.error = f"Lỗi không xác định: {exc}"
            self._refresh_row(job)

    def on_run_all(self) -> None:
        """Tạo XYZ + Upload lên Supabase Storage."""
        if self.cfg is None:
            self._warn_missing_config()
            return
        if not self.jobs:
            messagebox.showinfo("Không có file", "Hãy chọn ít nhất 1 file KMZ trước.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            return

        self.stop_requested = False
        self.run_button.config(state="disabled")
        self.check_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.overall_progress.config(maximum=len(self.jobs), value=0)

        self.worker_thread = threading.Thread(target=self._run_all_worker, daemon=True)
        self.worker_thread.start()

    def on_stop(self) -> None:
        self.stop_requested = True

    def _run_all_worker(self) -> None:
        max_zoom_val = self.max_zoom_var.get().strip()
        max_zoom = int(max_zoom_val) if max_zoom_val.isdigit() else None

        workers_val = self.workers_var.get().split()[0]
        upload_workers = int(workers_val) if workers_val.isdigit() else 32

        options = ProcessOptions(
            tile_version=1,
            max_zoom=max_zoom,
            upload_workers=upload_workers,
        )
        for index, job in enumerate(self.jobs):
            if self.stop_requested:
                break

            def _on_status(current_job: FileJob = job) -> None:
                self.update_queue.put(("row", current_job))

            process_one(self.cfg, job, options, on_status=_on_status)
            self.update_queue.put(("progress", index + 1, len(self.jobs)))

        self.update_queue.put(("done", None))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.update_queue.get_nowait()
                kind = item[0]
                if kind == "row":
                    self._refresh_row(item[1])
                elif kind == "progress":
                    done, total = item[1], item[2]
                    self.overall_progress.config(value=done)
                    self.overall_progress_var.set(f"Đã xử lý {done} / {total} tờ")
                elif kind == "done":
                    self.run_button.config(state="normal")
                    self.check_button.config(state="normal")
                    self.stop_button.config(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)



def main() -> None:
    try:
        cfg = load_config()
    except SystemExit as exc:
        print(exc)
        cfg = None
    app = App(cfg)
    app.mainloop()


if __name__ == "__main__":
    main()

