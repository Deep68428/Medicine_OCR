from __future__ import annotations

import asyncio
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import urllib3
import cv2
import numpy as np
from loguru import logger
from minio import Minio
from typing import Optional

from core.config import get_config

_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})_")


class ImageStore:
    """Saves debug images to MinIO with transparent fallback to local disk.

    MinIO key format:  {machine_id}/{dd-mm-yy}/{subdir}/{filename}
    Local fallback:    DEBUG_IMAGE_PATH/{subdir}/{filename}
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._local_base = Path(cfg.DEBUG_IMAGE_PATH).expanduser()
        self._machine_id = cfg.MACHINE_ID
        self._bucket = cfg.MINIO_BUCKET
        self._client: Minio | None = None
        self.cfg = cfg
        # Dedicated pool — never shared with OpenVINO's inference threads.
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="imgstore"
        )

        if cfg.MINIO_ENDPOINT:
            try:
                http_client = urllib3.PoolManager(
                    timeout=urllib3.Timeout(connect=5, read=30, total=60),
                    retries=urllib3.Retry(total=1, backoff_factor=0.5),
                )
                client = Minio(
                    cfg.MINIO_ENDPOINT,
                    access_key=cfg.MINIO_ACCESS_KEY,
                    secret_key=cfg.MINIO_SECRET_KEY,
                    secure=cfg.MINIO_SECURE,
                    http_client=http_client,
                )
                self._ensure_bucket(client)
                self._client = client
                logger.info("MinIO image store connected to {}", cfg.MINIO_ENDPOINT)
            except Exception:
                logger.warning("MinIO connection failed — images will be saved locally")

    def _ensure_bucket(self, client: Minio) -> None:
        if not client.bucket_exists(self._bucket):
            client.make_bucket(self._bucket)
            logger.info("Created MinIO bucket '{}'", self._bucket)

    def save(
        self,
        subdir: str,
        img: np.ndarray,
        prefix: str = "",
        ext: str = "jpg",
        picknote: str = "",
        stage: str = "",
    ) -> str:
        """Save *img* to MinIO (or local on failure). Returns the bare filename."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"{prefix}{ts}.{ext}" if prefix else f"{ts}.{ext}"

        if self._client is not None:
            try:
                date_str = datetime.now().strftime("%d-%m-%y")
                key = f"{self._machine_id}/{date_str}/{subdir}/{name}"
                ok, buf = cv2.imencode(f".{ext}", img)
                if not ok:
                    raise RuntimeError("cv2.imencode failed")
                raw = buf.tobytes()
                self._client.put_object(
                    self._bucket,
                    key,
                    io.BytesIO(raw),
                    len(raw),
                    content_type=f"image/{ext}",
                )
                return name
            except Exception:
                logger.warning(
                    "MinIO upload failed for {}/{}, saving locally", subdir, name
                )

        folder = self._local_base / subdir
        folder.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(folder / name), img)
        return name

    def _upload(
        self,
        subdir: str,
        name: str,
        raw: bytes,
        picknote: Optional[str] = None,
        stage: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """Upload pre-encoded bytes to MinIO; fall back to local on failure."""
        if self._client is None:
            self._write_local(subdir, name, raw)
            return
        ext = name.rsplit(".", 1)[-1]
        date_str = datetime.now().strftime("%d-%m-%y")
        key = f"{self._machine_id}/{date_str}/{subdir}/{name}"
        try:
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(raw),
                len(raw),
                content_type=f"image/{ext}",
            )
        except Exception:
            logger.warning("MinIO upload failed for {}, saving locally", key)
            self._write_local(subdir, name, raw)
            return

        # Upload succeeded — presign/log failures must not trigger the local fallback.
        try:
            preview_url = self._client.presigned_get_object(
                self._bucket,
                key,
                expires=timedelta(days=7),  # 7 days is the S3/minio maximum
                response_headers={"response-content-disposition": "inline"},
            )
            logger.bind(
                trace=trace_id,
                picknote=picknote,
                machine_id=self._machine_id,
                stage=stage,
                subdir=subdir,
            ).info("MinIO upload succeeded for {}", preview_url)
        except Exception:
            logger.warning("MinIO presign failed for {} (object uploaded)", key)

    def _write_local(self, subdir: str, name: str, raw: bytes) -> None:
        folder = self._local_base / subdir
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(raw)

    def _save_local(
        self, subdir: str, img: np.ndarray, prefix: str = "", ext: str = "jpg"
    ) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"{prefix}{ts}.{ext}" if prefix else f"{ts}.{ext}"
        folder = self._local_base / subdir
        folder.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(folder / name), img)
        return name

    def save_bg(
        self,
        subdir: str,
        img: np.ndarray,
        prefix: str = "",
        ext: str = "jpg",
        picknote: Optional[str] = None,
        stage: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> str | None:
        """Encode image immediately, then upload in a background thread.

        Returns the bare filename, or None if encoding failed (nothing saved).
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"{prefix}{ts}.{ext}" if prefix else f"{ts}.{ext}"

        # Encode on the calling thread (fast: ~20ms) so the upload thread only does I/O.
        ok, buf = cv2.imencode(f".{ext}", img)
        if not ok:
            logger.warning("save_bg: imencode failed for {}", subdir)
            return None
        raw = buf.tobytes()
        loop = asyncio.get_running_loop()

        async def _task():
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor,
                        self._upload,
                        subdir,
                        name,
                        raw,
                        picknote,
                        stage,
                        trace_id,
                    ),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "save_bg upload timed out for {} — saving locally", subdir
                )
                self._executor.submit(self._write_local, subdir, name, raw)
            except Exception:
                logger.exception("save_bg failed for {}", subdir)

        loop.create_task(_task())
        return name

    def migrate_existing(self) -> None:
        """Upload all images under DEBUG_IMAGE_PATH to MinIO, then delete them locally."""
        if self._client is None:
            return
        if not self._local_base.exists():
            return

        migrated = 0
        failed = 0
        image_extensions = {".jpg", ".jpeg", ".png", ".bmp"}

        for path in sorted(self._local_base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in image_extensions:
                continue

            # Determine date from filename (YYYYMMDD_...) or fall back to file mtime.
            subdir = path.parent.relative_to(self._local_base).as_posix()
            m = _DATE_RE.match(path.name)
            if m:
                year, month, day = m.group(1), m.group(2), m.group(3)
                date_str = f"{day}-{month}-{year[2:]}"
            else:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
                date_str = mtime.strftime("%d-%m-%y")

            key = f"{self._machine_id}/{date_str}/{subdir}/{path.name}"
            try:
                raw = path.read_bytes()
                ext = path.suffix.lstrip(".")
                self._client.put_object(
                    self._bucket,
                    key,
                    io.BytesIO(raw),
                    len(raw),
                    content_type=f"image/{ext}",
                )
                path.unlink()
                migrated += 1
            except Exception:
                failed += 1

        if migrated or failed:
            logger.info("MinIO migration: {} uploaded, {} failed", migrated, failed)


_store: ImageStore | None = None


def get_image_store() -> ImageStore:
    global _store
    if _store is None:
        _store = ImageStore()
    return _store
