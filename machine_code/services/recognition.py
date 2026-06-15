from __future__ import annotations

import base64
import time

import cv2
import numpy as np
from fastapi.concurrency import run_in_threadpool
from loguru import logger

from services.barcode_decoder import decode_barcode_crop
from services.openvino_ocr import OCRPipeline

pipeline = None
_MODELS_LOADED = False


def initialize_pipeline() -> None:
    """Load OCRPipeline after models have been downloaded. Called by the controller."""
    global pipeline, _MODELS_LOADED
    try:
        pipeline = OCRPipeline()
        _MODELS_LOADED = True
        logger.info("OCR pipeline initialized successfully")
    except FileNotFoundError as e:
        logger.warning(
            "ML models not found ({}). Scans will fail until models are downloaded.", e
        )
        pipeline = None
        _MODELS_LOADED = False


def _require_pipeline() -> OCRPipeline:
    if pipeline is None:
        raise RuntimeError(
            "ML models are not available. Check GITLAB credentials in machine-config "
            "and restart the machine controller."
        )
    return pipeline


def get_last_text_crops() -> list:
    return getattr(pipeline, "last_text_crops", [])


def warmup_pipeline(iterations: int = 3) -> None:
    """Run a few dummy inference passes to JIT-compile OpenVINO graphs.
    Called from the app lifespan in a background thread — not at import time.
    """
    if pipeline is None:
        return
    pipeline.warmup(iterations=iterations)


def encode_image_to_frontend(image: np.ndarray | None) -> str | None:
    if image is None:
        return None
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        return None
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def annotate_preview_with_ocr(
    image: np.ndarray | None, ocr_results: list[dict]
) -> np.ndarray | None:
    """Draw OCR text in the top-left corner, scaled to ~4% of image height."""
    if not ocr_results or image is None:
        return image
    texts = [r.get("text", "") for r in ocr_results if r.get("text")]
    if not texts:
        return image
    out = image.copy()
    h = out.shape[0]
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.3, h * 0.035 / 20)  # ~3.5% of image height
    thickness = 1
    pad = 2
    y_cursor = pad
    for text in texts:
        (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        y_text = y_cursor + th
        cv2.rectangle(
            out,
            (pad, y_cursor),
            (pad + tw + pad * 2, y_text + baseline + pad),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            out,
            text,
            (pad * 2, y_text),
            font,
            font_scale,
            (0, 255, 0),
            thickness,
            cv2.LINE_AA,
        )
        y_cursor = y_text + baseline + pad * 2
    return out


async def run_detection(image: np.ndarray, machine_id: int) -> list[dict]:
    return await run_in_threadpool(_run_detection_sync, image, machine_id)


def _run_detection_sync(image: np.ndarray, machine_id: int) -> list[dict]:
    result = _require_pipeline().predict_yolo(image)

    detection_results: list[dict] = []

    if not hasattr(result, "boxes") or result.boxes is None:
        return detection_results

    boxes = result.boxes
    crops = result.crops if result.crops else []

    for i in range(len(boxes.xyxyxyxy)):
        coords = boxes.xyxyxyxy[i].flatten().tolist()
        cls = int(boxes.cls[i])
        conf = float(boxes.conf[i])

        detection_results.append(
            {
                "detection_id": i,
                "coordinates": coords,
                "class_name": result.names.get(cls, str(cls)),
                "confidence": conf,
                "machine_id": machine_id,
                "crop": crops[i] if i < len(crops) else None,
            }
        )

    for detection in detection_results:
        coords = detection["coordinates"]
        xs = coords[0::2]
        ys = coords[1::2]
        cv2.polylines(
            image,
            [np.array([(int(x), int(y)) for x, y in zip(xs, ys)], dtype=np.int32)],
            isClosed=True,
            color=(0, 0, 255),
            thickness=2,
        )
    return detection_results


async def run_ocr(crop: np.ndarray):
    return await run_in_threadpool(_run_ocr_sync, crop)


def _run_ocr_sync(crop: np.ndarray):
    now = time.time()
    results = _require_pipeline().predict_rapid(crop)
    logger.info("OCR completed in {}ms", f"{(time.time() - now) * 1000:.2f}")
    return results


async def run_ocr_on_detections(
    detection_results: list[dict],
) -> tuple[list[dict], np.ndarray | None]:
    ocr_results: list[dict] = []
    preview_crop: np.ndarray | None = None

    def _parse_item(item):
        if not isinstance(item, (list, tuple)):
            return None, None
        if len(item) >= 3:
            return item[1], item[2]
        if len(item) == 2:
            return item[0], item[1]
        return None, None

    barcode_dets = [d for d in detection_results if d.get("class_name") == "barcode"]
    batch_dets = [d for d in detection_results if d.get("class_name") == "batch"]

    # --- Barcode first: if any decode succeeds, skip OCR entirely ---
    for det in barcode_dets:
        crop = det.get("crop")
        if crop is None:
            logger.warning(
                f"Skipping barcode detection {det.get('detection_id')} with no crop"
            )
            continue
        preview_crop = crop.copy()
        batch_text = await run_in_threadpool(decode_barcode_crop, crop)
        if batch_text:
            logger.info(f"Barcode decoded batch: {batch_text} — skipping OCR")
            ocr_results.append(
                {
                    "detection_id": det.get("detection_id"),
                    "text": batch_text,
                    "confidence": 1.0,
                    "machine_id": det.get("machine_id"),
                    "orientation": "barcode",
                }
            )

    if ocr_results:
        return ocr_results, preview_crop

    # --- Barcode failed or absent — fall back to Paddle OCR on batch crops ---
    logger.info("Barcode decode produced no result — falling back to OCR")
    for det in batch_dets:
        crop = det.get("crop")
        if crop is None:
            logger.warning(
                f"Skipping batch detection {det.get('detection_id')} with no crop"
            )
            continue
        preview_crop = crop.copy()

        results = await run_ocr(crop)

        all_results = []
        if results:
            detections = results[0] if isinstance(results, tuple) else results
            all_results.extend([(d, "original") for d in detections])

        print("OCR raw results:", all_results)
        for detection, orientation in all_results:
            text, score = _parse_item(detection)
            if not text:
                continue
            logger.info("OCR text ({}): {}", orientation, text)
            ocr_results.append(
                {
                    "detection_id": det.get("detection_id"),
                    "text": str(text),
                    "confidence": float(score) if score is not None else None,
                    "machine_id": det.get("machine_id"),
                    "orientation": orientation,
                }
            )

    return ocr_results, preview_crop
