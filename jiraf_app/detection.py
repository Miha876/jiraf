from __future__ import annotations

from typing import List

import cv2
import numpy as np

"""   : ONNX-   ."""

try:
    import onnxruntime as ort
except Exception:
    ort = None  # onnxruntime   ,     ONNX-


class DetectionResults:
    """  xywh/conf/cls,   ."""
    def __init__(self, xywh, conf, cls):
        self.xywh = xywh
        self.conf = conf
        self.cls = cls

    def __getitem__(self, idx):
        return DetectionResults(
            self.xywh[idx],
            self.conf[idx],
            self.cls[idx],
        )

    def __len__(self):
        return len(self.xywh)


def letterbox(image: np.ndarray, new_shape=(640, 640), color=(114, 114, 114)):
    """      ."""
    shape = image.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]
    dw /= 2
    dh /= 2
    if shape[::-1] != new_unpad:
        image = cv2.resize(image, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    image = cv2.copyMakeBorder(image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return image, r, (left, top)


def nms_boxes(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float = 0.45):
    """      IoU."""
    if boxes.size == 0:
        return []
    x = boxes[:, 0]
    y = boxes[:, 1]
    w = boxes[:, 2]
    h = boxes[:, 3]
    areas = w * h
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x[i], x[order[1:]])
        yy1 = np.maximum(y[i], y[order[1:]])
        xx2 = np.minimum(x[i] + w[i], x[order[1:]] + w[order[1:]])
        yy2 = np.minimum(y[i] + h[i], y[order[1:]] + h[order[1:]])
        inter_w = np.maximum(0.0, xx2 - xx1)
        inter_h = np.maximum(0.0, yy2 - yy1)
        inter = inter_w * inter_h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        inds = np.where(iou <= iou_thresh)[0]
        order = order[inds + 1]
    return keep


class OnnxDetector:
    """ ONNX-     ."""
    def __init__(self, model_path: str, class_names: List[str]):
        if ort is None:
            raise RuntimeError("onnxruntime  ")
        self.class_names = class_names
        self.last_pred_shape = None
        self.last_max_score = 0.0
        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
        providers = ["CPUExecutionProvider"]
        try:
            available = ort.get_available_providers()
            if "CUDAExecutionProvider" in available:
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        except Exception:
            pass
        self.session = ort.InferenceSession(model_path, sess_options=sess_opts, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.input_h = int(shape[2]) if isinstance(shape[2], int) else 640
        self.input_w = int(shape[3]) if isinstance(shape[3], int) else 640

    def infer(self, frame: np.ndarray, conf: float):
        """  ,   conf   ."""
        img, r, (pad_w, pad_h) = letterbox(frame, (self.input_h, self.input_w))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = img.astype(np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1))
        img = np.expand_dims(img, 0)

        outputs = self.session.run(None, {self.input_name: img})
        pred = outputs[0]
        if pred.ndim == 3:
            pred = pred[0]

        if pred.ndim != 2:
            return np.empty((0, 4)), np.array([]), np.array([])

        self.last_pred_shape = pred.shape
        used_nms = False
        max_coord = 0.0
        if pred.shape[1] in (6, 7):
            boxes = pred[:, :4]
            scores = pred[:, 4]
            self.last_max_score = float(np.nanmax(scores)) if scores.size else 0.0
            class_ids = pred[:, 5].astype(int)
            used_nms = True
            mask = scores >= conf
            boxes = boxes[mask]
            scores = scores[mask]
            class_ids = class_ids[mask]
            if boxes.size == 0:
                return np.empty((0, 4)), np.array([]), np.array([])
            xyxy = boxes.astype(np.float32)
            if xyxy.size:
                max_coord = float(np.nanmax(xyxy))
        else:
            if pred.shape[0] > pred.shape[1]:
                pred = pred.T
            if pred.shape[1] < 6:
                return np.empty((0, 4)), np.array([]), np.array([])
            box = pred[:, :4]
            cls_scores = pred[:, 4:]
            scores = cls_scores.max(axis=1)
            self.last_max_score = float(np.nanmax(scores)) if scores.size else 0.0
            class_ids = cls_scores.argmax(axis=1)

            mask = scores >= conf
            box = box[mask]
            scores = scores[mask]
            class_ids = class_ids[mask]
            if box.size == 0:
                return np.empty((0, 4)), np.array([]), np.array([])

            x, y, w, h = box[:, 0], box[:, 1], box[:, 2], box[:, 3]
            xyxy = np.stack([x - w / 2, y - h / 2, x + w / 2, y + h / 2], axis=1)
            if xyxy.size:
                max_coord = float(np.nanmax(xyxy))

        apply_scale = True
        if used_nms:
            if max_coord <= 1.5:
                #       
                xyxy[:, [0, 2]] *= self.input_w
                xyxy[:, [1, 3]] *= self.input_h
                max_coord = float(np.nanmax(xyxy)) if xyxy.size else max_coord
            if max_coord > max(self.input_w, self.input_h) * 1.1:
                apply_scale = False
        if apply_scale:
            #      
            xyxy[:, [0, 2]] -= pad_w
            xyxy[:, [1, 3]] -= pad_h
            xyxy /= r

        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, frame.shape[1] - 1)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, frame.shape[0] - 1)

        if used_nms:
            keep = list(range(len(scores)))
        else:
            nms_in = np.stack(
                [xyxy[:, 0], xyxy[:, 1], xyxy[:, 2] - xyxy[:, 0], xyxy[:, 3] - xyxy[:, 1]], axis=1
            )
            keep = nms_boxes(nms_in, scores, 0.45)
        return xyxy[keep], scores[keep], class_ids[keep]
