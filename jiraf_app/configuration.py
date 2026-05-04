# coding=utf-8
from __future__ import annotations

import json
import os
from pathlib import Path

"""Утилиты чтения/записи настроек и пути по умолчанию."""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_WEIGHTS = "yolov8n.pt"
DEFAULT_SCREENSHOT_DIR = Path(Path.cwd().anchor) / "jiraf"
DEFAULT_FPS = 15
DEFAULT_ADMIN_PASSWORD = "0098"


def _hash_password(password: str, salt: str) -> str:
    import hashlib

    data = (salt + password).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _default_admin() -> dict:
    salt = os.urandom(16).hex()
    return {
        "admin_salt": salt,
        "admin_hash": _hash_password(DEFAULT_ADMIN_PASSWORD, salt),
    }


def _default_config() -> dict:
    """Базовые значения, если файл настроек отсутствует."""
    admin = _default_admin()
    return {
        "camera_index": 0,
        "weights": DEFAULT_WEIGHTS,
        "conf": 0.8,
        "fps": DEFAULT_FPS,
        "classes": ["Box", "Sensor", "Documentation"],
        "frame_width": 1280,
        "frame_height": 720,
        "db": {
            "host": "localhost",
            "port": 5432,
            "dbname": "giraffe",
            "user": "postgres",
            "password": "postgres",
        },
        "resolution_preset": "1280x720",
        "snapshot_folder": str(DEFAULT_SCREENSHOT_DIR),
        **admin,
    }


def load_config() -> dict:
    """Возвращает конфигурацию из файла или заполняет ее значениями по умолчанию."""
    if not CONFIG_PATH.exists():
        return _default_config()
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    cfg.setdefault("classes", ["Box", "Sensor", "Documentation"])
    cfg.setdefault("conf", 0.8)
    cfg.setdefault("fps", DEFAULT_FPS)
    cfg.setdefault("frame_width", 1280)
    cfg.setdefault("frame_height", 720)
    cfg.setdefault("resolution_preset", "1280x720")
    cfg.setdefault("snapshot_folder", str(DEFAULT_SCREENSHOT_DIR))
    cfg.setdefault("db", {})
    if "admin_salt" not in cfg or "admin_hash" not in cfg:
        cfg.update(_default_admin())
    return cfg


def save_config(cfg: dict) -> None:
    """Сохраняет обновленные настройки без ASCII-экранирования."""
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2)


def reset_config() -> dict:
    """Сбрасывает конфиг к значениям по умолчанию и сохраняет файл."""
    cfg = _default_config()
    save_config(cfg)
    return cfg


def set_admin_password(cfg: dict, new_password: str) -> None:
    """Обновляет пароль администратора (хэш + соль)."""
    salt = os.urandom(16).hex()
    cfg["admin_salt"] = salt
    cfg["admin_hash"] = _hash_password(new_password, salt)


def check_admin_password(cfg: dict, password: str) -> bool:
    """Проверяет пароль администратора."""
    salt = cfg.get("admin_salt", "")
    expected = cfg.get("admin_hash", "")
    if not salt or not expected:
        return False
    return _hash_password(password, salt) == expected
