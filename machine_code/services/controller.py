from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import WebSocket
from loguru import logger

from core.config import get_config
from core.logging import configure_loki, new_scan_trace
from services.backend_client import BackendClient
from services.barcode_decoder import _extract_gs1_fields, _normalize_barcode
from services.image_store import get_image_store
from services.recognition import initialize_pipeline
from utils.load_model import download_model

from services.camera import decode_frame_from_payload
from services.persistence import load_and_restore, save_state
from services.pipeline import ScanPipeline
from services.state import (
    ConfigStatus,
    MachineState,
    _safe_int,
    build_snapshot,
    reset_scan_state,
)
from services.websocket_transport import (
    connect_websocket,
    disconnect_websocket,
    send_payload,
)


def _http_error_message(exc: httpx.HTTPError) -> str:
    """Return a short, user-friendly error string without MDN/docs URLs."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = exc.response.reason_phrase or ""
        return f"HTTP {code}{f' {reason}' if reason else ''}"
    # ConnectError, TimeoutException, ReadError, etc.
    return type(exc).__name__.replace("Error", " error").replace("Exception", " error")


class MachineController:
    """Top-level coordinator that wires together state, backend client, pipeline,
    and WebSocket transport for a single machine."""

    def __init__(self) -> None:
        config = get_config()
        self.backend = BackendClient()
        self.state = MachineState(machine_id=config.MACHINE_ID)
        self.conveyor = None
        self._scan_lock = asyncio.Lock()
        self._last_barcode: str = ""
        self._last_scan_time: float = 0.0
        self.pipeline = ScanPipeline(
            state=self.state,
            backend=self.backend,
            send=self.send,
            send_snapshot=self.send_snapshot,
            handle_search=self._handle_search,
            conveyor=self.conveyor,
        )
        logger.info(
            f"MachineController initialized with machine_id={self.state.machine_id}"
        )

    async def close(self) -> None:
        """Shut down the backend HTTP client cleanly."""
        await self.backend.aclose()

    # -------------------------------------------------------------------------
    # Startup
    # -------------------------------------------------------------------------

    async def initialize_from_server(self) -> None:
        """Load the machine configuration from the backend API on startup.

        Sets ``state.config_status`` to ``"loaded"`` on success, or to
        ``"missing"`` / ``"error"`` when the request fails.
        """
        machine_id = self.state.machine_id
        self.state.config_status = "loading"
        self.state.config_error = None
        logger.info(f"Loading machine config from server for machine_id={machine_id}")

        try:
            machine_config = await self.backend.fetch_machine_config(machine_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                message = f"Machine config not found for machine_id={machine_id}"
                logger.warning(message)
                self._set_config_error("missing", message)
                return
            logger.exception("Machine config request failed with HTTP error")
            self._set_config_error("error", _http_error_message(exc))
            return
        except httpx.HTTPError as exc:
            logger.exception("Machine config request failed")
            self._set_config_error("error", _http_error_message(exc))
            return
        self.state.machine_config = machine_config
        self.state.machine_id = int(machine_config.get("machine_id") or machine_id)
        self.state.config_status = "loaded"
        self.state.config_error = None
        logger.info(f"Machine config loaded for machine_id={self.state.machine_id}")

        # Configure Loki from remote config (no-op if loki_url is empty)
        configure_loki(
            loki_url=machine_config.get("loki_url") or "",
            machine_id=self.state.machine_id,
        )

        # Apply MinIO config from remote — must happen before get_image_store() is called
        cfg = get_config()
        cfg.MINIO_ENDPOINT = machine_config.get("minio_endpoint") or ""
        cfg.MINIO_ACCESS_KEY = machine_config.get("minio_access_key") or ""
        cfg.MINIO_SECRET_KEY = machine_config.get("minio_secret_key") or ""
        cfg.MINIO_BUCKET = machine_config.get("minio_bucket") or "medicinestrip-ai"
        cfg.MINIO_SECURE = bool(machine_config.get("minio_secure") or False)

        # Download models if missing or version changed, then init pipeline
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: download_model(
                models_dir="models",
                gitlab_url=machine_config.get("gitlab_url") or "",
                gitlab_project_id=int(machine_config.get("gitlab_project_id") or 0),
                gitlab_token=machine_config.get("gitlab_token") or "",
                model_name=machine_config.get("model_name") or "medicinebox",
                model_version=machine_config.get("model_version") or "",
            ),
        )
        initialize_pipeline()

        await load_and_restore(self.state)

        store = get_image_store()
        store._executor.submit(store.migrate_existing)

    # -------------------------------------------------------------------------
    # WebSocket interface
    # -------------------------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new frontend WebSocket connection."""
        await connect_websocket(self.state, websocket)

    def disconnect(self, websocket: WebSocket | None = None) -> None:
        """Handle a frontend WebSocket disconnection and clear it from state."""
        disconnect_websocket(self.state, websocket)

    async def send(self, payload: dict[str, Any]) -> None:
        """Send a JSON payload to the connected frontend WebSocket."""
        await send_payload(self.state, payload)

    async def send_snapshot(self, event_type: str) -> None:
        """Build a full state snapshot and send it to the frontend.

        Args:
            event_type: Event name to embed in the snapshot payload.
        """
        logger.info(f"Sending snapshot with event_type={event_type}")
        snapshot = build_snapshot(self.state, event_type)
        logger.debug(
            f"Snapshot: event_type={event_type} products_count={len(self.state.products)}"
        )
        await self.send(snapshot)

    async def handle_message(self, payload: dict[str, Any]) -> None:
        """Dispatch an incoming WebSocket message to the appropriate handler.

        Recognised types: ``search``, ``camera_trigger``, ``ambiguous_resolved``,
        ``ambiguous_skipped``, ``next_packbox``, ``update_done_quantity``,
        ``submit``, ``ping``. Unknown types result in an error response.

        Args:
            payload: Parsed JSON message from the frontend.
        """
        message_type = payload.get("type")
        logger.info(f"Received websocket message of type={message_type}")

        if message_type == "search":
            await self._handle_search(payload)
        elif message_type == "camera_trigger":
            image_base64 = payload.get("image_base64", "")
            image_bytes = (
                decode_frame_from_payload(image_base64) if image_base64 else b""
            )
            try:
                machine_id = int(payload.get("machine_id") or self.state.machine_id)
            except (ValueError, TypeError):
                logger.warning(
                    f"Invalid machine_id in payload: {payload.get('machine_id')!r}"
                )
                machine_id = self.state.machine_id
            await self.process_camera_trigger(
                image_bytes=image_bytes,
                picknote=(payload.get("picknote") or "").strip(),
                machine_id=machine_id,
            )
        elif message_type == "ambiguous_resolved":
            await self._handle_ambiguous_resolved(payload)
        elif message_type == "ambiguous_skipped":
            await self._handle_ambiguous_skipped()
        elif message_type == "next_packbox":
            await self._handle_next_packbox()
        elif message_type == "update_done_quantity":
            await self._handle_update_done_quantity(payload)
        elif message_type == "submit":
            await self._handle_submit(payload)
        elif message_type == "ping":
            await self.send({"type": "pong"})
        else:
            logger.warning(f"Unsupported message type received: {message_type}")
            await self.send(
                {
                    "type": "error",
                    "message": f"Unsupported message type: {message_type}",
                }
            )

    async def process_camera_trigger(
        self,
        image_bytes: bytes,
        picknote: str = "",
        machine_id: int | None = None,
    ) -> dict[str, Any]:
        """Run the full detection-OCR-matching pipeline for a captured frame.

        Args:
            image_bytes: Raw image bytes from the camera.
            picknote: Picknote to associate with this scan; triggers a search
                refresh if it differs from the currently loaded one.
            machine_id: Override for the machine ID; defaults to the stored ID.

        Returns:
            A dict with ``status`` and stage-specific result fields.
        """
        trace_id = new_scan_trace()
        logger.bind(picknote=picknote or "").info(
            f"Camera scan started trace_id={trace_id} picknote={picknote!r}"
        )
        result = await self.pipeline.run(image_bytes, picknote, machine_id, trace_id)
        outcome = result.get("status", "unknown")
        logger.bind(picknote=picknote or "").info(
            f"Camera scan finished trace_id={trace_id} outcome={outcome}"
        )
        if outcome == "success":
            await save_state(self.state)
        return result

    async def process_barcode_scan(self, barcode: str) -> dict[str, Any]:
        """Process a barcode string from the hardware scanner (no camera image).

        Runs matching directly — skips detection and OCR. Does NOT trigger the
        conveyor since no physical trigger fired. Applies accepted/batch-correction
        quantities and updates the UI exactly like a camera scan would.

        Args:
            barcode: Raw barcode string read from the USB scanner.

        Returns:
            A dict with ``status`` and result fields.
        """
        import time as _time

        now = _time.monotonic()
        if barcode == self._last_barcode and (now - self._last_scan_time) < 2.0:
            logger.debug(f"Duplicate barcode ignored (debounce): {barcode!r}")
            return {"status": "ignored", "message": "Duplicate scan"}
        self._last_barcode = barcode
        self._last_scan_time = now

        _pn = self.state.picknote or ""
        if self._scan_lock.locked():
            logger.bind(picknote=_pn).warning(
                f"Scan already in progress — dropping barcode: {barcode!r}"
            )
            return {"status": "ignored", "message": "Scan in progress"}

        if self.state.ambiguous_pending:
            logger.bind(picknote=_pn).warning(
                f"Barcode scan dropped — ambiguous dialog is open: {barcode!r}"
            )
            return {
                "status": "ignored",
                "message": "Resolve the ambiguous dialog first",
            }

        async with self._scan_lock:
            trace_id = new_scan_trace()
            logger.bind(picknote=_pn).info(
                f"Barcode scan started trace_id={trace_id} barcode={barcode!r}"
            )

            if not self.state.picknote:
                message = "Picknote is not loaded"
                logger.bind(picknote=_pn).warning(
                    f"Barcode scan ignored — no picknote loaded: {barcode!r}"
                )
                await self.send({"type": "error", "message": message})
                return {"status": "error", "message": message}

            normalized = _normalize_barcode(barcode)
            gs1 = _extract_gs1_fields(normalized)
            batch = gs1.get("batch") or barcode
            product_code = gs1.get("product_code")
            logger.bind(picknote=_pn).info(
                f"Barcode normalized: {normalized!r} → "
                f"batch extracted: {batch!r} product_code: {product_code!r}"
            )
            ocr_results = [{"text": batch, "orientation": "barcode", "confidence": 1.0}]
            self.state.last_ocr_results = ocr_results

            result = await self.pipeline._run_matching_stage(
                ocr_results,
                trigger_conveyor=False,
                barcode_product_code=product_code,
                trace_id=trace_id,
            )
            outcome = result.get("status", "unknown")
            logger.bind(picknote=_pn).info(
                f"Barcode scan finished trace_id={trace_id} outcome={outcome}"
            )
            if outcome == "success":
                await save_state(self.state)
            return result

    # -------------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------------

    async def _handle_search(self, payload: dict[str, Any]) -> None:
        """Handle a ``search`` message by loading picknote data from the backend.

        Resets scan state, queries the backend, and broadcasts the result as a
        ``search_result`` snapshot. Sends an error event on failure.

        Args:
            payload: Message dict expected to contain ``picknote`` and optionally
                ``machine_id``.
        """
        picknote = (payload.get("picknote") or "").strip()
        if not picknote:
            logger.warning("Received search request without picknote")
            await self.send({"type": "error", "message": "Picknote is required"})
            return

        try:
            self.state.machine_id = int(
                payload.get("machine_id") or self.state.machine_id
            )
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid machine_id in search payload: {payload.get('machine_id')!r}"
            )
        logger.bind(picknote=picknote).info(
            f"Starting search for picknote={picknote} machine_id={self.state.machine_id}"
        )
        reset_scan_state(self.state)

        await self.send(
            {
                "type": "search_started",
                "picknote": picknote,
                "machine_id": self.state.machine_id,
            }
        )

        try:
            search_result = await self.backend.search_picknote(
                picknote, self.state.machine_id
            )
        except httpx.HTTPError as exc:
            logger.bind(picknote=picknote).exception("Picknote search failed")
            await self.send(
                {
                    "type": "error",
                    "message": f"Picknote search failed: {_http_error_message(exc)}",
                }
            )
            return

        if search_result.get("status") == "error":
            logger.bind(picknote=picknote).warning(
                f"Picknote search returned error for picknote={picknote}: "
                f"{search_result.get('message')}"
            )
            await self.send(
                {
                    "type": "error",
                    "message": search_result.get("message", "Picknote not found"),
                }
            )
            return

        self.state.picknote = picknote
        self.state.products = search_result.get("picknote_data", [])
        logger.bind(picknote=picknote).info(
            f"Picknote search succeeded for picknote={picknote} "
            f"products_count={len(self.state.products)}"
        )

        try:
            self.state.product_batch_list = await self.backend.get_product_batch_list(
                picknote
            )
            logger.bind(picknote=picknote).info(
                f"Loaded {len(self.state.product_batch_list)} batch entries "
                f"for batch-correction lookup (picknote={picknote})"
            )
        except Exception as exc:
            logger.bind(picknote=picknote).warning(
                f"Could not fetch product batch list for {picknote!r}: {exc}"
            )
            self.state.product_batch_list = []

        await save_state(self.state)
        await self.send_snapshot(event_type="search_result")

    async def _handle_ambiguous_resolved(self, payload: dict[str, Any]) -> None:
        """Handle an ``ambiguous_resolved`` message by applying the chosen product's scan.

        Determines whether this is a regular ambiguous match or a batch-correction
        ambiguous by comparing the last OCR text with the chosen batch number.
        Delegates quantity updates to the same pipeline methods used by normal and
        batch-correction scans so exhaustion fallback and batch_quantity=0 edge cases
        are handled identically.

        Args:
            payload: Message dict expected to contain ``batch_number``.
        """
        self.state.ambiguous_pending = False
        _pn = self.state.picknote or ""
        batch_number = (payload.get("batch_number") or "").strip()
        if not batch_number:
            logger.bind(picknote=_pn).warning(
                "ambiguous_resolved received without batch_number — rejecting"
            )
            conveyor = self.pipeline.conveyor
            if conveyor is not None:
                try:
                    conveyor.move_reject()
                except Exception as e:
                    logger.bind(picknote=_pn).exception(
                        "Failed to trigger conveyor on missing batch_number: {}", e
                    )
            return

        # Trust the frontend flag — it knows whether the candidate had _correction_batch set.
        is_regular = not payload.get("is_correction", False)

        # Use both batch_number and product_name to disambiguate when two products
        # share the same batch number.
        product_name_chosen = (payload.get("product_name") or "").strip().lower()

        if is_regular:
            # Regular ambiguous: find chosen candidate in last_matching_result so
            # _apply_matching_results can locate the product by row_index.
            chosen = next(
                (
                    r
                    for r in (self.state.last_matching_result or [])
                    if str(r.get("batch_number", "")).lower() == batch_number.lower()
                    and (
                        not product_name_chosen
                        or str(r.get("product_name", "")).lower() == product_name_chosen
                    )
                ),
                None,
            )
            if chosen is None:
                logger.bind(picknote=_pn).warning(
                    f"ambiguous_resolved: candidate not found for "
                    f"batch={batch_number!r} product={product_name_chosen!r} — rejecting"
                )
                conveyor = self.pipeline.conveyor
                if conveyor is not None:
                    try:
                        conveyor.move_reject()
                    except Exception as e:
                        logger.bind(picknote=_pn).exception(
                            "Failed to trigger conveyor on missing candidate: {}", e
                        )
                await self.send(
                    {
                        "type": "error",
                        "message": "Could not find selected product — scan rejected",
                    }
                )
                return
            apply_result = self.pipeline._apply_matching_results(chosen)
            kind = "ambiguous"
        else:
            # Batch-correction ambiguous: the user confirmed which product the
            # scanned (non-picklist) batch belongs to.
            product = next(
                (
                    p
                    for p in self.state.products
                    if str(p.get("batch_number", "")).lower() == batch_number.lower()
                    and (
                        not product_name_chosen
                        or str(p.get("product_name", "")).lower() == product_name_chosen
                    )
                ),
                None,
            )
            if product is None:
                logger.bind(picknote=_pn).warning(
                    f"ambiguous_resolved: batch_number={batch_number!r} product={product_name_chosen!r} not found in products — rejecting"
                )
                conveyor = self.pipeline.conveyor
                if conveyor is not None:
                    try:
                        conveyor.move_reject()
                    except Exception as e:
                        logger.bind(picknote=_pn).exception(
                            "Failed to trigger conveyor on product not found: {}", e
                        )
                return
            correction_batch = next(
                (
                    r.get("_correction_batch", "")
                    for r in (self.state.last_matching_result or [])
                    if str(r.get("batch_number", "")).lower() == batch_number.lower()
                    and (
                        not product_name_chosen
                        or str(r.get("product_name", "")).lower() == product_name_chosen
                    )
                ),
                "",
            )
            if not correction_batch:
                logger.bind(picknote=_pn).warning(
                    f"ambiguous_resolved: _correction_batch missing for "
                    f"batch={batch_number!r} product={product_name_chosen!r} — rejecting"
                )
                conveyor = self.pipeline.conveyor
                if conveyor is not None:
                    try:
                        conveyor.move_reject()
                    except Exception as e:
                        logger.bind(picknote=_pn).exception(
                            "Failed to trigger conveyor on missing correction_batch: {}",
                            e,
                        )
                return
            apply_result = self.pipeline._apply_batch_correction(
                correction_batch, product.get("product_name", "")
            )
            kind = "batch_correction_ambiguous"

        if apply_result.get("status") == "exhausted":
            product_name = apply_result.get("product_name", "Unknown Product")
            logger.bind(picknote=_pn).warning(
                f"ambiguous_resolved: scan_exhausted product={product_name!r}"
            )
            conveyor = self.pipeline.conveyor
            if conveyor is not None:
                try:
                    conveyor.move_reject()
                except Exception as e:
                    logger.bind(picknote=_pn).exception(
                        "Failed to trigger conveyor on exhausted: {}", e
                    )
            await self.send(
                {
                    "type": "alert",
                    "message": f"No more pending qty for product {product_name}",
                }
            )
            await save_state(self.state)
            await self.send_snapshot(event_type="scan_exhausted")
            return

        logger.bind(picknote=self.state.picknote or "").info(
            f"ambiguous_resolved picknote={self.state.picknote} "
            f"batch={batch_number!r} kind={kind}"
        )
        conveyor = self.pipeline.conveyor
        if conveyor is not None:
            try:
                conveyor.move_accept()
            except Exception as e:
                logger.bind(picknote=_pn).exception("Failed to trigger conveyor: {}", e)
        await save_state(self.state)
        await self.send_snapshot(event_type="scan_result")

    async def _handle_ambiguous_skipped(self) -> None:
        """Move the conveyor to the reject lane when the user dismisses the ambiguous dialog."""
        self.state.ambiguous_pending = False
        _pn = self.state.picknote or ""
        logger.bind(picknote=_pn).info(
            f"ambiguous_skipped picknote={self.state.picknote} — triggering conveyor reject"
        )
        conveyor = self.pipeline.conveyor
        if conveyor is not None:
            try:
                conveyor.move_reject()
            except Exception as e:
                logger.bind(picknote=_pn).exception(
                    "Failed to trigger conveyor on ambiguous skip: {}", e
                )
        await save_state(self.state)
        await self.send_snapshot(event_type="scan_skipped")

    async def _handle_submit(self, payload: dict[str, Any]) -> None:
        """Forward a picknote submission to the backend and reply with the result."""
        request_id = payload.get("request_id")
        body = {
            "picknote": payload.get("picknote"),
            "machine_id": payload.get("machine_id") or self.state.machine_id,
            "party_name": payload.get("party_name"),
            "store_code": payload.get("store_code"),
            "products": payload.get("products", []),
        }
        try:
            result = await self.backend.submit_picknote(body)
        except httpx.HTTPStatusError as exc:
            detail: Any
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = None
            await self.send(
                {
                    "type": "submit_result",
                    "request_id": request_id,
                    "ok": False,
                    "detail": detail or _http_error_message(exc),
                }
            )
            return
        except httpx.HTTPError as exc:
            logger.bind(picknote=body.get("picknote") or "").exception(
                "Picknote submit failed"
            )
            await self.send(
                {
                    "type": "submit_result",
                    "request_id": request_id,
                    "ok": False,
                    "detail": _http_error_message(exc),
                }
            )
            return

        await self.send(
            {
                "type": "submit_result",
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        )
        reset_scan_state(self.state)
        await save_state(self.state)
        logger.bind(picknote=body.get("picknote") or "").info(
            f"picknote_submitted picknote={body.get('picknote')!r}"
        )

    async def _handle_next_packbox(self) -> None:
        """Increment the current packbox counter and broadcast a snapshot."""
        if not self.state.picknote:
            logger.warning("next_packbox received without a loaded picknote")
            return
        self.state.current_packbox += 1
        logger.bind(picknote=self.state.picknote or "").info(
            f"packbox_advanced picknote={self.state.picknote} "
            f"new_packbox={self.state.current_packbox}"
        )
        await save_state(self.state)
        await self.send_snapshot(event_type="packbox_changed")

    async def _handle_update_done_quantity(self, payload: dict[str, Any]) -> None:
        """Apply a manual Done Qty edit from the frontend to a product row.

        Clamps the value to [0, batch_quantity] when batch_quantity > 0 and
        recomputes pending_quantity, then persists and broadcasts a snapshot.
        scan_log is left untouched — it records physical scans only.
        """
        try:
            row_index = int(payload["row_index"])
            done_quantity = int(payload["done_quantity"])
        except (KeyError, TypeError, ValueError):
            await self.send(
                {"type": "error", "message": "Invalid done quantity update"}
            )
            return

        product = next(
            (
                p
                for p in self.state.products
                if _safe_int(p.get("row_index")) == row_index
            ),
            None,
        )
        if product is None:
            await self.send({"type": "error", "message": f"Row {row_index} not found"})
            return

        batch_qty = _safe_int(product.get("batch_quantity"))
        done_quantity = max(done_quantity, 0)
        if batch_qty > 0:
            done_quantity = min(done_quantity, batch_qty)
            product["pending_quantity"] = max(batch_qty - done_quantity, 0)
        else:
            product["pending_quantity"] = 0
        product["done_quantity"] = done_quantity

        logger.bind(picknote=self.state.picknote or "").info(
            f"done_quantity_manual_update row_index={row_index} "
            f"product={product.get('product_name')!r} "
            f"batch={product.get('batch_number')!r} "
            f"done={done_quantity} batch_qty={batch_qty}"
        )
        await save_state(self.state)
        await self.send_snapshot(event_type="done_quantity_updated")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _set_config_error(self, status: ConfigStatus, message: str) -> None:
        """Store a config error status and message, clearing any previously loaded config.

        Args:
            status: The config status to set (e.g. ``"missing"`` or ``"error"``).
            message: Human-readable error description.
        """
        self.state.machine_config = None
        self.state.config_status = status
        self.state.config_error = message
