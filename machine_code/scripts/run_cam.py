#!/usr/bin/env python3
"""
run_cam.py
Wires hardware-triggered camera capture into the machine controller.
Call make_camera_runner() from the app lifespan to start/stop capture
alongside the FastAPI server.
"""

import asyncio

import cv2
import numpy as np
from loguru import logger

try:
    from scripts.camera_callback import TriggerCapture

    _CAMERA_AVAILABLE = True
except ImportError as _cam_err:
    logger.warning(
        "Hardware camera unavailable: {} — running in manual-trigger mode (POST /trigger)",
        _cam_err,
    )
    _CAMERA_AVAILABLE = False


class _NoOpCameraRunner:
    """Returned when mvIMPACT is not available. POST /trigger handles images instead."""

    def start_async(self):
        pass

    def stop(self):
        pass


def make_camera_runner(controller, loop: asyncio.AbstractEventLoop):
    """Return a TriggerCapture (hardware) or _NoOpCameraRunner (dev/test).

    When mvIMPACT is unavailable the no-op runner is returned and images
    must be submitted manually via POST /trigger.
    """
    if not _CAMERA_AVAILABLE:
        return _NoOpCameraRunner()

    _cfg = controller.state.machine_config or {}
    camera_serial = _cfg.get("camera_serial") or None
    exposure_time_us = _cfg.get("exposure_time_us") or 40000.0

    def on_frame(frame: np.ndarray, frame_index: int, timestamp_ms: int):
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            logger.warning(f"[cam] Failed to encode frame {frame_index}")
            return

        image_bytes = encoded.tobytes()
        picknote = controller.state.picknote or ""
        machine_id = controller.state.machine_id

        future = asyncio.run_coroutine_threadsafe(
            controller.process_camera_trigger(image_bytes, picknote, machine_id),
            loop,
        )
        try:
            result = future.result(timeout=120)
            logger.info(f"[cam] frame={frame_index} status={result.get('status')}")
        except TimeoutError:
            logger.warning(
                f"[cam] process_camera_trigger timed out for frame {frame_index}"
            )
            future.cancel()
        except Exception as exc:
            logger.exception(
                f"[cam] process_camera_trigger failed for frame {frame_index}: {exc}"
            )

    return TriggerCapture(
        on_frame=on_frame,
        camera_serial=camera_serial,
        exposure_time_us=exposure_time_us,
        trigger_source="Line4",
        trigger_activation="RisingEdge",
        timeout_ms=-1,
    )
