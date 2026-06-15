from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from loguru import logger

from core.config import get_config
from services.backend_client import BackendClient
from services.image_store import get_image_store
from services.camera import encode_frame_for_frontend
from services.matching import _normalize as _normalize_ocr, _token_match, run_matching
from services.recognition import (
    annotate_preview_with_ocr,
    encode_image_to_frontend,
    get_last_text_crops,
    run_detection,
    run_ocr_on_detections,
)
from services.state import PIPELINE_STAGES, MachineState, move_product_to_front


def _dbg_base() -> Path:
    return Path(get_config().DEBUG_IMAGE_PATH).expanduser()


def _normalize_name(text: str) -> str:
    """Normalize a product name: uppercase, strip OCR noise, collapse whitespace.

    Mirrors the ``_normalize`` function used in ``matching.py`` to ensure
    product-name lookups are consistent across modules.
    """
    text = text.upper()
    text = re.sub(r"[?µμ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class ScanPipeline:
    """Executes the three-stage image processing pipeline: detection, OCR, and matching."""

    def __init__(
        self,
        state: MachineState,
        backend: BackendClient,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        send_snapshot: Callable[[str], Awaitable[None]],
        handle_search: Callable[[dict[str, Any]], Awaitable[None]],
        conveyor=None,
    ) -> None:
        self.state = state
        self.backend = backend
        self._send = send
        self._send_snapshot = send_snapshot
        self._handle_search = handle_search
        self.conveyor = conveyor

    async def run(
        self,
        image_bytes: bytes,
        picknote: str = "",
        machine_id: int | None = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run detection, OCR, and matching on a captured frame and log total elapsed time."""
        _t_start = time.perf_counter()
        result = await self._run_pipeline(image_bytes, picknote, machine_id, trace_id)
        elapsed_ms = int((time.perf_counter() - _t_start) * 1000)
        status = result.get("status", "unknown")
        stage = result.get("stage", "")
        _pn = self.state.picknote or ""
        if status == "success":
            logger.bind(picknote=_pn).info(
                "Pipeline finished in {}ms — accepted", elapsed_ms
            )
        elif status == "ambiguous":
            logger.bind(picknote=_pn).info(
                "Pipeline finished in {}ms — ambiguous", elapsed_ms
            )
        else:
            logger.bind(picknote=_pn).info(
                "Pipeline finished in {}ms — {} at stage={}", elapsed_ms, status, stage
            )
        return result

    async def _run_pipeline(
        self,
        image_bytes: bytes,
        picknote: str = "",
        machine_id: int | None = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Internal pipeline body — detection, OCR, matching."""
        _pn = self.state.picknote or ""
        logger.bind(picknote=_pn).info("Processing camera_trigger event")
        requested_picknote = picknote.strip()
        requested_machine_id = (
            machine_id if machine_id is not None else self.state.machine_id
        )

        if requested_picknote and (
            requested_picknote != self.state.picknote
            or requested_machine_id != self.state.machine_id
        ):
            logger.bind(picknote=requested_picknote).info(
                f"camera_trigger requires picknote refresh: "
                f"requested={requested_picknote} current={self.state.picknote}"
            )
            await self._handle_search(
                {
                    "type": "search",
                    "picknote": requested_picknote,
                    "machine_id": requested_machine_id,
                }
            )

        if not self.state.picknote:
            message = "Picknote is not loaded for camera trigger"
            logger.bind(picknote=_pn).warning(message)
            await self._send({"type": "error", "message": message})
            return {"status": "error", "message": message, "stage": "search"}

        if not image_bytes:
            message = "camera_trigger requires an image"
            logger.bind(picknote=_pn).warning(
                "camera_trigger called without image bytes"
            )
            await self._send({"type": "error", "message": message})
            return {"status": "error", "message": message, "stage": "input"}

        # Decode image once for all stages
        image_np = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image_np is None:
            message = "Failed to decode image"
            logger.bind(picknote=_pn).error(message)
            await self._send({"type": "error", "message": message})
            return {"status": "error", "message": message, "stage": "input"}

        self.state.latest_image = encode_frame_for_frontend(image_bytes)
        self.state.errors = dict.fromkeys(PIPELINE_STAGES)
        # Preserve a clean copy before run_detection draws polylines on image_np in-place.
        clean_image_np = image_np.copy()

        # --- Detection ---
        try:
            _t = time.perf_counter()
            detection_results = await run_detection(image_np, self.state.machine_id)
            _det_ms = int((time.perf_counter() - _t) * 1000)
            self.state.last_detection_results = detection_results
            logger.bind(picknote=_pn).info(
                "Detection: {}ms — {} detections", _det_ms, len(detection_results)
            )
            if not detection_results:
                get_image_store().save_bg(
                    "batch_barcode",
                    image_np,
                    picknote=_pn,
                    stage="detection",
                    trace_id=trace_id,
                )
                get_image_store().save_bg(
                    "full_image",
                    clean_image_np,
                    picknote=_pn,
                    stage="detection",
                    trace_id=trace_id,
                )
                try:
                    if self.conveyor is not None:
                        self.conveyor.move_reject()
                except Exception as e:
                    logger.bind(picknote=_pn).exception(
                        "Failed to trigger conveyor: {}", e
                    )
                return await self._fail_stage("detection", "No detection results found")
            self._update_latest_image(encode_image_to_frontend(image_np))
        except Exception as exc:
            get_image_store().save_bg(
                "full_image",
                clean_image_np,
                picknote=_pn,
                stage="detection",
                trace_id=trace_id,
            )
            try:
                if self.conveyor is not None:
                    self.conveyor.move_reject()
            except Exception as e:
                logger.bind(picknote=_pn).exception("Failed to trigger conveyor: {}", e)
            logger.bind(picknote=_pn).exception("Detection stage failed")
            return await self._fail_stage("detection", str(exc))

        # --- OCR / Barcode ---
        try:
            _t = time.perf_counter()
            ocr_results, preview_crop = await run_ocr_on_detections(detection_results)
            _ocr_ms = int((time.perf_counter() - _t) * 1000)
            self.state.last_ocr_results = ocr_results
            _ocr_src = (
                "barcode"
                if any(r.get("orientation") == "barcode" for r in ocr_results)
                else "ocr"
            )
            logger.bind(picknote=_pn).info(
                "OCR/Barcode: {}ms via {} — {} results",
                _ocr_ms,
                _ocr_src,
                len(ocr_results),
            )
            if not ocr_results:
                _text_crops = get_last_text_crops()
                if not _text_crops:
                    # Text detection produced no boxes — save batch crop for text-model retraining
                    if preview_crop is not None:
                        get_image_store().save_bg(
                            "text",
                            preview_crop,
                            picknote=_pn,
                            stage="ocr",
                            trace_id=trace_id,
                        )
                else:
                    # Text boxes found but Paddle returned nothing — save each crop to ocr/
                    for _i, _c in enumerate(_text_crops):
                        get_image_store().save_bg(
                            "ocr",
                            _c,
                            prefix=f"text_{_i}_",
                            ext="png",
                            picknote=_pn,
                            stage="ocr",
                            trace_id=trace_id,
                        )
                get_image_store().save_bg(
                    "full_image",
                    clean_image_np,
                    picknote=_pn,
                    stage="ocr",
                    trace_id=trace_id,
                )
                try:
                    if self.conveyor is not None:
                        self.conveyor.move_reject()
                except Exception as e:
                    logger.bind(picknote=_pn).exception(
                        "Failed to trigger conveyor: {}", e
                    )
                return await self._fail_stage("ocr", "No OCR results found")
            self._update_latest_image(
                encode_image_to_frontend(
                    annotate_preview_with_ocr(preview_crop, ocr_results)
                )
            )
        except Exception as exc:
            get_image_store().save_bg(
                "full_image",
                clean_image_np,
                picknote=_pn,
                stage="ocr",
                trace_id=trace_id,
            )
            try:
                if self.conveyor is not None:
                    self.conveyor.move_reject()
            except Exception as e:
                logger.bind(picknote=_pn).exception("Failed to trigger conveyor: {}", e)
            logger.bind(picknote=_pn).exception("OCR stage failed")
            return await self._fail_stage("ocr", str(exc))

        # --- Matching ---
        return await self._run_matching_stage(
            ocr_results,
            trigger_conveyor=True,
            preview_crop=preview_crop,
            full_image_np=clean_image_np,
            trace_id=trace_id,
        )

    # -------------------------------------------------------------------------
    # Pipeline helpers
    # -------------------------------------------------------------------------

    async def _run_matching_stage(
        self,
        ocr_results: list[dict[str, Any]],
        trigger_conveyor: bool = True,
        preview_crop: np.ndarray | None = None,
        barcode_product_code: str | None = None,
        full_image_np: np.ndarray | None = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run matching and apply results. Shared by camera and barcode scanner paths.

        Args:
            ocr_results: OCR/barcode results to match against the picklist.
            trigger_conveyor: Send accept/reject conveyor commands when True.
                Set to False for hardware barcode scanner (no physical trigger).
            preview_crop: Last camera crop for debug saving on reject. None for scanner.
            barcode_product_code: Product code extracted from GS1 barcode AI 02 field.
                Used for disambiguation when multiple batch numbers match.
        """
        try:
            _t = time.perf_counter()
            matching_payload = run_matching(
                ocr_results,
                self.state.products,
                self.state.latest_image,
                product_batch_list=self.state.product_batch_list,
                barcode_product_code=barcode_product_code,
            )
            _match_ms = int((time.perf_counter() - _t) * 1000)
            matching_state = matching_payload.get("state")
            logger.bind(picknote=self.state.picknote or "").info(
                "Matching: {}ms — state={}", _match_ms, matching_state
            )
            self.state.last_matching_result = (
                matching_payload.get("matching_result") or []
            )
            preview_images: list = matching_payload.get("preview_image") or []

            if matching_state == "accepted":
                first_result = (
                    self.state.last_matching_result[0]
                    if self.state.last_matching_result
                    else None
                )
                apply_result = self._apply_matching_results(first_result)
                if apply_result.get("status") == "exhausted":
                    product_name = apply_result.get("product_name", "Unknown Product")
                    logger.bind(picknote=self.state.picknote or "").warning(
                        f"scan_exhausted product={product_name!r} picknote={self.state.picknote!r}"
                    )
                    if trigger_conveyor:
                        try:
                            if self.conveyor is not None:
                                self.conveyor.move_reject()
                        except Exception as e:
                            logger.bind(picknote=self.state.picknote or "").exception(
                                "Failed to trigger conveyor: {}", e
                            )
                    await self._send(
                        {
                            "type": "alert",
                            "message": f"No more pending qty for product {product_name}",
                        }
                    )
                    await self._send_snapshot("scan_exhausted")
                    return {
                        "status": "exhausted",
                        "picknote": self.state.picknote,
                        "machine_id": self.state.machine_id,
                        "product_name": product_name,
                    }
                else:
                    logger.bind(picknote=self.state.picknote or "").info(
                        "Matching accepted; applying results"
                    )
                    if trigger_conveyor:
                        try:
                            if self.conveyor is not None:
                                self.conveyor.move_accept()
                        except Exception as e:
                            logger.bind(picknote=self.state.picknote or "").exception(
                                "Failed to trigger conveyor: {}", e
                            )

            elif matching_state == "batch_correction":
                scanned_batch = matching_payload.get("scanned_batch", "")
                matched_name = matching_payload.get("matched_product_name", "")
                logger.bind(picknote=self.state.picknote or "").info(
                    f"Batch correction: scanned={scanned_batch!r} product={matched_name!r}"
                )
                apply_result = self._apply_batch_correction(scanned_batch, matched_name)
                if apply_result.get("status") == "exhausted":
                    product_name = apply_result.get("product_name", matched_name)
                    logger.bind(picknote=self.state.picknote or "").warning(
                        f"scan_exhausted product={product_name!r} picknote={self.state.picknote!r}"
                    )
                    if trigger_conveyor:
                        try:
                            if self.conveyor is not None:
                                self.conveyor.move_reject()
                        except Exception as e:
                            logger.bind(picknote=self.state.picknote or "").exception(
                                "Failed to trigger conveyor: {}", e
                            )
                    await self._send(
                        {
                            "type": "alert",
                            "message": f"No more pending qty for product {product_name}",
                        }
                    )
                    await self._send_snapshot("scan_exhausted")
                    return {
                        "status": "exhausted",
                        "picknote": self.state.picknote,
                        "machine_id": self.state.machine_id,
                        "product_name": product_name,
                    }
                else:
                    if trigger_conveyor:
                        try:
                            if self.conveyor is not None:
                                self.conveyor.move_accept()
                        except Exception as e:
                            logger.bind(picknote=self.state.picknote or "").exception(
                                "Failed to trigger conveyor: {}", e
                            )

            elif matching_state == "ambiguous":
                logger.bind(picknote=self.state.picknote or "").info(
                    f"Matching ambiguous: {len(self.state.last_matching_result)} candidates"
                )
                scanned_texts = [
                    r.get("text", "") for r in ocr_results if r.get("text")
                ]
                # Find the OCR text that contains a candidate batch as a token —
                # works for short batches like "1134" and long ones like "SA25260382".
                candidate_batches = [
                    _normalize_ocr(
                        str(c.get("_correction_batch") or c.get("batch_number") or "")
                    )
                    for c in self.state.last_matching_result
                ]
                best_scanned = next(
                    (
                        t
                        for t in scanned_texts
                        if any(
                            b and _token_match(b, _normalize_ocr(t))
                            for b in candidate_batches
                        )
                    ),
                    scanned_texts[0] if scanned_texts else "",
                )
                self.state.ambiguous_pending = True
                await self._send(
                    {
                        "type": "scan_ambiguous",
                        "candidates": self.state.last_matching_result,
                        "preview_images": preview_images,
                        "scanned_batch": best_scanned,
                    }
                )
                return {
                    "status": "ambiguous",
                    "picknote": self.state.picknote,
                    "machine_id": self.state.machine_id,
                    "candidates": self.state.last_matching_result,
                }

            else:
                _text_crops = get_last_text_crops()
                _ocr_dir = _dbg_base() / "ocr"
                _ocr_dir.mkdir(parents=True, exist_ok=True)
                with (_ocr_dir / "ocr.txt").open("a", encoding="utf-8") as _f:
                    if _text_crops:
                        for _i, _crop in enumerate(_text_crops):
                            _name = get_image_store().save_bg(
                                "ocr",
                                _crop,
                                prefix=f"text_{_i}_",
                                ext="png",
                                picknote=self.state.picknote or "",
                                stage="matching",
                                trace_id=trace_id,
                            )
                            _ocr_text = (
                                ocr_results[_i].get("text", "")
                                if _i < len(ocr_results)
                                else ""
                            )
                            if _name:
                                _f.write(f"{_name}\t{_ocr_text}\n")

                    elif preview_crop is not None:
                        _name = get_image_store().save_bg(
                            "ocr",
                            preview_crop,
                            prefix="crop_",
                            ext="png",
                            picknote=self.state.picknote or "",
                            stage="matching",
                            trace_id=trace_id,
                        )
                        _ocr_text = (
                            " | ".join(r.get("text", "") for r in ocr_results)
                            if ocr_results
                            else ""
                        )
                        if _name:
                            _f.write(f"{_name}\t{_ocr_text}\n")
                if preview_crop is not None:
                    get_image_store().save_bg(
                        "text",
                        preview_crop,
                        prefix="preview_",
                        ext="png",
                        picknote=self.state.picknote or "",
                        stage="matching",
                        trace_id=trace_id,
                    )
                if full_image_np is not None:
                    get_image_store().save_bg(
                        "full_image",
                        full_image_np,
                        picknote=self.state.picknote,
                        trace_id=trace_id,
                        stage="matching",
                    )
                if trigger_conveyor:
                    try:
                        if self.conveyor is not None:
                            self.conveyor.move_reject()
                    except Exception as e:
                        logger.bind(picknote=self.state.picknote or "").exception(
                            "Failed to trigger conveyor: {}", e
                        )
                logger.bind(picknote=self.state.picknote or "").warning(
                    f"scan_rejected picknote={self.state.picknote!r} matching_state={matching_state!r}"
                )
                return await self._fail_stage("matching", "Matching failed")

        except Exception as exc:
            if full_image_np is not None:
                get_image_store().save_bg(
                    "full_image",
                    full_image_np,
                    picknote=self.state.picknote,
                    trace_id=trace_id,
                    stage="matching",
                )
            if trigger_conveyor:
                try:
                    if self.conveyor is not None:
                        self.conveyor.move_reject()
                except Exception as e:
                    logger.bind(picknote=self.state.picknote or "").exception(
                        "Failed to trigger conveyor: {}", e
                    )
            logger.bind(picknote=self.state.picknote or "").exception(
                "Matching stage failed"
            )
            return await self._fail_stage("matching", str(exc))

        await self._send_snapshot("scan_result")
        return {
            "status": "success",
            "picknote": self.state.picknote,
            "machine_id": self.state.machine_id,
            "current_product": self.state.current_product,
            "matching_result": self.state.last_matching_result,
        }

    async def _fail_stage(self, stage: str, message: str) -> dict[str, Any]:
        """Record a stage failure, notify the frontend, and return an error dict.

        Args:
            stage: Pipeline stage name (one of ``PIPELINE_STAGES``).
            message: Human-readable description of the failure.

        Returns:
            A dict with ``status="error"``, the failing ``stage``, and ``message``.
        """
        self.state.errors[stage] = message
        await self._send_pipeline_failure(stage, message)
        return {"status": "error", "message": message, "stage": stage}

    async def _send_pipeline_failure(self, stage: str, message: str) -> None:
        """Emit a ``scan_failed`` event to the frontend with stage and error details."""
        logger.bind(picknote=self.state.picknote or "").error(
            f"Scan pipeline failed at stage={stage}: {message}"
        )
        await self._send(
            {
                "type": "scan_failed",
                "stage": stage,
                "error": {"type": stage, "message": message},
                "errors": self.state.errors,
                "image": self.state.latest_image,
            }
        )

    def _update_latest_image(self, preview_image: str | None) -> None:
        """Replace the stored preview image if a non-empty value is provided."""
        if preview_image:
            self.state.latest_image = preview_image

    # -------------------------------------------------------------------------
    # Product matching
    # -------------------------------------------------------------------------

    def _apply_matching_results(
        self, matching_result: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Apply an accepted matching result to the product list, updating quantities.

        Args:
            matching_result: The top matching result dict, or None.

        Returns:
            A dict with ``status`` of ``"updated"``, ``"exhausted"``, or ``"no_products"``.
        """
        _pn = self.state.picknote or ""
        if not self.state.products:
            logger.bind(picknote=_pn).warning(
                "Attempted to apply matching results when no products are loaded"
            )
            return {"status": "no_products"}

        matched_product = None
        scanned_batch_for_log: str | None = None
        if matching_result:
            matched_product = self._find_product_for_match(
                self.state.products, matching_result
            )

        if matched_product is None:
            logger.bind(picknote=_pn).info(
                "No direct match found; falling back to first pending product"
            )
            current_product: dict[str, Any] = next(
                (p for p in self.state.products if p.get("pending_quantity", 0) > 0),
                self.state.products[0],
            )
        else:
            current_product = matched_product

        self.state.current_product = current_product
        batch_qty = current_product.get("batch_quantity", 0)
        done_qty = current_product.get("done_quantity", 0)
        # Skip exhaustion check if batch_quantity is 0 (allow 0 demanded quantity tracking)
        if batch_qty > 0 and done_qty >= batch_qty:
            # The matched row is exhausted — look for another row of the same
            # product that still has pending quantity.  This handles the case
            # where batch corrections consumed this row's capacity.
            product_name = _normalize_name(current_product.get("product_name") or "")
            alternate = next(
                (
                    p
                    for p in self.state.products
                    if _normalize_name(p.get("product_name") or "") == product_name
                    and p.get("pending_quantity", 0) > 0
                    and p is not current_product
                ),
                None,
            )
            if alternate is not None:
                # The originally matched batch_number is the one on the package — record it
                # as a batch_correction on the alternate row so the UI shows it correctly.
                original_batch = (
                    str(matching_result.get("batch_number", "")).strip()
                    if matching_result
                    else ""
                )
                if original_batch:
                    if "batch_corrections" not in alternate:
                        alternate["batch_corrections"] = []
                    alternate["batch_corrections"].append(original_batch)
                    scanned_batch_for_log = original_batch
                    logger.bind(picknote=_pn).info(
                        f"Matched row exhausted, falling back to alternate row for "
                        f"product={product_name!r} alternate_batch={alternate.get('batch_number')!r} "
                        f"recording correction={original_batch!r}"
                    )
                else:
                    logger.bind(picknote=_pn).info(
                        f"Matched row exhausted, falling back to alternate row for "
                        f"product={product_name!r} batch={alternate.get('batch_number')!r}"
                    )
                current_product = alternate
                self.state.current_product = current_product
            else:
                logger.bind(picknote=_pn).warning(
                    f"Matched product has no pending quantity left: {current_product.get('product_name')}"
                )
                return {
                    "status": "exhausted",
                    "product_name": current_product.get("product_name"),
                }

        strip = current_product.get("strip_in_box", 1) or 1
        previous_done = current_product.get("done_quantity", 0)
        batch_qty = current_product.get("batch_quantity", 0)

        if batch_qty == 0:
            # Over-scans on batch_quantity=0: track as positive
            current_product["done_quantity"] = previous_done + strip
            current_product["pending_quantity"] = 0
        else:
            # Normal case: track progress towards batch_quantity
            current_product["done_quantity"] = previous_done + strip
            current_product["pending_quantity"] = max(
                batch_qty - current_product["done_quantity"],
                0,
            )
        _logged_batch = current_product.get("batch_number")
        current_product.setdefault("scan_log", []).append(
            {
                "packbox": self.state.current_packbox,
                "batch_number": _logged_batch,
            }
        )
        logger.bind(picknote=self.state.picknote or "").info(
            f"scan_recorded picknote={self.state.picknote} "
            f"product={current_product.get('product_name')!r} "
            f"batch={_logged_batch!r} packbox={self.state.current_packbox} "
            f"kind={'correction' if scanned_batch_for_log else 'normal'}"
        )
        new_done = current_product["done_quantity"]
        if batch_qty > 0 and new_done >= batch_qty and previous_done < batch_qty:
            logger.bind(picknote=self.state.picknote or "").info(
                f"line_item_completed picknote={self.state.picknote} "
                f"product={current_product.get('product_name')!r} "
                f"product_code={current_product.get('product_code')!r} "
                f"batch={current_product.get('batch_number')!r} "
                f"row_index={current_product.get('row_index')}"
            )
        move_product_to_front(self.state, current_product)
        logger.bind(picknote=_pn).info(
            f"Updated quantities for {current_product.get('product_name')}: "
            f"done={current_product['done_quantity']} "
            f"pending={current_product['pending_quantity']} "
            f"(was done={previous_done})"
        )
        return {
            "status": "updated",
            "product_name": current_product.get("product_name"),
        }

    def _find_product_for_match(
        self,
        products: list[dict[str, Any]],
        match: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the product whose unique row_index matches the given matching result."""
        _pn = self.state.picknote or ""
        match_idx = match.get("row_index")
        if match_idx is not None:
            for product in products:
                if product.get("row_index") == match_idx:
                    logger.bind(picknote=_pn).info(
                        f"Matched product by row_index={match_idx}"
                    )
                    return product

        # Fallback to batch_number if row_index is missing (e.g. legacy data)
        match_batch = str(match.get("batch_number", "")).lower().strip()
        if not match_batch:
            logger.bind(picknote=_pn).warning(
                "Matching result has no row_index or batch_number — cannot find product"
            )
            return None
        for product in products:
            if str(product.get("batch_number", "")).lower().strip() == match_batch:
                logger.bind(picknote=_pn).info(
                    f"Matched product by fallback batch_number='{match_batch}'"
                )
                return product
        logger.bind(picknote=_pn).warning(
            f"No product found for match with row_index={match_idx} or batch='{match_batch}'"
        )
        return None

    def _apply_batch_correction(
        self, scanned_batch: str, matched_product_name: str
    ) -> dict[str, Any]:
        """Apply a batch-correction scan to the first pending row for the matched product.

        Appends ``scanned_batch`` to the row's ``batch_corrections`` list and increments
        done/pending quantities exactly like a normal accepted scan. Exhausted is checked
        against the combined done/qty totals for all rows sharing the product name.

        Args:
            scanned_batch: The actual batch string read by OCR from the package.
            matched_product_name: Product name resolved from the full batch list lookup.

        Returns:
            A dict with ``status`` of ``"updated"``, ``"exhausted"``, or ``"no_products"``.
        """
        products = self.state.products
        if not products:
            return {"status": "no_products"}

        _pn = self.state.picknote or ""
        if not scanned_batch or not scanned_batch.strip():
            logger.bind(picknote=_pn).warning(
                "_apply_batch_correction called with empty scanned_batch — skipping"
            )
            return {"status": "no_products"}

        norm_name = _normalize_name(matched_product_name)

        def _name_matches(p: dict[str, Any]) -> bool:
            return _normalize_name(p.get("product_name") or "") == norm_name

        # First pending row for this product name
        target = next(
            (
                p
                for p in products
                if _name_matches(p) and p.get("pending_quantity", 0) > 0
            ),
            None,
        )
        has_pending_target = target is not None  # captured before the fallback

        # Fallback: first row for this product (all rows exhausted)
        if target is None:
            target = next((p for p in products if _name_matches(p)), None)
        if target is None:
            logger.bind(picknote=_pn).warning(
                f"_apply_batch_correction: no row found for product_name={matched_product_name!r}"
            )
            return {"status": "no_products"}

        # Exhaustion check: use pending_quantity (always capped at 0 via max()) rather than
        # done_quantity (which can exceed batch_quantity when strip_in_box > 1, causing
        # premature exhaustion when multiple rows exist for the same product).
        total_qty = sum(
            p.get("batch_quantity", 0) for p in products if _name_matches(p)
        )
        if total_qty > 0 and not has_pending_target:
            logger.bind(picknote=_pn).warning(
                f"Batch correction over-scan: product={matched_product_name!r} "
                f"all rows exhausted (total_qty={total_qty})"
            )
            return {"status": "exhausted", "product_name": matched_product_name}

        # Record the correction batch and increment quantities
        if "batch_corrections" not in target:
            target["batch_corrections"] = []
        target["batch_corrections"].append(scanned_batch)

        strip = target.get("strip_in_box", 1) or 1
        prev_done = target.get("done_quantity", 0)
        batch_qty = target.get("batch_quantity", 0)

        if batch_qty == 0:
            # Over-scans on batch_quantity=0: track as positive count
            target["done_quantity"] = prev_done + strip
            target["pending_quantity"] = 0
        else:
            # Normal case: track progress towards batch_quantity
            target["done_quantity"] = prev_done + strip
            target["pending_quantity"] = max(batch_qty - target["done_quantity"], 0)
        target.setdefault("scan_log", []).append(
            {
                "packbox": self.state.current_packbox,
                "batch_number": scanned_batch,
            }
        )
        logger.bind(picknote=self.state.picknote or "").info(
            f"scan_recorded picknote={self.state.picknote} "
            f"product={matched_product_name!r} batch={scanned_batch!r} "
            f"packbox={self.state.current_packbox} kind=correction"
        )
        new_done = target["done_quantity"]
        if batch_qty > 0 and new_done >= batch_qty and prev_done < batch_qty:
            logger.bind(picknote=self.state.picknote or "").info(
                f"line_item_completed picknote={self.state.picknote} "
                f"product={target.get('product_name')!r} "
                f"product_code={target.get('product_code')!r} "
                f"batch={target.get('batch_number')!r} "
                f"row_index={target.get('row_index')}"
            )
        self.state.current_product = target
        move_product_to_front(self.state, target)

        logger.bind(picknote=_pn).info(
            f"Batch correction applied: product={matched_product_name!r} "
            f"scanned={scanned_batch!r} done={target['done_quantity']} "
            f"pending={target['pending_quantity']}"
        )
        return {"status": "updated", "product_name": matched_product_name}
