"""
Inference module for YOLO v26-OBB OpenVINO INT8 models.

    from infer import OBBModel

    model = OBBModel("best_text_int8_512/")
    results = model.run("image.png")
    results = model.run(img_bgr_array)

Each result dict:
    {
        "class":   "Text" | "barcode" | "batch",
        "conf":    0.82,
        "cx":      412.3,   # center x (original image coords)
        "cy":      310.1,   # center y
        "w":       180.4,   # box width
        "h":        42.7,   # box height
        "angle":    0.392,  # rotation in radians
        "corners": [[x0,y0],[x1,y1],[x2,y2],[x3,y3]]
    }

YOLO v26 output head (decoded, shape: (1, 300, 7)):
    col 0   cx          letterbox space
    col 1   cy
    col 2   w
    col 3   h
    col 4   conf        already max-class score
    col 5   class_id    already argmax — integer stored as float
    col 6   angle       radians
"""

import cv2
import numpy as np
import yaml
import openvino as ov
from pathlib import Path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _letterbox(img, size):
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    img_resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    dw, dh = (size - nw) / 2, (size - nh) / 2
    t = int(round(dh - 0.1))
    b = int(round(dh + 0.1))
    l = int(round(dw - 0.1))  # noqa: E741
    r_ = int(round(dw + 0.1))
    img_padded = cv2.copyMakeBorder(
        img_resized,
        t,
        b,
        l,
        r_,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    return img_padded, r, (dw, dh)


def _nms_rotated(boxes, scores, iou_thresh):
    """
    Rotated NMS.
    boxes : (N, 5)  columns [cx, cy, w, h, angle_rad]
    scores: (N,)
    Returns indices to keep.
    """
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        ri = (
            (float(boxes[i, 0]), float(boxes[i, 1])),
            (float(boxes[i, 2]), float(boxes[i, 3])),
            float(np.degrees(boxes[i, 4])),
        )
        ai = boxes[i, 2] * boxes[i, 3]
        ious = []
        for j in order[1:]:
            rj = (
                (float(boxes[j, 0]), float(boxes[j, 1])),
                (float(boxes[j, 2]), float(boxes[j, 3])),
                float(np.degrees(boxes[j, 4])),
            )
            ret, pts = cv2.rotatedRectangleIntersection(ri, rj)
            inter = (
                float(cv2.contourArea(pts))
                if ret != cv2.INTERSECT_NONE and pts is not None
                else 0.0
            )
            ious.append(inter / (ai + boxes[j, 2] * boxes[j, 3] - inter + 1e-9))
        order = order[1:][np.array(ious) <= iou_thresh]
    return np.array(keep, dtype=np.int64)


def _corners(cx, cy, w, h, angle):
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    hw, hh = w / 2, h / 2
    pts = np.array([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]], dtype=np.float32)
    rot = np.array([[cos_a, -sin_a], [sin_a, cos_a]])
    return (pts @ rot.T + np.array([cx, cy])).tolist()


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class OBBModel:
    def __init__(self, model_dir, conf=0.25, iou=0.45):
        model_dir = Path(model_dir)
        meta = yaml.safe_load((model_dir / "metadata.yaml").read_text())

        self.names = meta["names"]
        self.nc = len(self.names)
        self.conf = conf
        self.iou = iou

        # imgsz from metadata — must match training size (e.g. 832)
        imgsz = meta.get("imgsz", 832)
        self.imgsz = imgsz[0] if isinstance(imgsz, (list, tuple)) else int(imgsz)

        core = ov.Core()
        compiled = core.compile_model(
            core.read_model(next(model_dir.glob("*.xml"))),
            "CPU",
            config={
                "PERFORMANCE_HINT": "LATENCY",
                "NUM_STREAMS": "1",
                "INFERENCE_NUM_THREADS": "2",
            },
        )
        self._infer = compiled
        self._output = compiled.output(0)

        # warm up
        self._infer({0: np.zeros((1, 3, self.imgsz, self.imgsz), np.float32)})

    def run(self, source, conf=None, iou=None):
        """
        Run inference on an image.

        Parameters
        ----------
        source : str | Path | np.ndarray  (BGR)
        conf   : float, optional  override confidence threshold
        iou    : float, optional  override NMS IoU threshold

        Returns
        -------
        list[dict]  sorted by confidence descending
        """
        conf = conf if conf is not None else self.conf
        iou = iou if iou is not None else self.iou

        img = cv2.imread(str(source)) if not isinstance(source, np.ndarray) else source
        if img is None:
            raise FileNotFoundError(f"Cannot read image: {source}")

        lb, ratio, (dw, dh) = _letterbox(img, self.imgsz)
        blob = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None]

        # v26 decoded output: (1, 300, 7)
        # columns: cx  cy  w  h  conf  class_id  angle
        raw = self._infer({0: blob})[self._output][0]  # (300, 7)

        scores = raw[:, 4]
        cls_ids = raw[:, 5].astype(np.int32)
        ang = raw[:, 6]
        xywh = raw[:, :4]

        # confidence filter
        mask = scores >= conf
        if not mask.any():
            return []

        scores = scores[mask]
        cls_ids = cls_ids[mask]
        ang = ang[mask]
        xywh = xywh[mask]

        # [cx, cy, w, h, angle] for NMS
        boxes = np.concatenate([xywh, ang[:, None]], axis=1)

        # per-class rotated NMS with correct global-index remapping
        keep_global = []
        for c in np.unique(cls_ids):
            local_idx = np.where(cls_ids == c)[0]
            kept_local = _nms_rotated(boxes[local_idx], scores[local_idx], iou)
            keep_global.append(local_idx[kept_local])

        if not keep_global:
            return []
        keep = np.concatenate(keep_global)

        # scale back to original image space
        oh, ow = img.shape[:2]
        results = []
        for i in keep:
            cx = float(np.clip((boxes[i, 0] - dw) / ratio, 0, ow))
            cy = float(np.clip((boxes[i, 1] - dh) / ratio, 0, oh))
            w = float(boxes[i, 2] / ratio)
            h = float(boxes[i, 3] / ratio)
            a = float(boxes[i, 4])

            # skip degenerate boxes
            if w <= 0 or h <= 0:
                continue

            results.append(
                {
                    "class": self.names[int(cls_ids[i])],
                    "conf": round(float(scores[i]), 4),
                    "cx": round(cx, 2),
                    "cy": round(cy, 2),
                    "w": round(w, 2),
                    "h": round(h, 2),
                    "angle": round(a, 6),
                    "corners": _corners(cx, cy, w, h, a),
                }
            )

        results.sort(key=lambda x: -x["conf"])
        return results
