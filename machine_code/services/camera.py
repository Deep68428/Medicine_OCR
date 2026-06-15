from __future__ import annotations

import base64
import binascii

from loguru import logger


def decode_frame_from_payload(image_base64: str) -> bytes:
    """Decode a base64 image string into raw bytes, stripping any data-URL prefix.

    Args:
        image_base64: A plain base64 string or a data-URL of the form
            ``data:<mime>;base64,<data>``.

    Returns:
        The decoded image as bytes, or empty bytes if decoding fails.
    """
    payload = image_base64
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        return base64.b64decode(payload)
    except (binascii.Error, ValueError) as exc:
        logger.error(f"Failed to decode base64 image: {exc}")
        return b""


def encode_frame_for_frontend(image_bytes: bytes) -> str:
    """Encode raw image bytes as an ASCII base64 string for transmission to the frontend.

    Args:
        image_bytes: Raw image data (e.g. JPEG bytes).

    Returns:
        Base64-encoded ASCII string.
    """
    return base64.b64encode(image_bytes).decode("ascii")
