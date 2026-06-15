from __future__ import annotations

import re
import time

import cv2
import numpy as np
from loguru import logger

GS = chr(29)

try:
    from pylibdmtx.pylibdmtx import decode as _dmtx_decode

    _DMTX_AVAILABLE = True
except ImportError:
    _DMTX_AVAILABLE = False
    logger.warning("pylibdmtx not found — DataMatrix barcode decoding disabled")


def _decode_fast(img: np.ndarray) -> tuple[list, str]:
    """Try multiple preprocessing variants to maximise decode success rate."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img

    attempts = [
        ("original", img),
        ("gray", gray),
        ("small", cv2.resize(gray, (0, 0), fx=0.5, fy=0.5)),
    ]

    for name, attempt in attempts:
        _t = time.perf_counter()
        decoded = _dmtx_decode(attempt, timeout=300)  # 300ms native C-library timeout
        elapsed_ms = int((time.perf_counter() - _t) * 1000)
        if decoded:
            logger.info("Barcode decoded on attempt={} in {}ms", name, elapsed_ms)
            return decoded, name
        logger.debug("Barcode attempt={} no result in {}ms", name, elapsed_ms)

    return [], "fail"


def _normalize_barcode(barcode: str) -> str:
    """Collapse GS1 separator variants and strip trailing quantity suffix.

    Also strips parenthesized AI notation (e.g. "(02) GTIN (10) BATCH")
    and internal spaces so the rest of the parsing sees a clean string.
    """
    # Strip parenthesized AI labels: (02), (10), (37), etc.
    barcode = re.sub(r"\(\d{2,3}\)\s*", "", barcode)

    index = barcode.rfind(chr(0x20))
    if index > 0:
        barcode = barcode[:index].strip() + GS + "37"

    index = barcode.rfind("_")
    if index > 0:
        barcode = barcode[:index].strip() + GS + "37"

    index = barcode.rfind("37")
    if index > 0:
        barcode = barcode[:index].strip()

    return barcode


def _extract_batch_gs1(barcode: str) -> str | None:
    """Extract batch number using GS1 Application Identifier 10 (batch/lot)."""
    return _extract_gs1_fields(barcode).get("batch")


def _extract_gs1_fields(barcode: str) -> dict[str, str | None]:
    """Extract batch number and product code from a GS1 barcode string.

    AI 02 carries a 14-character GTIN (fixed length). The last 4 characters of
    that GTIN are the product code. AI 10 (batch/lot) immediately follows the
    GTIN field. Using the fixed length of AI 02 ensures the correct AI 10 is
    found even when the batch number itself contains the substring "10".

    Returns a dict with keys:
        ``batch``        — lot/batch string (str or None)
        ``product_code`` — 4-char product code from GTIN tail (str or None)
    """
    result: dict[str, str | None] = {"batch": None, "product_code": None}

    ai02_pos = barcode.find("02")
    if ai02_pos != -1:
        gtin_start = ai02_pos + 2
        gtin_end = gtin_start + 14  # AI 02 GTIN is always 14 chars
        if len(barcode) >= gtin_end + 2 and barcode[gtin_end : gtin_end + 2] == "10":
            result["product_code"] = barcode[
                gtin_end - 4 : gtin_end
            ]  # 4 chars before AI 10
            batch_start = gtin_end + 2
            gs_pos = barcode.find(GS, batch_start)
            batch_end = gs_pos if gs_pos != -1 else len(barcode)
            result["batch"] = barcode[batch_start:batch_end].strip() or None
            return result

    # Fallback: first-occurrence find for barcodes without AI 02.
    # Guard against plain EAN-13/EAN-8 product codes (all-digits, ≤13 chars)
    # which falsely match "10" as a substring but contain no GS1 batch data.
    is_plain_ean = barcode.isdigit() and len(barcode) <= 13
    if not is_plain_ean and "10" in barcode:
        start = barcode.find("10") + 2
        gs_pos = barcode.find(GS, start)
        end = gs_pos if gs_pos != -1 else len(barcode)
        result["batch"] = barcode[start:end].strip() or None

    return result


def decode_barcode_crop(img: np.ndarray) -> str | None:
    """Decode a DataMatrix barcode crop and extract the GS1 batch/lot number.

    Returns the batch string on success, or None if decode fails or no batch
    application identifier (AI 10) is found.
    """
    if not _DMTX_AVAILABLE:
        return None

    decoded_objects, attempt_name = _decode_fast(img)
    if not decoded_objects:
        logger.debug("Barcode decode: no DataMatrix found (attempt={})", attempt_name)
        return None

    for obj in decoded_objects:
        raw = obj.data.decode(errors="ignore")
        logger.debug("Barcode raw: {!r} (attempt={})", raw, attempt_name)

        normalized = _normalize_barcode(raw)
        batch = _extract_batch_gs1(normalized)
        if batch:
            logger.info("Barcode batch extracted: {!r}", batch)
            return batch

    logger.debug("Barcode decoded but no GS1 batch (AI 10) found")
    return None
