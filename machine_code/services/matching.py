from __future__ import annotations
import re
from typing import Any
from loguru import logger


def _normalize(text: str) -> str:
    """Uppercase, replace OCR noise characters, collapse whitespace."""
    text = text.upper()
    text = re.sub(r"[?µμ]", "", text)  # strip ? and mu-signs
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Visually similar characters that OCR frequently confuses.
# Both sides of a comparison are mapped to the same canonical form so that
# e.g. DB batch "LGP12" and OCR reading "L6P12" both become "L6P12" → match.
_CHAR_SWAP_TABLE = str.maketrans(
    {
        "O": "0",
        "V": "U",
        "G": "6",
        "B": "8",
    }
)


def _swap_normalize(text: str) -> str:
    return text.translate(_CHAR_SWAP_TABLE)


def _token_match(batch: str, ocr: str) -> bool:
    """True if batch appears in ocr as an exact bounded token.

    Also matches after applying _swap_normalize to both sides to handle common
    OCR confusions (O/0, V/U, G/6, B/8).
    """
    n = len(batch)
    if n == 0 or n > len(ocr):
        return False
    for i in range(len(ocr) - n + 1):
        before_ok = i == 0 or not ocr[i - 1].isalnum()
        after_ok = (i + n == len(ocr)) or not ocr[i + n].isalnum()
        if before_ok and after_ok and batch == ocr[i : i + n]:
            return True
    # Retry with visually-similar character substitutions applied to both sides
    s_batch = _swap_normalize(batch)
    s_ocr = _swap_normalize(ocr)
    n2 = len(s_batch)
    if n2 == 0 or n2 > len(s_ocr):
        return False
    for i in range(len(s_ocr) - n2 + 1):
        before_ok = i == 0 or not s_ocr[i - 1].isalnum()
        after_ok = (i + n2 == len(s_ocr)) or not s_ocr[i + n2].isalnum()
        if before_ok and after_ok and s_batch == s_ocr[i : i + n2]:
            return True
    return False


# ---------------------------------------------------------------------------
# Secondary disambiguation helpers (exp date + MRP exact match)
# ---------------------------------------------------------------------------

_MONTH_ABBR: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def _extract_dates(text: str) -> set[tuple[int, int]]:
    """Return all (month, year) tuples found in an OCR string.

    Handles:
      MM/YYYY, MM-YYYY, MM.YYYY  →  02/2028
      YYYY/MM, YYYY-MM           →  2028/02
      MM/YY, MM-YY, MM.YY        →  02/28  (normalised to 20YY)
      DD/MM/YYYY                 →  01/02/2028  (day ignored)
      MMM YYYY, MMM.YYYY, MMMYYYY, MMM-YYYY, MMM:YYYY, MMM_YYYY
      MMM-YY, MMM.YY             →  FEB 2028 / APR-26
      Noise-tolerant: MMM?YYYY, MMM.?YYYY, etc.
    """
    results: set[tuple[int, int]] = set()
    upper = text.upper()

    # DD/MM/YYYY — must come before MM/YYYY to avoid partial overlap
    for m in re.finditer(r"\b\d{1,2}[/\-.:](\d{1,2})[/\-.:](\d{4})\b", text):
        mo, yr = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 2000 <= yr <= 2100:
            results.add((mo, yr))

    # MM/YYYY or MM-YYYY or MM.YYYY
    for m in re.finditer(r"\b(\d{1,2})[/\-.](\d{4})\b", text):
        mo, yr = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 2000 <= yr <= 2100:
            results.add((mo, yr))

    # YYYY/MM or YYYY-MM
    for m in re.finditer(r"\b(\d{4})[/\-.](\d{1,2})\b", text):
        yr, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 2000 <= yr <= 2100:
            results.add((mo, yr))

    # MM/YY or MM-YY or MM.YY → 20YY
    for m in re.finditer(r"\b(\d{1,2})[/\-.](\d{2})\b", text):
        mo, yy = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            results.add((mo, 2000 + yy))

    # MMM<sep>YYYY and MMM<sep>YY — sep = space / . / - / : / _ / ? or nothing
    # Also tolerates a noise char like "." followed by optional "?" (e.g. FEB.?2028)
    _SEP = r"[.\-:_ ?]*"
    for m in re.finditer(
        r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)" + _SEP + r"(\d{4})\b",
        upper,
    ):
        mo = _MONTH_ABBR[m.group(1)]
        yr = int(m.group(2))
        if 2000 <= yr <= 2100:
            results.add((mo, yr))

    # MMM<sep>YY → 20YY  (e.g. APR-26, MAR.28)
    for m in re.finditer(
        r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)" + _SEP + r"(\d{2})\b",
        upper,
    ):
        mo = _MONTH_ABBR[m.group(1)]
        yy = int(m.group(2))
        results.add((mo, 2000 + yy))

    return results


def _parse_product_date(date_str: str | None) -> tuple[int, int] | None:
    """Parse a DB date string to (month, year).

    Supports YYYY-MM-DD, DD/MM/YYYY, and MM/YYYY.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{1,2})-\d{1,2}$", s)
    if m:
        return int(m.group(2)), int(m.group(1))
    # DD/MM/YYYY
    m = re.match(r"^\d{1,2}/(\d{1,2})/(\d{4})$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    # MM/YYYY
    m = re.match(r"^(\d{1,2})/(\d{4})$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _extract_mrp_values(text: str) -> set[float]:
    """Extract MRP-like numeric values from an OCR string.

    Looks for numbers after MRP/₹/RS/INR keywords, and standalone
    values with 1-2 decimal places (e.g. 34.00 or 34.0).
    Handles noise chars between keyword and value (e.g. RS.?5.29),
    full-width colons (：), comma decimals (50,00), and leading dots (.50.00).
    """
    results: set[float] = set()

    # Normalise full-width colon and strip leading dots
    cleaned = text.replace("：", " ").replace("：", " ")

    # After explicit keyword — allow noise chars between keyword and digits
    for m in re.finditer(
        r"(?:MRP|₹|RS\.?|INR)[^0-9]{0,5}(\d{1,6}[.,]\d{1,2})",
        cleaned,
        re.IGNORECASE,
    ):
        raw = m.group(1).replace(",", ".")
        results.add(round(float(raw), 2))

    # Standalone NN.NN or NN.N — 1 or 2 decimal places
    for m in re.finditer(r"\b(\d{1,5}\.\d{1,2})\b", cleaned):
        val = round(float(m.group(1)), 2)
        if 1.0 <= val <= 10000.0:
            results.add(val)

    # Comma-decimal standalone  e.g. "50,00"
    for m in re.finditer(r"\b(\d{1,5}),(\d{2})\b", cleaned):
        val = round(float(f"{m.group(1)}.{m.group(2)}"), 2)
        if 1.0 <= val <= 10000.0:
            results.add(val)

    return results


def _try_product_code_disambiguation(
    matches: list[dict[str, Any]],
    barcode_product_code: str,
) -> dict[str, Any] | None:
    """Filter ambiguous candidates by exact product_code match from the barcode.

    Leading zeros are stripped from both sides to handle GS1 zero-padding.
    Returns the single matching candidate, or None if still ambiguous.
    """
    norm_code = barcode_product_code.strip().lstrip("0")
    if not norm_code:
        return None

    filtered = [
        c
        for c in matches
        if str(c.get("product_code") or "").strip().lstrip("0") == norm_code
    ]

    if len(filtered) == 1:
        logger.info(
            f"Product code disambiguation resolved: "
            f"code={norm_code!r} → batch={filtered[0].get('batch_number')!r}"
        )
        return filtered[0]

    logger.info(
        f"Product code disambiguation inconclusive: "
        f"code={norm_code!r} matched {len(filtered)} candidates"
    )
    return None


def _try_secondary_disambiguation(
    matches: list[dict[str, Any]],
    ocr_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Try to reduce ambiguous candidates to one using exp_date and MRP exact matching.

    Both exp_date and MRP must match when both are detectable in the OCR output.
    If only one signal is present, that signal alone is used.
    Returns the single resolved candidate, or None if still ambiguous.
    """
    ocr_texts = [r.get("text", "") for r in ocr_results if r.get("text")]
    if not ocr_texts:
        return None

    all_dates: set[tuple[int, int]] = set()
    all_mrp: set[float] = set()
    for t in ocr_texts:
        all_dates |= _extract_dates(t)
        all_mrp |= _extract_mrp_values(t)

    have_dates = bool(all_dates)
    have_mrp = bool(all_mrp)

    if not have_dates and not have_mrp:
        return None

    filtered = []
    for candidate in matches:
        exp_match = True
        mrp_match = True

        if have_dates:
            exp_parsed = _parse_product_date(candidate.get("expiry_date"))
            exp_match = exp_parsed is not None and exp_parsed in all_dates

        if have_mrp:
            product_mrp = candidate.get("mrp")
            mrp_match = (
                product_mrp is not None and round(float(product_mrp), 2) in all_mrp
            )

        if exp_match and mrp_match:
            filtered.append(candidate)

    if len(filtered) == 1:
        logger.info(
            f"Secondary disambiguation resolved: "
            f"batch={filtered[0].get('batch_number')!r} "
            f"exp={filtered[0].get('expiry_date')!r} "
            f"mrp={filtered[0].get('mrp')!r} "
            f"ocr_dates={all_dates} ocr_mrp={all_mrp}"
        )
        return filtered[0]

    logger.info(
        f"Secondary disambiguation inconclusive: "
        f"{len(filtered)} candidates remain "
        f"(ocr_dates={all_dates} ocr_mrp={all_mrp})"
    )
    return None


# ---------------------------------------------------------------------------
# Main matching entry point
# ---------------------------------------------------------------------------


def run_matching(
    ocr_results: list[dict[str, Any]],
    products: list[dict[str, Any]],
    preview_image: str | None = None,
    product_batch_list: list[dict[str, Any]] | None = None,
    barcode_product_code: str | None = None,
) -> dict[str, Any]:
    if not products:
        logger.info("No products loaded — matching rejected")
        return {
            "status": "error",
            "state": "rejected",
            "message": "No products loaded",
            "matching_result": [],
            "preview_image": [],
        }

    ocr_texts = [_normalize(r.get("text", "")) for r in ocr_results if r.get("text")]
    logger.info(f"OCR texts for matching: {ocr_texts}")

    matches = []
    for product in products:
        batch_number = _normalize(product.get("batch_number") or "")
        if not batch_number:
            continue
        if any(_token_match(batch_number, ocr) for ocr in ocr_texts):
            matches.append(product)

    if len(matches) == 1:
        logger.info(f"Single match found: {matches[0]}")
        # add ocr_texts in preview_image
        return {
            "status": "success",
            "state": "accepted",
            "matching_result": matches,
            "preview_image": [preview_image],
        }

    if len(matches) > 1:
        # 1. Product code from barcode (most precise — try first)
        if barcode_product_code:
            resolved = _try_product_code_disambiguation(matches, barcode_product_code)
            if resolved is not None:
                return {
                    "status": "success",
                    "state": "accepted",
                    "matching_result": [resolved],
                    "preview_image": [preview_image],
                }
        # 2. Exp date + MRP from OCR text
        resolved = _try_secondary_disambiguation(matches, ocr_results)
        if resolved is not None:
            return {
                "status": "success",
                "state": "accepted",
                "matching_result": [resolved],
                "preview_image": [preview_image],
            }
        logger.info(f"Ambiguous match: {len(matches)} candidates")
        return {
            "status": "success",
            "state": "ambiguous",
            "matching_result": matches,
            "preview_image": [preview_image] * len(matches),
        }

    # Batch not in picklist — check if it belongs to any picklist product via full batch list
    if product_batch_list:
        picklist_names = {
            _normalize(p.get("product_name") or "")
            for p in products
            if p.get("product_name")
        }
        correction_hits: list[dict] = []
        seen_keys: set[str] = set()
        for candidate in ocr_results:
            norm_ocr = _normalize(candidate.get("text", ""))
            if not norm_ocr:
                continue
            for entry in product_batch_list:
                entry_batch = _normalize(entry.get("batch_number") or "")
                if not entry_batch:
                    continue
                if _token_match(entry_batch, norm_ocr):
                    matched_name = entry.get("product_name") or ""
                    norm_matched = _normalize(matched_name)
                    if norm_matched in picklist_names:
                        dedup_key = f"{entry_batch}||{norm_matched}"
                        if dedup_key not in seen_keys:
                            seen_keys.add(dedup_key)
                            correction_hits.append(
                                {
                                    "entry": entry,
                                    "matched_name": matched_name,
                                }
                            )

        if correction_hits:
            # All hits resolve to the same product → single batch correction
            unique_products = {_normalize(h["matched_name"]) for h in correction_hits}
            if len(unique_products) == 1:
                # Prefer the most specific (longest) batch number to avoid short
                # fragments like expiry dates ("1/2026") winning over real batch IDs.
                correction_hits.sort(
                    key=lambda h: len(h["entry"].get("batch_number") or ""),
                    reverse=True,
                )
                hit = correction_hits[0]
                logger.info(
                    f"Batch correction match: batch={hit['entry'].get('batch_number')!r} "
                    f"→ product={hit['matched_name']!r}"
                )
                return {
                    "status": "success",
                    "state": "batch_correction",
                    "scanned_batch": hit["entry"].get("batch_number", ""),
                    "matched_product_name": hit["matched_name"],
                    "matching_result": [],
                    "preview_image": [preview_image] if preview_image else [],
                }

            # Multiple different products — surface as ambiguous
            logger.info(
                f"Ambiguous batch correction: {len(correction_hits)} candidates "
                f"across {len(unique_products)} products"
            )
            ambiguous_products = []
            seen_names: set[str] = set()
            for h in correction_hits:
                norm = _normalize(h["matched_name"])
                if norm not in seen_names:
                    seen_names.add(norm)
                    matched_batch = h["entry"].get("batch_number", "")
                    for p in products:
                        if _normalize(p.get("product_name") or "") == norm:
                            candidate = dict(p)
                            candidate["_correction_batch"] = matched_batch
                            ambiguous_products.append(candidate)
                            break
            return {
                "status": "success",
                "state": "ambiguous",
                "matching_result": ambiguous_products,
                "preview_image": [preview_image] * len(ambiguous_products),
            }

    logger.info("Batch number not found")
    return {
        "status": "error",
        "state": "rejected",
        "message": "Batch number not found",
        "matching_result": [],
        "preview_image": [],
    }
