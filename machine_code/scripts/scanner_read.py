#!/usr/bin/env python3
"""Read barcodes from Newtologic 4010E USB scanner (HID keyboard mode)."""

import glob
import select

import evdev
from evdev import InputDevice, categorize, ecodes

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


def _find_scanner() -> str:
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            dev = evdev.InputDevice(path)
            if "newtologic" in dev.name.lower() or "4010" in dev.name.lower():
                return path
        except Exception:
            continue
    raise RuntimeError("Scanner not found. Is it plugged in?")


def read_barcodes(callback=None):
    """Read barcodes from scanner. Calls callback(barcode) on each scan,
    or prints to stdout if no callback given."""
    dev = InputDevice(_find_scanner())
    print(f"Listening on: {dev.name} ({dev.path})")

    barcode = ""
    shift = False

    while True:
        r, _, _ = select.select([dev], [], [])
        for event in dev.read():
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
                    if callback:
                        callback(barcode)
                    else:
                        print(f"Scanned: {barcode}")
                barcode = ""
                shift = False
                continue

            char = SHIFT_MAP.get(key) if shift else KEY_MAP.get(key)
            if char:
                barcode += char


if __name__ == "__main__":
    read_barcodes()
