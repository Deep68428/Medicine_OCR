"""
PP-OCRv5 recognition inference with OpenVINO.

Usage
-----
    from rec_server_vino import Paddle_vino

    ocr = Paddle_vino("/path/to/rec_server_model.xml")           # model XML
    ocr = Paddle_vino("/path/to/rec_server_model.xml",
                      dict_path="/path/to/ppocrv5_dict.txt")     # explicit dict

    # run on a single image file
    results = ocr.run("crop/foo.jpg")

    # run on a directory of images
    results = ocr.run("crop/")

    # run on a pre-loaded numpy BGR image (H×W×3, uint8)
    results = ocr.run(np_bgr_image)

    # run on a list of paths / arrays
    results = ocr.run(["img1.jpg", "img2.png", np_array])

Output
------
Each call returns a list of dicts, one per image:

    [
        {
            "file":    "foo.jpg",        # filename or "array_N" for numpy inputs
            "text":    "M78T86005",      # recognised text  (empty string = no text)
            "score":   0.846,            # mean char confidence  (0.0 if no text)
            "time_ms": 53.9              # inference time in milliseconds
        },
        ...
    ]
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Union

import cv2
import numpy as np
import openvino as ov

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
ImageLike = Union[str, Path, np.ndarray]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_IMG_H = 48
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# Candidate dict locations relative to the XML file (tried in order)
_DICT_SEARCH = [
    "ppocrv5_dict.txt",  # same dir as XML
    "../paddle_convert/ppocrv5_dict.txt",
    "../../paddle_convert/ppocrv5_dict.txt",
]


# ---------------------------------------------------------------------------
class Paddle_vino:
    """OpenVINO-accelerated PP-OCRv5 text recognition."""

    # ------------------------------------------------------------------
    def __init__(
        self,
        model_xml: str | Path,
        dict_path: str | Path | None = None,
        device: str = "CPU",
    ) -> None:
        """
        Parameters
        ----------
        model_xml : path to the OpenVINO IR ``.xml`` file
                    (the companion ``.bin`` must sit next to it)
        dict_path : path to ``ppocrv5_dict.txt``; if *None* the constructor
                    searches common locations relative to model_xml
        device    : OpenVINO device string, e.g. ``"CPU"``, ``"GPU"``
        """
        model_xml = Path(model_xml).expanduser().resolve()
        if not model_xml.exists():
            raise FileNotFoundError(f"Model XML not found: {model_xml}")

        # --- load character dictionary ---
        dict_file = self._find_dict(model_xml, dict_path)
        with open(dict_file, encoding="utf-8") as f:
            chars = [line.rstrip("\n") for line in f]
        # index 0 → CTC blank; chars at 1 … N
        self._chars: list[str] = [""] + chars

        # --- compile OpenVINO model ---
        core = ov.Core()
        model = core.read_model(str(model_xml))
        self._compiled = core.compile_model(
            model,
            device,
            config={
                "PERFORMANCE_HINT": "LATENCY",
                "NUM_STREAMS": "1",
                "INFERENCE_NUM_THREADS": "2",
            },
        )

        # get the single output tensor key once
        self._out_key = list(self._compiled.outputs)[0]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        source: ImageLike | List[ImageLike],
    ) -> list[dict]:
        """
        Recognise text in one or more images.

        Parameters
        ----------
        source : one of
            - str / Path pointing to an **image file**
            - str / Path pointing to a **directory** (all images inside)
            - numpy ndarray  (H × W × 3, BGR, uint8)
            - list of any of the above

        Returns
        -------
        list[dict]  — see module docstring for field descriptions
        """
        items = self._collect(source)  # list of (label, np.ndarray)
        results = []
        for label, bgr in items:
            blob = self._preprocess(bgr)
            t0 = time.perf_counter()
            raw = self._compiled({0: blob})[self._out_key]  # [1, T, C]
            ms = (time.perf_counter() - t0) * 1000
            text, score = self._ctc_decode(raw)
            results.append(
                dict(file=label, text=text, score=round(score, 4), time_ms=round(ms, 2))
            )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _find_dict(model_xml: Path, explicit: str | Path | None) -> Path:
        if explicit is not None:
            p = Path(explicit).expanduser().resolve()
            if not p.exists():
                raise FileNotFoundError(f"dict_path not found: {p}")
            return p
        base = model_xml.parent
        for rel in _DICT_SEARCH:
            p = (base / rel).resolve()
            if p.exists():
                return p
        raise FileNotFoundError(
            "Cannot find ppocrv5_dict.txt — pass dict_path= explicitly."
        )

    def _collect(self, source: ImageLike | list) -> list[tuple[str, np.ndarray]]:
        """Normalise any accepted input into a list of (label, bgr_array)."""
        if isinstance(source, list):
            out = []
            for i, item in enumerate(source):
                out.extend(self._collect_one(item, fallback_label=f"array_{i}"))
            return out
        return self._collect_one(source, fallback_label="array_0")

    def _collect_one(
        self, item: ImageLike, fallback_label: str
    ) -> list[tuple[str, np.ndarray]]:
        if isinstance(item, np.ndarray):
            return [(fallback_label, item)]

        p = Path(item).expanduser()
        if p.is_dir():
            pairs = []
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in _IMG_EXTS:
                    img = cv2.imread(str(f))
                    if img is not None:
                        pairs.append((f.name, img))
            return pairs

        # single file
        img = cv2.imread(str(p))
        if img is None:
            raise FileNotFoundError(f"Cannot read image: {p}")
        return [(p.name, img)]

    @staticmethod
    def _preprocess(bgr: np.ndarray) -> np.ndarray:
        """Return float32 blob [1, 3, 48, W] normalised to [-1, 1]."""
        h, w = bgr.shape[:2]
        new_w = max(1, round(w * _IMG_H / h))
        img = cv2.resize(bgr, (new_w, _IMG_H))
        img = img.astype(np.float32) / 255.0
        img = (img - 0.5) / 0.5  # → [-1, 1]
        img = img.transpose(2, 0, 1)  # HWC → CHW
        return img[np.newaxis]  # → [1, 3, H, W]

    def _ctc_decode(self, probs: np.ndarray) -> tuple[str, float]:
        """
        CTC greedy decode.

        probs : [1, T, num_classes]  — already softmax probabilities
        Returns (text, mean_char_confidence).
        """
        p = probs[0]  # [T, C]
        indices = p.argmax(axis=-1)  # [T]
        confs = p.max(axis=-1)  # [T]

        chars_out: list[str] = []
        conf_out: list[float] = []
        prev = -1
        for idx, c in zip(indices.tolist(), confs.tolist()):
            if idx != prev:
                if idx != 0:  # 0 = CTC blank
                    ch = self._chars[idx] if idx < len(self._chars) else "?"
                    chars_out.append(ch)
                    conf_out.append(c)
                prev = idx
            else:
                prev = idx

        text = "".join(chars_out)
        score = float(np.mean(conf_out)) if conf_out else 0.0
        return text, score


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 3:
        print("Usage: python rec_server_vino.py <model.xml> <image_or_dir> [dict.txt]")
        sys.exit(1)

    xml = sys.argv[1]
    src = sys.argv[2]
    dpath = sys.argv[3] if len(sys.argv) > 3 else None

    ocr = Paddle_vino(xml, dict_path=dpath)
    results = ocr.run(src)
    print(json.dumps(results, ensure_ascii=False, indent=2))
