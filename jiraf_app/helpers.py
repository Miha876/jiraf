from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

"""Маленькие хелперы, используемые в разных частях приложения."""


def encode_frame_png(frame: np.ndarray) -> Optional[bytes]:
    """Сжимает кадр в PNG и возвращает бинарный буфер или None при ошибке."""
    ok, buf = cv2.imencode(".png", frame)
    if not ok:
        return None
    return buf.tobytes()
