from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import WebSocket

ConfigStatus = Literal["idle", "loading", "loaded", "missing", "error"]
MatchState = Literal["accepted", "ambiguous", "rejected", "batch_correction"]

PIPELINE_STAGES: tuple[str, ...] = ("detection", "ocr", "matching")


@dataclass
class MachineState:
    """Holds all runtime state for a single machine controller session."""

    websocket: WebSocket | None = None
    machine_id: int = 1
    machine_config: dict[str, Any] | None = None
    config_status: ConfigStatus = "idle"
    config_error: str | None = None
    picknote: str | None = None
    products: list[dict[str, Any]] = field(default_factory=list)
    product_batch_list: list[dict[str, Any]] = field(default_factory=list)
    latest_image: str | None = None
    current_product: dict[str, Any] | None = None
    errors: dict[str, str | None] = field(
        default_factory=lambda: dict.fromkeys(PIPELINE_STAGES)
    )
    last_detection_results: list[dict[str, Any]] = field(default_factory=list)
    last_ocr_results: list[dict[str, Any]] = field(default_factory=list)
    last_matching_result: list[dict[str, Any]] = field(default_factory=list)
    current_packbox: int = 1
    ambiguous_pending: bool = False


def reset_scan_state(state: MachineState) -> None:
    """Clear all scan-related fields on the state, leaving config and connection intact."""
    state.picknote = None
    state.products = []
    state.latest_image = None
    state.current_product = None
    state.errors = dict.fromkeys(PIPELINE_STAGES)
    state.product_batch_list = []
    state.last_detection_results = []
    state.last_ocr_results = []
    state.last_matching_result = []
    state.current_packbox = 1
    state.ambiguous_pending = False


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def move_product_to_front(state: MachineState, product: dict[str, Any]) -> None:
    """Move the given product to the front of state.products so display order is preserved on restart."""
    idx = next((i for i, p in enumerate(state.products) if p is product), -1)
    if idx > 0:
        state.products.insert(0, state.products.pop(idx))


def build_stats(products: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate total, pending, done, and completed quantities across all products.

    ``completed`` counts individual line-item rows where done_quantity >= batch_quantity.
    ``done`` is the sum of all done_quantity values across the picklist.

    Args:
        products: List of product dicts, each expected to have batch_quantity,
            pending_quantity, done_quantity, and product_name fields.

    Returns:
        A dict with keys ``total``, ``pending``, ``done``, and ``completed``.
    """
    name_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"qty": 0, "done": 0, "pending": 0}
    )
    for p in products:
        n = p.get("product_name") or ""
        name_totals[n]["qty"] += _safe_int(p.get("batch_quantity"))
        name_totals[n]["done"] += _safe_int(p.get("done_quantity"))
        name_totals[n]["pending"] += _safe_int(p.get("pending_quantity"))
    return {
        "total": sum(v["qty"] for v in name_totals.values()),
        "pending": sum(v["pending"] for v in name_totals.values()),
        "done": sum(v["done"] for v in name_totals.values()),
        "completed": sum(
            1
            for p in products
            if _safe_int(p.get("batch_quantity")) > 0
            and _safe_int(p.get("done_quantity")) >= _safe_int(p.get("batch_quantity"))
        ),
    }


def build_snapshot(state: MachineState, event_type: str) -> dict[str, Any]:
    """Build a full state snapshot dict suitable for sending to the frontend.

    Args:
        state: The current MachineState instance.
        event_type: The event name to embed in the payload (e.g. ``"scan_result"``).

    Returns:
        A serialisable dict representing the complete current state.
    """
    party_name = None
    store_code = None
    if state.products:
        party_name = state.products[0].get("party_name")
        store_code = state.products[0].get("store_code")

    return {
        "type": event_type,
        "picknote": state.picknote,
        "machine_id": state.machine_id,
        "machine_config": state.machine_config,
        "config_status": state.config_status,
        "config_error": state.config_error,
        "party_name": party_name,
        "store_code": store_code,
        "products": state.products,
        "stats": build_stats(state.products),
        "current_product": state.current_product,
        "image": state.latest_image,
        "errors": state.errors,
        "matching_result": state.last_matching_result,
        "current_packbox": state.current_packbox,
    }
