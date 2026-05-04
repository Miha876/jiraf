# coding=utf-8
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import cv2

"""Утилиты для поиска камер и настройки разрешения."""


def enumerate_cameras(max_index: int = 10) -> List[int]:
    """Пробует открыть камеры по индексам и возвращает доступные."""
    names = enumerate_camera_names()
    max_probe = max_index
    if names:
        max_probe = max(max_index, max(names.keys()))
    available = []
    for idx in range(max_probe + 1):
        cap = open_camera(idx)
        if cap is not None and cap.isOpened():
            available.append(idx)
        if cap is not None:
            cap.release()
    return available


def open_camera(index: int) -> Optional[cv2.VideoCapture]:
    """Открывает камеру с заданным индексом, перебирая доступные бэкенды."""
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            return cap
        cap.release()
    return None


def enumerate_camera_names() -> Dict[int, str]:
    """Читает человекочитаемые имена камер через DirectShow."""
    names: Dict[int, str] = {}
    try:
        import comtypes.client  # type: ignore
        from comtypes import GUID, CoInitialize, CoUninitialize  # type: ignore

        CLSID_SystemDeviceEnum = GUID("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
        IID_ICreateDevEnum = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
        CLSID_VideoInputDeviceCategory = GUID("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")

        CoInitialize()
        try:
            dev_enum = comtypes.client.CreateObject(CLSID_SystemDeviceEnum, interface=comtypes.IUnknown)
            create_enum = dev_enum.QueryInterface(IID_ICreateDevEnum)
            moniker_enum = create_enum.CreateClassEnumerator(CLSID_VideoInputDeviceCategory, 0)
            if moniker_enum is None:
                return names

            i = 0
            while True:
                try:
                    monikers = moniker_enum.Next(1)
                except Exception:
                    break
                if not monikers:
                    break
                moniker = monikers[0]
                prop_bag = moniker.BindToStorage(None, None, GUID("{55272A00-42CB-11CE-8135-00AA004BB851}"))
                if prop_bag is None:
                    continue
                try:
                    name = prop_bag.Read("FriendlyName")[0]
                except Exception:
                    name = f"Camera {i}"
                names[i] = name
                i += 1
        finally:
            CoUninitialize()
    except Exception:
        names = {}  # Если DirectShow недоступен, вернем пустой набор

    return names


def pick_best_resolution(cap: cv2.VideoCapture) -> Tuple[int, int]:
    """Пытается выставить максимальное разрешение из списка кандидатов."""
    candidates = [
        (3840, 2160),
        (2560, 1440),
        (1920, 1080),
        (1600, 900),
        (1280, 720),
        (1024, 768),
        (800, 600),
        (640, 480),
    ]
    best = (0, 0)
    for w, h in candidates:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(w))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(h))
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_w * actual_h > best[0] * best[1]:
            best = (actual_w, actual_h)
        if actual_w >= w and actual_h >= h:
            return actual_w, actual_h
    return best
