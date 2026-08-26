"""Upload tile lên Supabase Storage bằng giao thức S3-compatible (khóa
Access Key/Secret Key RIÊNG của Storage — không phải Service Role Key của
DB) — upload song song đa luồng (32 luồng mặc định), có connection pool lớn,
retry tự động cho lỗi mạng.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from config import ToolConfig

DEFAULT_MAX_WORKERS = 32
MAX_RETRIES = 2


@dataclass
class UploadResult:
    total: int
    uploaded: int
    failed_keys: list[str]


def _make_client(cfg: ToolConfig, max_workers: int = DEFAULT_MAX_WORKERS):
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_endpoint,
        aws_access_key_id=cfg.s3_access_key_id,
        aws_secret_access_key=cfg.s3_secret_access_key,
        region_name=cfg.s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            max_pool_connections=max(64, max_workers + 16),
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _upload_one(client, bucket: str, key: str, data: bytes) -> None:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/png")
            return
        except (BotoCoreError, ClientError) as exc:
            last_error = exc
    raise RuntimeError(f"Upload {key} thất bại sau {MAX_RETRIES + 1} lần thử: {last_error}")


def upload_tiles(
    cfg: ToolConfig,
    key_prefix: str,
    tiles: Iterable[tuple[int, int, int, bytes]],
    max_workers: int = DEFAULT_MAX_WORKERS,
    on_progress: Callable[[int, int], None] | None = None,
) -> UploadResult:
    """`tiles` là danh sách/iterable (z, x, y, png_bytes).
    Upload song song với `max_workers` (mặc định 32).
    `on_progress(uploaded, total)` gọi sau mỗi tile để cập nhật UI."""
    tile_list = list(tiles)
    total = len(tile_list)
    if total == 0:
        return UploadResult(total=0, uploaded=0, failed_keys=[])

    workers = max(1, min(max_workers, total))
    client = _make_client(cfg, workers)
    uploaded = 0
    failed_keys: list[str] = []

    def _job(item):
        z, x, y, data = item
        key = f"{key_prefix}/{z}/{x}/{y}.png"
        _upload_one(client, cfg.s3_bucket, key, data)
        return key

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_job, item): item for item in tile_list}
        for future in as_completed(futures):
            uploaded += 1
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - ghi lại key lỗi, không dừng cả batch
                z, x, y, _ = futures[future]
                failed_keys.append(f"{key_prefix}/{z}/{x}/{y}.png ({exc})")
            if on_progress:
                on_progress(uploaded, total)

    return UploadResult(total=total, uploaded=total - len(failed_keys), failed_keys=failed_keys)

