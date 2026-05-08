# coding=utf-8
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

import cv2

"""Утилиты для поиска камер и настройки разрешения."""

try:
    cv2.setLogLevel(cv2.LOG_LEVEL_SILENT)
except Exception:
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass


def enumerate_cameras(max_index: int = 10) -> List[int]:
    """Возвращает индексы камер из списка имен без тяжелой проверки кадра."""
    names = enumerate_camera_names()
    if names:
        return sorted(names.keys())
    return list(range(max_index + 1))


def open_camera(index: int) -> Optional[cv2.VideoCapture]:
    """Открывает камеру с заданным индексом и проверяет, что с нее читается кадр."""
    backends = (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)
    for backend in backends:
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened() and _can_read_frame(cap):
            return cap
        cap.release()
    return None


def enumerate_camera_names() -> Dict[int, str]:
    """Читает человекочитаемые имена камер через DirectShow."""
    names = _enumerate_camera_names_directshow()
    if names:
        return names
    return _enumerate_camera_names_powershell()


def _can_read_frame(cap: cv2.VideoCapture) -> bool:
    """Некоторые виртуальные/битые камеры открываются, но не отдают кадры."""
    deadline = time.time() + 1.5
    while time.time() < deadline:
        try:
            ok, frame = cap.read()
        except cv2.error:
            return False
        if ok and frame is not None and frame.size:
            return True
        time.sleep(0.05)
    return False


def _enumerate_camera_names_directshow() -> Dict[int, str]:
    """Читает имена камер из DirectShow в порядке, близком к индексам OpenCV."""
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


def _enumerate_camera_names_powershell() -> Dict[int, str]:
    """Fallback для Windows: берем имена устройств из PnP/CIM."""
    if not sys.platform.startswith("win"):
        return {}

    script = r"""
$devices = Get-CimInstance Win32_PnPEntity |
  Where-Object {
    $_.Name -and (
      $_.PNPClass -eq 'Camera' -or
      $_.PNPClass -eq 'Image' -or
      $_.Name -match 'camera|webcam|веб'
    )
  } |
  Select-Object -ExpandProperty Name
$devices | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return {}
    return {idx: str(name) for idx, name in enumerate(parsed) if str(name).strip()}


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
