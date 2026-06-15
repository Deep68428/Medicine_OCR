import json
import logging
import sqlite3
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from types import FrameType

from loguru import logger

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def new_scan_trace() -> str:
    """Generate a fresh trace ID for one scan event and bind it to the current async context."""
    tid = uuid.uuid4().hex[:12]
    _trace_id_var.set(tid)
    return tid


def _patch_trace_id(record: dict) -> None:
    record["extra"]["trace_id"] = _trace_id_var.get()


LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


class LokiSink:
    """Loguru sink: pushes logs to Loki with SQLite-backed offline buffering.

    Activated only when LOKI_URL is set in config. Logs are pushed directly via
    httpx. On failure (network down), entries are queued in SQLite and drained
    by a background thread every 10 seconds when connectivity resumes.
    """

    def __init__(self, url: str, machine_id: str, db_path: str = "logs/loki_buffer.db"):
        self._url = url.rstrip("/") + "/loki/api/v1/push"
        self._machine_id = machine_id
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS queue"
            "(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, source TEXT, picknote TEXT DEFAULT '', line TEXT)"
        )
        # Idempotent migration for buffer DBs created before the picknote column existed.
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(queue)")}
        if "picknote" not in cols:
            self._db.execute("ALTER TABLE queue ADD COLUMN picknote TEXT DEFAULT ''")
        self._db.commit()
        threading.Thread(target=self._drain_loop, daemon=True).start()

    def __call__(self, message):
        record = message.record
        extra = record["extra"]
        entry_dict: dict = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "message": record["message"],
            "module": record["name"],
            "function": record["function"],
            "line": record["line"],
            "source": extra.get("source", "machine"),
        }
        # Forward all bound extra fields (trace, stage, picknote, machine_id, trace_id, …)
        for k, v in extra.items():
            if k not in ("source",) and v is not None:
                entry_dict[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
        entry = json.dumps(entry_dict)
        ts_ns = str(int(record["time"].timestamp() * 1e9))
        level = record["level"].name
        source = record["extra"].get("source", "machine")
        picknote = record["extra"].get("picknote") or ""
        if not self._push_one(ts_ns, level, source, picknote, entry):
            with self._lock:
                self._db.execute(
                    "INSERT INTO queue (ts, level, source, picknote, line) VALUES (?,?,?,?,?)",
                    (ts_ns, level, source, picknote, entry),
                )
                self._db.commit()

    def _push_one(
        self, ts_ns: str, level: str, source: str, picknote: str, line: str
    ) -> bool:
        import httpx

        stream: dict[str, str] = {
            "job": "medicine_box",
            "machine_id": self._machine_id,
            "level": level,
            "source": source,
        }
        if picknote:
            stream["picknote"] = picknote
        payload = {
            "streams": [
                {
                    "stream": stream,
                    "values": [[ts_ns, line]],
                }
            ]
        }
        try:
            r = httpx.post(self._url, json=payload, timeout=5)
            return r.status_code in (200, 204)
        except Exception:
            return False

    def _drain_loop(self):
        while True:
            time.sleep(10)
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, ts, level, source, picknote, line FROM queue ORDER BY id LIMIT 100"
                ).fetchall()
            for row_id, ts_ns, level, source, picknote, line in rows:
                if self._push_one(ts_ns, level, source, picknote or "", line):
                    with self._lock:
                        self._db.execute("DELETE FROM queue WHERE id=?", (row_id,))
                        self._db.commit()
                else:
                    break  # still offline — stop draining


def intercept_fastapi_logs() -> None:
    """Intercept FastAPI's standard logging and redirect to loguru."""

    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            level_map = {
                logging.DEBUG: "DEBUG",
                logging.INFO: "INFO",
                logging.WARNING: "WARNING",
                logging.ERROR: "ERROR",
                logging.CRITICAL: "CRITICAL",
            }
            level = level_map.get(record.levelno, "INFO")

            frame: FrameType | None = sys._getframe(6)
            depth = 1
            while frame and frame.f_code.co_name in {
                "emit",
                "_log",
                "log",
                "info",
                "warning",
                "error",
                "critical",
            }:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level,
                record.getMessage(),
            )

    handler = InterceptHandler()

    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(logging.INFO)

    for name in ["uvicorn", "uvicorn.access", "fastapi"]:
        logger_instance = logging.getLogger(name)
        logger_instance.handlers = [handler]
        logger_instance.propagate = False


def setup_logging() -> None:
    logger.remove()

    logger.configure(extra={"trace_id": "-"}, patcher=_patch_trace_id)

    # intercept_fastapi_logs()

    logger.add(
        sys.stdout,
        level="INFO",
        colorize=True,
        backtrace=True,
        diagnose=True,
        enqueue=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<dim>trace={extra[trace_id]}</dim> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    logger.add(
        LOG_DIR / "machine_app.log",
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "trace={extra[trace_id]} | "
            "{name}:{function}:{line} - {message}"
        ),
    )

    logger.info("✅ Machine controller logging initialized")


_loki_configured = False


def configure_loki(loki_url: str, machine_id: int) -> None:
    """Attach a Loki sink to the running logger from remote config.

    Safe to call multiple times — only adds the sink once.
    No-op if loki_url is empty or DEV_MODE is enabled.
    """
    from core.config import get_config

    global _loki_configured
    if _loki_configured or not loki_url:
        return
    if get_config().DEV_MODE:
        logger.info(
            "DEV_MODE enabled — Loki sink disabled, no remote logging or local buffering"
        )
        return
    _loki = LokiSink(url=loki_url, machine_id=str(machine_id))
    logger.add(_loki, level="INFO", enqueue=True, backtrace=False, diagnose=False)
    _loki_configured = True
    logger.info("Loki sink configured: {}", loki_url)
