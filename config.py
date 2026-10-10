"""Đọc cấu hình cục bộ (config.local.json hoặc config.json).
Hỗ trợ cả chạy bằng mã nguồn Python lẫn chạy dưới dạng file thực thi (.exe PyInstaller).
File cấu hình luôn nằm cạnh file .exe / script.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


def get_base_dir() -> Path:
    """Trả về thư mục chứa file thực thi (.exe) nếu đóng gói, hoặc thư mục chứa script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def get_config_path() -> Path:
    """Tìm config.local.json hoặc config.json trong thư mục ứng dụng."""
    base = get_base_dir()
    for name in ("config.local.json", "config.json"):
        p = base / name
        if p.exists():
            return p
    return base / "config.local.json"


REQUIRED_KEYS = (
    "webgis_api_url",
    "import_token",
    "tile_public_base_url",
)


@dataclass
class ToolConfig:
    webgis_api_url: str
    import_token: str
    tile_public_base_url: str


def load_config() -> ToolConfig:
    config_file = get_config_path()
    if not config_file.exists():
        raise SystemExit(
            f"Không tìm thấy file cấu hình tại:\n{config_file}\n\n"
            "Hãy tạo file config.local.json hoặc config.json cạnh file .exe và điền thông tin trước khi chạy."
        )

    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{config_file.name} không phải JSON hợp lệ: {exc}") from exc

    missing = [key for key in REQUIRED_KEYS if not raw.get(key)]
    if missing:
        raise SystemExit(f"{config_file.name} thiếu các trường bắt buộc: {', '.join(missing)}")

    return ToolConfig(
        webgis_api_url=raw["webgis_api_url"].rstrip("/"),
        import_token=raw["import_token"],
        tile_public_base_url=raw["tile_public_base_url"].rstrip("/"),
    )


if __name__ == "__main__":
    try:
        cfg = load_config()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
    print("Config OK:", cfg.webgis_api_url, cfg.tile_public_base_url)

