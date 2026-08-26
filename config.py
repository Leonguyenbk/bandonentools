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
    "s3_endpoint",
    "s3_access_key_id",
    "s3_secret_access_key",
    "s3_bucket",
)


@dataclass
class ToolConfig:
    webgis_api_url: str
    import_token: str
    s3_endpoint: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket: str
    s3_region: str = "auto"
    tile_public_base_url: str = ""


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
        s3_endpoint=raw["s3_endpoint"],
        s3_access_key_id=raw["s3_access_key_id"],
        s3_secret_access_key=raw["s3_secret_access_key"],
        s3_bucket=raw["s3_bucket"],
        s3_region=raw.get("s3_region", "auto"),
        tile_public_base_url=raw.get("tile_public_base_url", ""),
    )


if __name__ == "__main__":
    try:
        cfg = load_config()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
    print("Config OK:", cfg.webgis_api_url, cfg.s3_bucket)

