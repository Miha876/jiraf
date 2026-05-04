# coding=utf-8
from __future__ import annotations

import os
import time
from typing import List

import cv2
import numpy as np
from PySide6 import QtCore

try:
    import torch
except Exception:
    torch = None

try:
    from ultralytics import YOLO
    from ultralytics.trackers.byte_tracker import BYTETracker
    from types import SimpleNamespace
except Exception:
    YOLO = None
    BYTETracker = None
    SimpleNamespace = None

from jiraf_app.detection import DetectionResults, OnnxDetector

"""Поток обработки кадра: захват, детекция и трекинг."""


class CameraWorker(QtCore.QThread):
    """Поток Qt, который читаем кадры и триггерит детекцию + трекинг."""

    frame_ready = QtCore.Signal(np.ndarray, np.ndarray, list, bool)
    status_changed = QtCore.Signal(str)
    log_line = QtCore.Signal(str)

    def __init__(
        self,
        camera_index: int,
        weights_path: str,
        frame_width: int,
        frame_height: int,
        fps: int,
        conf: float,
        class_names: List[str],
        parent=None,
    ):
        super().__init__(parent)
        self.camera_index = camera_index
        self.weights_path = weights_path
        self._running = False
        self._model = None
        self._tracker = None
        self._device = "cpu"
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._fps = fps
        self._conf = conf
        self._onnx = False
        self._onnx_detector = None
        self._class_names = class_names or []
        self._model_ready = False
        self._tracker_disabled = False
        self._last_track_error_ts = 0.0
        self._last_error_ts = 0.0

    def stop(self):
        self._running = False

    def _init_model(self):
        """Загружает веса, выбирает ONNX или Ultralytics и подготавливает трекер."""
        if not os.path.exists(self.weights_path):
            self.status_changed.emit("Весов нет")
            return False
        ext = os.path.splitext(self.weights_path)[1].lower()
        if ext == ".onnx":
            try:
                self._onnx_detector = OnnxDetector(self.weights_path, self._class_names)
                self._onnx = True
                if BYTETracker is None:
                    self._tracker = None
                    self.status_changed.emit("BYTETracker не доступен")
                else:
                    tracker_args = SimpleNamespace(
                        track_low_thresh=0.1,
                        track_thresh=0.5,
                        track_high_thresh=0.6,
                        new_track_thresh=0.6,
                        track_buffer=30,
                        match_thresh=0.8,
                        frame_rate=30,
                        mot20=False,
                        fuse_score=True,
                    )
                    self._tracker = BYTETracker(args=tracker_args)
                self.status_changed.emit(f"Модель: {self.weights_path} (ONNX)")
                self._model_ready = True
                return True
            except Exception:
                self.status_changed.emit("ONNX не доступен")
                self._model_ready = False
                return False

        if YOLO is None:
            self.status_changed.emit("YOLO не доступен")
            self._model_ready = False
            return False

        self._device = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"
        self._model = YOLO(self.weights_path)
        if BYTETracker is None:
            self._tracker = None
            self.status_changed.emit("BYTETracker не доступен")
        else:
            tracker_args = SimpleNamespace(
                track_low_thresh=0.1,
                track_thresh=0.5,
                track_high_thresh=0.6,
                new_track_thresh=0.6,
                track_buffer=30,
                match_thresh=0.8,
                frame_rate=30,
                mot20=False,
                fuse_score=True,
            )
            self._tracker = BYTETracker(args=tracker_args)
        self.status_changed.emit(f"Модель: {self.weights_path} ({self._device})")
        self._model_ready = True
        return True

    def run(self):
        """Главный цикл захвата, предобработки и отправки кадров UI."""
        self._running = True
        from jiraf_app.video import open_camera, pick_best_resolution

        cap = open_camera(self.camera_index)
        if cap is None or not cap.isOpened():
            self.status_changed.emit("Камера недоступна")
            return

        resolution_locked = bool(self._frame_width and self._frame_height)
        if resolution_locked:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self._frame_width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self._frame_height))
        if self._fps:
            cap.set(cv2.CAP_PROP_FPS, int(self._fps))
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not resolution_locked or actual_w == 0 or actual_h == 0:
            actual_w, actual_h = pick_best_resolution(cap)
        self.status_changed.emit(f"Разрешение: {actual_w}x{actual_h}")

        self._init_model()

        target_interval = 1.0 / self._fps if self._fps else 0.0
        last_frame_ts = time.time()

        while self._running:
            try:
                ret, frame = cap.read()
            except cv2.error:
                ret = False
                frame = None
            if not ret or frame is None:
                time.sleep(0.05)
                continue

            detected_names = []
            raw_frame = frame.copy()
            tracking_ok = True
            try:
                if not self._model_ready:
                    self.frame_ready.emit(raw_frame, frame, detected_names, tracking_ok)
                    continue
                if self._onnx:
                    # ONNX-ветка возвращает сырые координаты
                    xyxy, scores, class_ids = self._onnx_detector.infer(frame, self._conf)
                    if xyxy.size > 0:
                        finite_mask = np.isfinite(xyxy).all(axis=1) & np.isfinite(scores)
                        xyxy = xyxy[finite_mask]
                        scores = scores[finite_mask]
                        class_ids = class_ids[finite_mask]
                        if xyxy.size == 0:
                            now = time.time()
                            if now - self._last_error_ts > 2.0:
                                self._last_error_ts = now
                                self.log_line.emit(
                                    f"ONNX: нет детекций, max_score={self._onnx_detector.last_max_score:.3f}, "
                                    f"pred_shape={self._onnx_detector.last_pred_shape}"
                                )
                            self.frame_ready.emit(raw_frame, frame, detected_names, tracking_ok)
                            continue
                        x1, y1, x2, y2 = xyxy[:, 0], xyxy[:, 1], xyxy[:, 2], xyxy[:, 3]
                        w = x2 - x1
                        h = y2 - y1
                        cx = x1 + w / 2
                        cy = y1 + h / 2
                        xywh = np.stack([cx, cy, w, h], axis=1)
                        size_mask = (xywh[:, 2] > 1) & (xywh[:, 3] > 1)
                        xywh = xywh[size_mask]
                        xyxy = xyxy[size_mask]
                        scores = scores[size_mask]
                        class_ids = class_ids[size_mask]
                        if xywh.size == 0:
                            self.frame_ready.emit(raw_frame, frame, detected_names, tracking_ok)
                            continue

                        class_names = []
                        for cid in class_ids:
                            if 0 <= int(cid) < len(self._class_names):
                                class_names.append(self._class_names[int(cid)])
                            else:
                                class_names.append(f"class_{int(cid)}")
                        detected_names = class_names

                        if self._tracker is not None and not self._tracker_disabled:
                            tracks = self._tracker.update(
                                DetectionResults(xywh=xywh, conf=scores, cls=class_ids),
                                frame,
                            )
                            for track in tracks:
                                x1, y1, x2, y2 = map(int, track[:4])
                                track_id = int(track[4])
                                score = float(track[5]) if len(track) > 5 else 0.0
                                class_id = int(track[6]) if len(track) > 6 else -1
                                class_name = (
                                    self._class_names[class_id]
                                    if 0 <= class_id < len(self._class_names)
                                    else "unknown"
                                )
                                label = f"{class_name} ({class_id}) ID:{track_id} {score:.2f}"
                                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                                cv2.putText(
                                    frame,
                                    label,
                                    (int(x1), int(y1) - 8),
                                    cv2.FONT_HERSHEY_SIMPLEX,
                                    0.6,
                                    (0, 255, 0),
                                    2,
                                )
                        self.frame_ready.emit(raw_frame, frame, detected_names, tracking_ok)
                        continue

                # YOLO-модель возвращает списки боксов для трекера
                results = self._model(frame, conf=self._conf, verbose=False, device=self._device)[0]
                boxes = results.boxes
                if boxes is not None and len(boxes) > 0:
                    xyxy = boxes.xyxy.cpu().numpy()
                    conf_scores = boxes.conf.cpu().numpy()
                    cls = boxes.cls.cpu().numpy()

                    x1, y1, x2, y2 = xyxy[:, 0], xyxy[:, 1], xyxy[:, 2], xyxy[:, 3]
                    w = x2 - x1
                    h = y2 - y1
                    cx = x1 + w / 2
                    cy = y1 + h / 2
                    xywh = np.stack([cx, cy, w, h], axis=1)

                    class_names = [self._model.names.get(int(c), "unknown") for c in cls]
                    detected_names = class_names

                    if self._tracker is not None and not self._tracker_disabled:
                        class_ids = cls.astype(int)
                        tracks = self._tracker.update(
                            DetectionResults(xywh=xywh, conf=conf_scores, cls=class_ids),
                            frame,
                        )
                        for track in tracks:
                            x1, y1, x2, y2 = map(int, track[:4])
                            track_id = int(track[4])
                            score = float(track[5]) if len(track) > 5 else 0.0
                            class_id = int(track[6]) if len(track) > 6 else -1
                            class_name = self._model.names.get(class_id, "unknown")
                            label = f"{class_name} ({class_id}) ID:{track_id} {score:.2f}"
                            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                            cv2.putText(
                                frame,
                                label,
                                (int(x1), int(y1) - 8),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.6,
                                (0, 255, 0),
                                2,
                            )
            except Exception:
                tracking_ok = False
                self.status_changed.emit("Ошибка трекинга")
                now = time.time()
                if now - self._last_error_ts > 2.0:
                    self._last_error_ts = now
                    try:
                        import traceback

                        self.log_line.emit(traceback.format_exc())
                    except Exception:
                        self.log_line.emit("Ошибка трекинга: неизвестная ошибка")

            self.frame_ready.emit(raw_frame, frame, detected_names, tracking_ok)

            if target_interval > 0:
                now = time.time()
                elapsed = now - last_frame_ts
                sleep_for = target_interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
                last_frame_ts = time.time()

        cap.release()
