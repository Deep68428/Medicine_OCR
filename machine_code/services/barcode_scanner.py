from __future__ import annotations

import asyncio
import select
import threading
from collections.abc import Awaitable, Callable

from loguru import logger

KEY_MAP = {
    "KEY_0": "0",
    "KEY_1": "1",
    "KEY_2": "2",
    "KEY_3": "3",
    "KEY_4": "4",
    "KEY_5": "5",
    "KEY_6": "6",
    "KEY_7": "7",
    "KEY_8": "8",
    "KEY_9": "9",
    "KEY_A": "a",
    "KEY_B": "b",
    "KEY_C": "c",
    "KEY_D": "d",
    "KEY_E": "e",
    "KEY_F": "f",
    "KEY_G": "g",
    "KEY_H": "h",
    "KEY_I": "i",
    "KEY_J": "j",
    "KEY_K": "k",
    "KEY_L": "l",
    "KEY_M": "m",
    "KEY_N": "n",
    "KEY_O": "o",
    "KEY_P": "p",
    "KEY_Q": "q",
    "KEY_R": "r",
    "KEY_S": "s",
    "KEY_T": "t",
    "KEY_U": "u",
    "KEY_V": "v",
    "KEY_W": "w",
    "KEY_X": "x",
    "KEY_Y": "y",
    "KEY_Z": "z",
    "KEY_MINUS": "-",
    "KEY_EQUAL": "=",
    "KEY_SLASH": "/",
    "KEY_DOT": ".",
    "KEY_COMMA": ",",
    "KEY_SPACE": " ",
}

SHIFT_MAP = {
    "KEY_1": "!",
    "KEY_2": "@",
    "KEY_3": "#",
    "KEY_4": "$",
    "KEY_5": "%",
    "KEY_6": "^",
    "KEY_7": "&",
    "KEY_8": "*",
    "KEY_9": "(",
    "KEY_0": ")",
    "KEY_MINUS": "_",
    "KEY_EQUAL": "+",
    "KEY_SLASH": "?",
    "KEY_DOT": ">",
    "KEY_COMMA": "<",
    "KEY_A": "A",
    "KEY_B": "B",
    "KEY_C": "C",
    "KEY_D": "D",
    "KEY_E": "E",
    "KEY_F": "F",
    "KEY_G": "G",
    "KEY_H": "H",
    "KEY_I": "I",
    "KEY_J": "J",
    "KEY_K": "K",
    "KEY_L": "L",
    "KEY_M": "M",
    "KEY_N": "N",
    "KEY_O": "O",
    "KEY_P": "P",
    "KEY_Q": "Q",
    "KEY_R": "R",
    "KEY_S": "S",
    "KEY_T": "T",
    "KEY_U": "U",
    "KEY_V": "V",
    "KEY_W": "W",
    "KEY_X": "X",
    "KEY_Y": "Y",
    "KEY_Z": "Z",
}


def _find_scanner() -> str | None:
    try:
        import glob
        import evdev
    except ImportError:
        return None
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            dev = evdev.InputDevice(path)
            if "newtologic" in dev.name.lower() or "4010" in dev.name.lower():
                return path
        except Exception:
            continue
    return None


class BarcodeScanner:
    """Background thread that reads barcodes from a Newtologic 4010E USB scanner
    and fires an async callback for each complete barcode."""

    def __init__(
        self,
        on_barcode: Callable[[str], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._on_barcode = on_barcode
        self._loop = loop
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start_async(self) -> None:
        path = _find_scanner()
        if path is None:
            logger.warning("No barcode scanner found — hardware scanner unavailable")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(path,), daemon=True, name="barcode-scanner"
        )
        self._thread.start()
        logger.info(f"Barcode scanner thread started for device={path}")

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self, path: str) -> None:
        try:
            import evdev
            from evdev import categorize, ecodes
        except ImportError:
            logger.error("evdev not available — scanner thread exiting")
            return

        disconnected_logged = False

        while not self._stop_event.is_set():
            try:
                device = evdev.InputDevice(path)
                try:
                    device.grab()
                except PermissionError:
                    logger.warning(
                        "Could not grab scanner device — run: sudo chmod a+rw /dev/input/event* to stop keystrokes leaking to terminal"
                    )
                logger.info(f"Barcode scanner connected: {device.name} ({device.path})")
                disconnected_logged = False
                barcode = ""
                shift = False

                while not self._stop_event.is_set():
                    r, _, _ = select.select([device], [], [], 1.0)
                    if not r:
                        continue
                    for event in device.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        key_event = categorize(event)

                        key = key_event.keycode
                        if isinstance(key, list):
                            key = key[0]

                        if key in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
                            shift = key_event.keystate == key_event.key_down
                            continue

                        if key_event.keystate != key_event.key_down:
                            continue

                        if key == "KEY_ENTER":
                            if barcode:
                                logger.info(f"Barcode scanned: {barcode!r}")
                                asyncio.run_coroutine_threadsafe(
                                    self._on_barcode(barcode),
                                    self._loop,
                                )
                            barcode = ""
                            shift = False
                            continue

                        char = SHIFT_MAP.get(key) if shift else KEY_MAP.get(key)
                        if char:
                            barcode += char

            except OSError as exc:
                if self._stop_event.is_set():
                    break
                if not disconnected_logged:
                    logger.warning(
                        f"Barcode scanner disconnected: {exc} — will retry until reconnected"
                    )
                    disconnected_logged = True
                self._stop_event.wait(3)
                new_path = _find_scanner()
                if new_path:
                    path = new_path
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                if not disconnected_logged:
                    logger.exception(
                        f"Barcode scanner error: {exc} — will retry until reconnected"
                    )
                    disconnected_logged = True
                self._stop_event.wait(3)
