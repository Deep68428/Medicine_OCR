from loguru import logger
from services.infer import OBBModel
import cv2
import time
import numpy as np
from types import SimpleNamespace
from services.parseq_infer import ParseqVino
import os

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENVINO_CPU_THREADS_NUM"] = "1"


class OCRPipeline:
    def __init__(
        self,
        batch_model_path="models/batch_model/",
        text_model_path="models/text_model/",
        ocr_xml="models/ocr_model/parseq_s_zota.xml",
        dict_path="models/ocr_model/charset.txt",
        batch_class_name="batch",
        barcode_class_name="barcode",
    ):
        self.batch_model = OBBModel(batch_model_path)
        self.text_model = OBBModel(text_model_path)
        self.ocr = ParseqVino(ocr_xml, dict_path=dict_path)
        self.batch_class_name = batch_class_name
        self.barcode_class_name = barcode_class_name
        self.last_text_crops: list[np.ndarray] = []

    # ------------------------------------------------------------------ #
    #  Static helpers                                                      #
    # ------------------------------------------------------------------ #

    def warmup(self, iterations=10):
        warmup_img = np.full((640, 640, 3), 255, dtype=np.uint8)
        for i in range(iterations):
            self.batch_model.run(warmup_img)
            logger.debug(f"Batch warmup {i}")
        for i in range(iterations):
            self.text_model.run(warmup_img)
            logger.debug(f"Text warmup  {i}")
        for i in range(iterations):
            self.ocr.run(warmup_img)
            logger.debug(f"OCR warmup   {i}")

    @staticmethod
    def expand_corners(batch, margin=50):
        pts = np.array(batch, dtype=np.float32)
        center = np.mean(pts, axis=0)
        expanded_pts = []
        for pt in pts:
            direction = pt - center
            norm = np.linalg.norm(direction)
            if norm == 0:
                expanded_pts.append(pt)
                continue
            expanded_pts.append(pt + (direction / norm) * margin)
        return np.array(expanded_pts, dtype=np.float32)

    @staticmethod
    def _order_corners(pts, angle_rad=None):
        pts = np.array(pts, dtype=np.float32)
        center = pts.mean(axis=0)

        if angle_rad is not None:
            tl_dir = angle_rad + np.radians(-135)
            tl_vec = np.array([np.cos(tl_dir), np.sin(tl_dir)])

            scores = []
            for pt in pts:
                v = pt - center
                norm = np.linalg.norm(v)
                if norm == 0:
                    scores.append(-np.inf)
                else:
                    scores.append(np.dot(v / norm, tl_vec))

            start = int(np.argmax(scores))
        else:
            start = int(np.argmin(pts[:, 0] + pts[:, 1]))

        ordered = np.roll(pts, -start, axis=0)
        return ordered.astype(np.float32)

    @classmethod
    def unwarp_batch(cls, image, batch, angle=None, margin=40):
        # 1. Expand corners outward, then order tl→tr→br→bl using angle
        expanded = cls.expand_corners(batch, margin)
        tl, tr, br, bl = cls._order_corners(expanded, angle_rad=angle)

        # 2. Compute width & height from the correctly ordered corners
        w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
        h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
        w = max(w, 1)
        h = max(h, 1)

        # 3. Perspective warp to upright rectangle
        src = np.array([tl, tr, br, bl], dtype=np.float32)
        dst = np.array(
            [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32
        )
        M = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(image, M, (w, h))

        return warped

    @staticmethod
    def add_pad(img, pad=30):
        return cv2.copyMakeBorder(
            img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=[255, 255, 255]
        )

    # ------------------------------------------------------------------ #
    #  Format converters                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_yolo_result(
        raw_results,
        image,
        warped_crops=None,
        model_names=None,
        t_preprocess=0.0,
        t_inference=0.0,
        t_postprocess=0.0,
    ):
        if model_names is None:
            unique_classes = sorted({r["class"] for r in raw_results})
            model_names = {i: c for i, c in enumerate(unique_classes)}

        class_to_idx = {v: k for k, v in model_names.items()}

        xyxy_list = []
        xyxyxyxy_list = []
        conf_list = []
        cls_list = []

        for r in raw_results:
            corners = np.array(r["corners"], dtype=np.float32)
            x1, y1 = corners.min(axis=0)
            x2, y2 = corners.max(axis=0)
            xyxy_list.append([x1, y1, x2, y2])
            xyxyxyxy_list.append(corners.flatten())
            conf_list.append(float(r.get("conf", 1.0)))
            cls_idx = class_to_idx.get(r["class"], 0)
            cls_list.append(float(cls_idx))

        n = len(raw_results)
        boxes_ns = SimpleNamespace()
        boxes_ns.xyxy = np.array(xyxy_list, dtype=np.float32).reshape(n, 4)
        boxes_ns.xyxyxyxy = np.array(xyxyxyxy_list, dtype=np.float32).reshape(n, 8)
        boxes_ns.conf = np.array(conf_list, dtype=np.float32)
        boxes_ns.cls = np.array(cls_list, dtype=np.float32)

        result = SimpleNamespace()
        result.boxes = boxes_ns
        result.names = model_names
        result.orig_shape = image.shape[:2]
        result.orig_img = image
        result.crops = warped_crops if warped_crops is not None else []
        result.speed = {
            "preprocess": t_preprocess,
            "inference": t_inference,
            "postprocess": t_postprocess,
        }
        return result

    @staticmethod
    def _to_rapid_result(text_results, ocr_results):
        rows = []

        def _extract(item):
            if isinstance(item, dict):
                return str(item.get("TEXT") or item.get("text", "")), float(
                    item.get("SCORE") or item.get("score", 0.0)
                )
            if isinstance(item, (list, tuple)) and len(item) == 2:
                return str(item[0]), float(item[1])
            return str(item), 0.0

        for i, r in enumerate(text_results):
            corners = np.array(r["corners"], dtype=np.float32)
            box = corners.tolist()

            orig_idx = i * 2
            rot_idx = i * 2 + 1

            text, score = "", 0.0
            if orig_idx < len(ocr_results):
                text, score = _extract(ocr_results[orig_idx])
            if rot_idx < len(ocr_results):
                rot_text, rot_score = _extract(ocr_results[rot_idx])
                if rot_score > score:
                    text, score = rot_text, rot_score

            rows.append([box, text, score])

        return rows, 0.0

    # ------------------------------------------------------------------ #
    #  Public prediction methods                                           #
    # ------------------------------------------------------------------ #

    def predict_yolo(self, image_or_path, model_names=None, unwarp_margin=40, pad=0):
        # Clear stale crops from the previous scan so reject-path saving can't
        # re-upload a prior frame's crops when this scan skips Paddle OCR
        # (barcode path / no batch detections never call predict_rapid).
        self.last_text_crops = []
        if isinstance(image_or_path, str):
            image = cv2.imread(image_or_path)
            if image is None:
                raise ValueError(f"Cannot read image: {image_or_path}")
        else:
            image = image_or_path

        t0 = time.time()
        raw = self.batch_model.run(image)
        t_infer = (time.time() - t0) * 1000

        # Keep both batch-label and barcode detections; each gets a perspective crop
        relevant_classes = {self.batch_class_name, self.barcode_class_name}
        relevant_detections = [r for r in raw if r["class"] in relevant_classes]
        batch_count = sum(
            1 for r in relevant_detections if r["class"] == self.batch_class_name
        )
        barcode_count = sum(
            1 for r in relevant_detections if r["class"] == self.barcode_class_name
        )
        logger.debug(
            f"predict_yolo: {len(raw)} total detections, "
            f"{batch_count} '{self.batch_class_name}', {barcode_count} '{self.barcode_class_name}'"
        )

        t1 = time.time()
        warped_crops = []
        for r in relevant_detections:
            warped = self.unwarp_batch(
                image,
                r["corners"],
                angle=r.get("angle"),
                margin=unwarp_margin,
            )
            padded = self.add_pad(warped, pad=pad)
            warped_crops.append(padded)
        t_post = (time.time() - t1) * 1000

        return self._to_yolo_result(
            relevant_detections,
            image,
            warped_crops=warped_crops,
            model_names=model_names,
            t_inference=t_infer,
            t_postprocess=t_post,
        )

    def predict_rapid(self, image_or_path, unwarp_margin=10, pad=0):
        if isinstance(image_or_path, str):
            image = cv2.imread(image_or_path)
            if image is None:
                raise ValueError(f"Cannot read image: {image_or_path}")
        else:
            image = image_or_path

        text_results = self.text_model.run(image)
        text_results = [r for r in text_results if r["conf"] >= 0.40]

        crops = []
        logger.info("total text detections: {}".format(len(text_results)))
        for i, r in enumerate(text_results):
            warped = self.unwarp_batch(
                image,
                r["corners"],
                angle=r.get("angle"),
                margin=unwarp_margin,
            )
            padded = self.add_pad(warped, pad=pad)
            # based on hight and width of the padded image rotate the iamge based on hight and width h<w
            h, w = padded.shape[:2]
            if h > w:
                padded = cv2.rotate(padded, cv2.ROTATE_90_CLOCKWISE)

            # Apply contrast and sharpness
            alpha = 1.0  # Contrast (1.0 = no change)
            beta = 10  # Brightness offset
            padded = cv2.convertScaleAbs(padded, alpha=alpha, beta=beta)

            kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
            padded = cv2.filter2D(padded, -1, kernel)

            crops.append(padded)
            # rotate 180 and add to crops list
            rotated = cv2.rotate(padded, cv2.ROTATE_180)
            crops.append(rotated)

        ocr_results = self.ocr.run(crops) if crops else []

        # originals only (even indices); rotated copies are odd indices — available for failure saving
        self.last_text_crops = crops[::2]

        return self._to_rapid_result(text_results, ocr_results)
