#!/usr/bin/env python3
"""OpenVINO inference for the PARSeq-S Zota OCR model — class API.

Dependencies: openvino, numpy, pillow  (NO torch / strhub needed).

    from infer import ParseqVino

    ocr = ParseqVino("/path/to/parseq_s_zota.xml")               # model XML
    ocr = ParseqVino("/path/to/parseq_s_zota.xml",
                     dict_path="/path/to/charset.txt")           # explicit charset

    results = ocr.run("crop/foo.jpg")        # single image file
    results = ocr.run("crop/")               # directory of images
    results = ocr.run(np_bgr_image)          # pre-loaded numpy BGR image (H×W×3, uint8)
    results = ocr.run(["img1.jpg", np_arr])  # list of paths / arrays

Each result:
    {
        "file":    "foo.jpg",      # filename, or "array_N" for numpy inputs
        "text":    "M78T86005",    # recognised text ("" = no text)
        "score":   0.846,          # mean char confidence (0.0 if no text)
        "time_ms": 53.9            # inference time in milliseconds
    }

The IR output is logits of shape (batch, T, 95): class 0 is <eos> and classes
1..94 map to charset[0..93]. Decoding is greedy until the first <eos>; the score
is the mean softmax probability of the chosen characters.

Tighten-on-loop fallback: some crops carry a faint ghost echo of the line (or a
merged second row), making the recognizer read the text twice and loop
(e.g. SPT260912 -> SPT260912260912). When a prediction has an immediately
repeated substring we re-run OCR on a crop tightened to the dominant text band
and keep it if the loop clears. Selective by design — tightening every crop hurts
clean ones. Disable via ParseqVino(..., tighten_on_loop=False) or CLI --no-tighten.

CLI:
    uv run python infer.py crop/ --out preds.txt
    python infer.py crop/foo.jpg --device CPU
"""

import argparse
import time
from pathlib import Path

import numpy as np
import openvino as ov
from PIL import Image, ImageOps

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


# --------------------------------------------------------------------------- #
# Image / decoding helpers (module-level so they can be reused or unit-tested).
# --------------------------------------------------------------------------- #
def preprocess(img: Image.Image, img_size) -> np.ndarray:
    """Match PARSeq: BICUBIC resize -> [0,1] -> normalize(0.5,0.5) -> CHW float32."""
    h, w = img_size
    img = img.convert("RGB").resize((w, h), Image.BICUBIC)  # PIL takes (W, H)
    arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC, [0,1]
    arr = (arr - 0.5) / 0.5  # [-1, 1]
    return np.transpose(arr, (2, 0, 1))  # CHW


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max(axis=-1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=-1, keepdims=True)


def decode(logits: np.ndarray, charset: str):
    """logits: (T, num_classes). Returns (text, mean_char_confidence).

    Greedy argmax until the first <eos> (class 0). Score is the mean softmax
    probability of the selected characters, or 0.0 for an empty prediction.
    """
    probs = _softmax(logits)
    ids = logits.argmax(-1)
    chars, confs = [], []
    for t, i in enumerate(ids):
        if i == 0:  # <eos>
            break
        chars.append(charset[i - 1])
        confs.append(float(probs[t, i]))
    text = "".join(chars)
    return text, (float(np.mean(confs)) if confs else 0.0)


def _otsu(gray: np.ndarray) -> float:
    hist, _ = np.histogram(gray, bins=256, range=(0, 255))
    p = hist / max(hist.sum(), 1)
    w = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    denom = w * (1 - w)
    sigma_b = np.where(denom > 0, (mu[-1] * w - mu) ** 2 / np.maximum(denom, 1e-12), 0)
    return float(np.argmax(sigma_b))


def tighten(
    img: Image.Image, pad: float = 0.18, row_frac: float = 0.30, min_band_h: int = 8
) -> Image.Image:
    """Trim to the dominant horizontal text band (dark text on light bg).

    Removes ghost-echo / secondary rows. Returns the original if no confident band.
    """
    g = np.asarray(ImageOps.grayscale(img), dtype=np.float32)
    ink = g < _otsu(g)  # True where dark (text)
    row = ink.mean(axis=1)
    if row.max() <= 0:
        return img
    rows = np.where(row >= max(0.04, row.max() * row_frac))[0]
    if len(rows) == 0:
        return img
    # Pick the contiguous run of ink-rows holding the most ink (the main line).
    groups = np.split(rows, np.where(np.diff(rows) > 2)[0] + 1)
    band = max(groups, key=lambda s: row[s].sum())
    y0, y1 = int(band[0]), int(band[-1])
    if (y1 - y0 + 1) < min_band_h:
        return img
    W, H = img.size
    pa = int(round((y1 - y0 + 1) * pad))
    return img.crop((0, max(0, y0 - pa), W, min(H, y1 + pa + 1)))


def has_loop(s: str, min_len: int = 3) -> bool:
    """True if s has an immediately-repeated substring of length >= min_len
    (the signature of the recognizer's echo/repeat failure)."""
    n = len(s)
    for L in range(min_len, n // 2 + 1):
        for st in range(n - 2 * L + 1):
            if s[st : st + L] == s[st + L : st + 2 * L]:
                return True
    return False


# --------------------------------------------------------------------------- #
# Class API
# --------------------------------------------------------------------------- #
class ParseqVino:
    """PARSeq-S OCR recogniser backed by an OpenVINO IR.

    Args:
        model_path: path to the .xml IR (the .bin must sit next to it).
        dict_path:  optional charset file. Defaults to `charset.txt` next to the
                    model. Accepts a single-line charset or one-char-per-line.
        device:     OpenVINO device ("CPU", "GPU", "AUTO", ...).
        img_size:   (H, W). Defaults to `img_size.txt` next to the model, else (32,128).
        tighten_on_loop: enable the ghost-echo tighten-and-retry fallback.
        warmup:     number of warmup inferences at construction time.
    """

    def __init__(
        self,
        model_path,
        dict_path=None,
        device="CPU",
        img_size=None,
        tighten_on_loop=True,
        warmup=10,
    ):
        model_path = Path(model_path)
        self.charset = self._load_charset(model_path, dict_path)
        self.img_size = tuple(img_size) if img_size else self._load_img_size(model_path)
        self.device = device
        self.tighten_on_loop = tighten_on_loop

        core = ov.Core()
        self.compiled = core.compile_model(core.read_model(model_path), device)
        self.out_port = self.compiled.output(0)

        dummy = np.zeros((1, 3, *self.img_size), dtype=np.float32)
        for _ in range(max(0, warmup)):
            self.compiled(dummy)

    # -- asset loading ----------------------------------------------------- #
    @staticmethod
    def _load_charset(model_path: Path, dict_path) -> str:
        p = Path(dict_path) if dict_path else model_path.parent / "charset.txt"
        raw = p.read_text(encoding="utf-8")
        lines = raw.splitlines()
        if len(lines) > 1:  # one-char-per-line dict
            return "".join(lines)
        return raw.rstrip("\n")  # single-line charset

    @staticmethod
    def _load_img_size(model_path: Path):
        f = model_path.parent / "img_size.txt"
        if f.is_file():
            h, w = (int(x) for x in f.read_text().split())
            return (h, w)
        return (32, 128)

    # -- input normalisation ---------------------------------------------- #
    @staticmethod
    def _to_pil(ref) -> Image.Image:
        """Path/str -> RGB image; numpy BGR (H×W×3 or H×W) uint8 -> RGB image."""
        if isinstance(ref, np.ndarray):
            arr = ref
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, axis=-1)
            arr = arr[:, :, ::-1]  # BGR -> RGB
            return Image.fromarray(np.ascontiguousarray(arr.astype(np.uint8)), "RGB")
        return Image.open(ref).convert("RGB")

    @classmethod
    def _enumerate(cls, source):
        """Return a list of (name, ref) where ref is a Path or a numpy array."""
        if isinstance(source, np.ndarray):
            return [("array_0", source)]
        if isinstance(source, (str, Path)):
            p = Path(source)
            if p.is_dir():
                return [
                    (f.name, f)
                    for f in sorted(p.iterdir())
                    if f.suffix.lower() in IMG_EXTS
                ]
            if p.is_file():
                return [(p.name, p)]
            raise FileNotFoundError(f"No such file or folder: {p}")
        if isinstance(source, (list, tuple)):
            items, ai = [], 0
            for it in source:
                if isinstance(it, np.ndarray):
                    items.append((f"array_{ai}", it))
                    ai += 1
                else:
                    items.append((Path(it).name, Path(it)))
            return items
        raise TypeError(f"Unsupported source type: {type(source)}")

    # -- inference --------------------------------------------------------- #
    def run(self, source, batch_size=32):
        """Run OCR on a file, directory, numpy BGR image, or list thereof.

        Returns a list of {"file", "text", "score", "time_ms"} dicts.
        """
        items = self._enumerate(source)
        results = []
        for i in range(0, len(items), batch_size):
            chunk = items[i : i + batch_size]
            pil = [self._to_pil(ref) for _, ref in chunk]
            x = np.stack([preprocess(im, self.img_size) for im in pil]).astype(
                np.float32
            )
            t0 = time.perf_counter()
            logits = self.compiled(x)[self.out_port]  # (bs, T, classes)
            per_ms = 1000 * (time.perf_counter() - t0) / len(chunk)
            for (name, _), im, lg in zip(chunk, pil, logits):
                text, score = decode(lg, self.charset)
                ms = per_ms
                if self.tighten_on_loop and has_loop(text):
                    t = tighten(im)
                    if t.size != im.size:
                        tt = time.perf_counter()
                        lg2 = self.compiled(preprocess(t, self.img_size)[None])[
                            self.out_port
                        ][0]
                        ms += 1000 * (time.perf_counter() - tt)
                        text2, score2 = decode(lg2, self.charset)
                        if not has_loop(text2):
                            text, score = text2, score2
                results.append(
                    {
                        "file": name,
                        "text": text,
                        "score": round(score, 3),
                        "time_ms": round(ms, 1),
                    }
                )
        return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="OpenVINO OCR inference (PARSeq-S).")
    ap.add_argument("path", help="Image file or folder of images")
    ap.add_argument("--model", default=str(here / "parseq_s_zota.xml"))
    ap.add_argument(
        "--dict", default=None, help="Charset file (default: charset.txt beside model)"
    )
    ap.add_argument(
        "--device", default="CPU", help="OpenVINO device (CPU, GPU, AUTO, ...)"
    )
    ap.add_argument("--out", default=None, help="Optional output .txt (tab-separated)")
    ap.add_argument("--batch_size", type=int, default=32)
    ap.add_argument(
        "--no-tighten",
        action="store_true",
        help="Disable the tighten-on-loop fallback for repeated/echoed crops",
    )
    args = ap.parse_args()

    ocr = ParseqVino(
        args.model,
        dict_path=args.dict,
        device=args.device,
        tighten_on_loop=not args.no_tighten,
    )
    results = ocr.run(args.path, batch_size=args.batch_size)
    if not results:
        print("No images found.")
        return

    for r in results:
        print(f"{r['file']}\t{r['text']}\t{r['score']:.3f}\t{r['time_ms']:.1f} ms")
    avg = sum(r["time_ms"] for r in results) / len(results)
    print(f"\n[{len(results)} images | avg {avg:.2f} ms/image on {args.device}]")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(f"{r['file']}\t{r['text']}\t{r['score']:.3f}\n")
        print(f"[saved {len(results)} predictions -> {args.out}]")


if __name__ == "__main__":
    main()
