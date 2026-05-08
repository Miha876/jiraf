# coding=utf-8
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set
import socket

import cv2
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

try:
    import psycopg2
except Exception:
    psycopg2 = None

from jiraf_app.configuration import (
    DEFAULT_SCREENSHOT_DIR,
    DEFAULT_WEIGHTS,
    check_admin_password,
    load_config,
    reset_config,
    save_config,
    set_admin_password,
)
from jiraf_app.database import create_db_schema, log_event
from jiraf_app.helpers import encode_frame_png
from jiraf_app.video import enumerate_camera_names
from jiraf_app.worker import CameraWorker

"""Главное окно с табами отображения видео и админки."""

PRESET_RESOLUTIONS = [
    ("640x360", 640, 360),
    ("1280x720", 1280, 720),
    ("1600x900", 1600, 900),
    ("1920x1080", 1920, 1080),
]
PRESET_CUSTOM = "Пользовательское"
MAX_CELL_LETTER = 26
MAX_CELL_NUMBER = 10


class MainWindow(QtWidgets.QMainWindow):
    """Основное окно: превью камеры, статусы и настройки."""

    def __init__(self):
        """Готовим конфигурацию, директорию снимков и запускаем камеры."""
        super().__init__()
        self.setWindowTitle("Жираф — камерная валидация")
        self.resize(1100, 700)

        self.cfg = load_config()
        snapshot_folder = self.cfg.get("snapshot_folder")
        if snapshot_folder:
            self._screenshot_dir = Path(snapshot_folder)
        else:
            self._screenshot_dir = DEFAULT_SCREENSHOT_DIR
        self._ensure_screenshot_dir()
        self._snapshot_sequence = self._count_existing_snapshots()

        self._last_frame = None
        self._last_classes: List[str] = []
        self._db_conn = None
        self._worker = None
        self._last_status = ""
        self._last_status_ts = 0.0
        self._admin_unlocked = False

        self._build_ui()
        self._apply_theme()
        self._refresh_cameras()
        self._start_camera()
        self._camera_poll_timer = QtCore.QTimer(self)
        self._camera_poll_timer.setInterval(5000)
        self._camera_poll_timer.timeout.connect(self._poll_cameras)
        self._camera_poll_timer.start()

    def closeEvent(self, event):
        self._stop_camera()
        if self._db_conn:
            self._db_conn.close()
        event.accept()

    def _build_ui(self):
        """Создаем элементы управления и раскладываем компоновку."""
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_main = QtWidgets.QWidget()
        self.tab_admin = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_main, "Жираф")
        self.tabs.addTab(self.tab_admin, "Админка")
        # Перехватываем клики по вкладкам, чтобы требовать пароль до показа админки
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        # Защищаем также переключение вкладок с клавиатуры/программно.
        self.tabs.currentChanged.connect(self._on_tab_changed)

        main_layout = QtWidgets.QHBoxLayout(self.tab_main)

        self.preview = QtWidgets.QLabel("Нет видео")
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumSize(640, 480)
        self.preview.setStyleSheet("background:#111722; color:#98a0b3; border-radius:8px;")

        right_panel = QtWidgets.QFrame()
        right_panel.setObjectName("rightPanel")
        right_layout = QtWidgets.QVBoxLayout(right_panel)

        self.status_header = QtWidgets.QLabel("Ждём")
        self.status_header.setObjectName("statusHeader")
        right_layout.addWidget(self.status_header)

        info_grid = QtWidgets.QGridLayout()
        info_grid.addWidget(QtWidgets.QLabel("Серийный номер:"), 0, 0)
        self.serial_value = QtWidgets.QLabel("н/д")
        info_grid.addWidget(self.serial_value, 0, 1)
        info_grid.addWidget(QtWidgets.QLabel("Код модели:"), 1, 0)
        self.model_value = QtWidgets.QLabel("н/д")
        info_grid.addWidget(self.model_value, 1, 1)
        right_layout.addLayout(info_grid)

        right_layout.addWidget(QtWidgets.QLabel("Признаки"))
        self.feature_table = QtWidgets.QTableWidget(3, 2)
        self.feature_table.setHorizontalHeaderLabels(["Элемент", "Статус"])
        self.feature_table.verticalHeader().setVisible(False)
        self.feature_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.feature_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.feature_table.setShowGrid(False)
        self.feature_table.setItem(0, 0, QtWidgets.QTableWidgetItem("Коробка"))
        self.feature_table.setItem(1, 0, QtWidgets.QTableWidgetItem("Датчик"))
        self.feature_table.setItem(2, 0, QtWidgets.QTableWidgetItem("Документация"))
        for row in range(3):
            item = QtWidgets.QTableWidgetItem("Нет")
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.feature_table.setItem(row, 1, item)
        self.feature_table.horizontalHeader().setStretchLastSection(True)
        right_layout.addWidget(self.feature_table)

        right_layout.addStretch(1)

        self.snapshot_button = QtWidgets.QPushButton("Снимок")
        self.snapshot_button.clicked.connect(self._save_snapshot)
        right_layout.addWidget(self.snapshot_button)

        self.report_error_button = QtWidgets.QPushButton("Сообщить об ошибке")
        self.report_error_button.setStyleSheet("background:#b45309; color:#fff;")
        self.report_error_button.clicked.connect(self._report_error)
        right_layout.addWidget(self.report_error_button)

        main_layout.addWidget(self.preview, 3)
        main_layout.addWidget(right_panel, 1)

        admin_layout = QtWidgets.QFormLayout(self.tab_admin)

        self.camera_combo = QtWidgets.QComboBox()
        self.refresh_button = QtWidgets.QPushButton("Обновить список")
        self.refresh_button.clicked.connect(self._refresh_cameras)
        camera_row = QtWidgets.QHBoxLayout()
        camera_row.addWidget(self.camera_combo)
        camera_row.addWidget(self.refresh_button)
        admin_layout.addRow("Камера", camera_row)

        self.weights_edit = QtWidgets.QLineEdit(self.cfg.get("weights", DEFAULT_WEIGHTS))
        self.weights_button = QtWidgets.QToolButton()
        self.weights_button.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirOpenIcon))
        self.weights_button.clicked.connect(self._pick_weights)
        weights_row = QtWidgets.QHBoxLayout()
        weights_row.addWidget(self.weights_edit)
        weights_row.addWidget(self.weights_button)
        admin_layout.addRow("Веса модели", weights_row)

        self.preset_combo = QtWidgets.QComboBox()
        for label, _, _ in PRESET_RESOLUTIONS:
            self.preset_combo.addItem(label)
        self.preset_combo.addItem(PRESET_CUSTOM)
        preset = self.cfg.get("resolution_preset", "1280x720")
        idx = self.preset_combo.findText(preset)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self.frame_width_edit = QtWidgets.QSpinBox()
        self.frame_width_edit.setMaximum(4096)
        self.frame_width_edit.setValue(self.cfg.get("frame_width", 1280))
        self.frame_height_edit = QtWidgets.QSpinBox()
        self.frame_height_edit.setMaximum(4096)
        self.frame_height_edit.setValue(self.cfg.get("frame_height", 720))
        size_row = QtWidgets.QHBoxLayout()
        size_row.addWidget(self.preset_combo)
        size_row.addWidget(QtWidgets.QLabel("W"))
        size_row.addWidget(self.frame_width_edit)
        size_row.addWidget(QtWidgets.QLabel("H"))
        size_row.addWidget(self.frame_height_edit)
        admin_layout.addRow("Разрешение", size_row)

        self.conf_edit = QtWidgets.QDoubleSpinBox()
        self.conf_edit.setDecimals(2)
        self.conf_edit.setSingleStep(0.05)
        self.conf_edit.setRange(0.05, 0.99)
        self.conf_edit.setValue(float(self.cfg.get("conf", 0.8)))
        admin_layout.addRow("Порог conf", self.conf_edit)

        self.fps_edit = QtWidgets.QSpinBox()
        self.fps_edit.setRange(1, 60)
        self.fps_edit.setValue(int(self.cfg.get("fps", 15)))
        admin_layout.addRow("Ограничение FPS", self.fps_edit)

        self.admin_pass_edit = QtWidgets.QLineEdit()
        self.admin_pass_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.admin_pass_button = QtWidgets.QPushButton("Сменить пароль")
        self.admin_pass_button.clicked.connect(self._change_admin_password)
        pass_row = QtWidgets.QHBoxLayout()
        pass_row.addWidget(self.admin_pass_edit)
        pass_row.addWidget(self.admin_pass_button)
        admin_layout.addRow("Новый пароль", pass_row)

        self.reset_config_button = QtWidgets.QPushButton("Сбросить конфиг")
        self.reset_config_button.clicked.connect(self._reset_config)
        admin_layout.addRow(self.reset_config_button)

        db_cfg = self.cfg.get("db", {})
        self.db_host = QtWidgets.QLineEdit(db_cfg.get("host", "localhost"))
        self.db_port = QtWidgets.QSpinBox()
        self.db_port.setMaximum(65535)
        self.db_port.setValue(db_cfg.get("port", 5432))
        self.db_name = QtWidgets.QLineEdit(db_cfg.get("dbname", "giraffe"))
        self.db_user = QtWidgets.QLineEdit(db_cfg.get("user", "postgres"))
        self.db_password = QtWidgets.QLineEdit(db_cfg.get("password", "postgres"))
        self.db_password.setEchoMode(QtWidgets.QLineEdit.Password)

        admin_layout.addRow("DB Host", self.db_host)
        admin_layout.addRow("DB Port", self.db_port)
        admin_layout.addRow("DB Name", self.db_name)
        admin_layout.addRow("DB User", self.db_user)
        admin_layout.addRow("DB Password", self.db_password)

        self.connect_button = QtWidgets.QPushButton("Подключиться к БД")
        self.connect_button.clicked.connect(self._connect_db)
        admin_layout.addRow(self.connect_button)

        self.admin_status = QtWidgets.QLabel("Нет подключения")
        admin_layout.addRow("Статус", self.admin_status)

        self.camera_combo.currentIndexChanged.connect(self._on_camera_changed)
        self.weights_edit.editingFinished.connect(self._on_weights_changed)
        self.frame_width_edit.editingFinished.connect(self._on_resolution_changed)
        self.frame_height_edit.editingFinished.connect(self._on_resolution_changed)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.conf_edit.valueChanged.connect(self._on_conf_changed)
        self.fps_edit.valueChanged.connect(self._on_fps_changed)
        self.db_host.editingFinished.connect(self._on_db_changed)
        self.db_port.editingFinished.connect(self._on_db_changed)
        self.db_name.editingFinished.connect(self._on_db_changed)
        self.db_user.editingFinished.connect(self._on_db_changed)
        self.db_password.editingFinished.connect(self._on_db_changed)

        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(200)
        self.log_box.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        admin_layout.addRow("Логи", self.log_box)

        self._admin_controls = [
            self.camera_combo,
            self.refresh_button,
            self.weights_edit,
            self.weights_button,
            self.preset_combo,
            self.frame_width_edit,
            self.frame_height_edit,
            self.conf_edit,
            self.fps_edit,
            self.admin_pass_edit,
            self.admin_pass_button,
            self.reset_config_button,
            self.db_host,
            self.db_port,
            self.db_name,
            self.db_user,
            self.db_password,
            self.connect_button,
        ]
        self._set_admin_controls_enabled(False)

        self._sync_preset_combo()

    def _apply_theme(self):
        """Накладываем стили для интерфейса."""
        self.setStyleSheet(
            """
            QMainWindow { background: #014186; color: #e8eef7; font-size: 15px; }
            QTabWidget::pane { border: 0; }
            QTabBar::tab { background: #014186; color: #b9c7de; padding: 10px 18px; font-size: 15px; }
            QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #ffffff; }
            QLabel { color: #dfe7f4; font-size: 15px; }
            #rightPanel { background: #f5f7fb; border-radius: 10px; border: 2px solid #c2d1ea; }
            #rightPanel QLabel { color: #222; }
            #statusHeader { background: #CCD9EF; color: #1f2d3d; padding: 12px 14px; border-radius: 8px; font-weight: 800; font-size: 16px; border: 2px solid #b6c7e4; }
            QPushButton { background: #1f5fb6; color: #fff; padding: 9px 16px; border-radius: 8px; font-size: 15px; font-weight: 700; }
            QPushButton:hover { background: #2a6fd0; }
            QTableWidget { background: #ffffff; color: #222; border: 0; font-size: 15px; }
            QTableWidget::item { padding: 7px; }
            QHeaderView::section { background: #ccd9ef; padding: 8px; border: 0; color: #1f2d3d; font-weight: 700; }
            """
        )

    def _on_tab_clicked(self, idx: int):
        """Требуем пароль перед показом админки."""
        admin_idx = 1
        if idx != admin_idx:
            # При уходе с админки блокируем повторно
            self._admin_unlocked = False
            self._set_admin_controls_enabled(False)
            return
        if self._admin_unlocked:
            return
        # Сразу остаемся на главной вкладке до ввода пароля
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(0)
        self.tabs.blockSignals(False)
        self._request_admin_access()

    def _on_tab_changed(self, idx: int):
        admin_idx = 1
        if idx != admin_idx:
            self._admin_unlocked = False
            self._set_admin_controls_enabled(False)
            return
        if self._admin_unlocked:
            return
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(0)
        self.tabs.blockSignals(False)
        self._request_admin_access()

    def _request_admin_access(self):
        admin_idx = 1
        if self._ask_admin_password():
            self._admin_unlocked = True
            self._set_admin_controls_enabled(True)
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(admin_idx)
            self.tabs.blockSignals(False)
        else:
            self._admin_unlocked = False
            self._set_admin_controls_enabled(False)

    def _set_admin_controls_enabled(self, enabled: bool):
        for widget in getattr(self, "_admin_controls", []):
            widget.setEnabled(enabled)

    def _refresh_cameras(self):
        """Обновляем список доступных камер и выбираем прошлый индекс."""
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        names = enumerate_camera_names()
        if not names:
            self.camera_combo.addItem("Камеры не найдены", None)
            self.camera_combo.blockSignals(False)
            return False
        for idx in sorted(names.keys()):
            name = names.get(idx, f"Камера #{idx}")
            self.camera_combo.addItem(f"{name} (#{idx})", idx)
        current_idx = self.cfg.get("camera_index", 0)
        match = self.camera_combo.findData(current_idx)
        if match >= 0:
            self.camera_combo.setCurrentIndex(match)
        else:
            self.camera_combo.setCurrentIndex(0)
            selected = self.camera_combo.currentData()
            if selected is not None:
                self.cfg["camera_index"] = int(selected)
                save_config(self.cfg)
        self.camera_combo.blockSignals(False)
        return True

    def _poll_cameras(self):
        """Периодически ищем камеру, чтобы подхватить ее без перезапуска."""
        had_cameras = self._refresh_cameras()
        if had_cameras and (self._worker is None or not self._worker.isRunning()):
            if self.camera_combo.currentData() is not None:
                self._start_camera()

    def _start_camera(self):
        """Создаем рабочий поток и подключаем сигналы для кадра и статуса."""
        self._stop_camera()
        if hasattr(self, "camera_combo") and self.camera_combo.currentData() is None:
            self.status_header.setText("Камера не найдена")
            self._append_log("Камера не найдена: проверьте подключение и нажмите «Обновить список»")
            return
        cam_index = int(self.cfg.get("camera_index", 0))
        weights = self.cfg.get("weights", DEFAULT_WEIGHTS)
        conf = float(self.cfg.get("conf", 0.8))
        class_names = self.cfg.get("classes", ["Box", "Sensor", "Documentation"])
        frame_width = int(self.cfg.get("frame_width", 1280))
        frame_height = int(self.cfg.get("frame_height", 720))
        fps = int(self.cfg.get("fps", 15))
        self._worker = CameraWorker(cam_index, weights, frame_width, frame_height, fps, conf, class_names)
        self._worker.frame_ready.connect(self._update_frame)
        self._worker.status_changed.connect(self._set_status)
        self._worker.log_line.connect(self._append_log)
        self._worker.start()
        self._append_log(f"Camera index: {cam_index}")
        self._append_log("Стрим камеры запущен")
        self._log_db("INFO", "camera_start", f"index={cam_index}")
        self._append_log(f"Weights: {weights}")

    def _stop_camera(self):
        """Останавливаем текущий worker и ждем завершения."""
        if self._worker:
            self._worker.stop()
            self._worker.wait(2000)
            self._worker = None

    def _set_status(self, text: str):
        """Подставляем текст статуса в заголовок."""
        self.status_header.setText(text)

    def _append_log(self, text: str):
        """Добавляет строку в лог, сохраняя позицию прокрутки."""
        if not hasattr(self, "log_box"):
            return
        bar = self.log_box.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        self.log_box.appendPlainText(text)
        if at_bottom:
            bar.setValue(bar.maximum())

    def _log_db(self, level: str, event: str, details: str = "") -> None:
        if self._db_conn:
            log_event(self._db_conn, level, event, details)

    def _report_error(self):
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self,
            "Сообщить об ошибке",
            "Опишите проблему:",
            "",
        )
        if not ok:
            return
        message = text.strip()
        if not message:
            return
        self._append_log(f"Ошибка отправлена: {message}")
        self._log_db("WARN", "error_report", message)
        self._broadcast_notification(f"ERROR_REPORT {message}")
        self.status_header.setText("Сообщение отправлено")

    def _send_warehouse_full_notification(self):
        self._broadcast_notification("WAREHOUSE_FULL")

    def _broadcast_notification(self, event: str):
        try:
            payload = f"{event} {datetime.now().isoformat()}".encode("utf-8")
            targets = set(self._notification_targets())
            discovered = self._discover_admin_notifiers(targets)
            targets.update(discovered)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for host in targets:
                sock.sendto(payload, (host, 50505))
            sock.close()
        except Exception:
            pass

    def _discover_admin_notifiers(self, seeds: Set[str]) -> Set[str]:
        peers: Set[str] = set()
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe.settimeout(0.15)
            msg = f"DISCOVER_ADMIN_NOTIFIER {datetime.now().isoformat()}".encode("utf-8")
            for host in seeds:
                try:
                    probe.sendto(msg, (host, 50505))
                except Exception:
                    pass
            deadline = time.time() + 0.6
            while time.time() < deadline:
                try:
                    data, addr = probe.recvfrom(2048)
                except socket.timeout:
                    continue
                except Exception:
                    break
                text = data.decode("utf-8", errors="ignore")
                if text.startswith("ADMIN_NOTIFIER_HERE"):
                    peers.add(addr[0])
            probe.close()
        except Exception:
            pass
        return peers

    def _notification_targets(self) -> List[str]:
        targets: Set[str] = {"255.255.255.255", "127.0.0.1"}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                self_ip = probe.getsockname()[0]
                self._append_directed_broadcast(targets, self_ip)
        except Exception:
            pass
        try:
            hostname = socket.gethostname()
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            for info in infos:
                ip = info[4][0]
                self._append_directed_broadcast(targets, ip)
        except Exception:
            pass
        return sorted(targets)

    def _append_directed_broadcast(self, targets: Set[str], ip: str):
        if not ip or ip.startswith("127."):
            return
        parts = ip.split(".")
        if len(parts) != 4:
            return
        targets.add(f"{parts[0]}.{parts[1]}.{parts[2]}.255")

    def _build_cell_sequence(self) -> List[str]:
        cells = []
        for letter_idx in range(MAX_CELL_LETTER):
            letter = chr(ord("A") + letter_idx)
            for number in range(1, MAX_CELL_NUMBER + 1):
                cells.append(f"{letter}{number}")
        return cells

    def _next_free_cell(self, cur) -> Optional[str]:
        cur.execute("SELECT cell FROM snapshots WHERE cell IS NOT NULL;")
        used = set()
        for (raw_cell,) in cur.fetchall():
            if raw_cell is None:
                continue
            normalized = str(raw_cell).strip().upper()
            if normalized:
                used.add(normalized)
        for cell in self._build_cell_sequence():
            if cell not in used:
                return cell
        return None

    def _ask_admin_password(self) -> bool:
        text, ok = QtWidgets.QInputDialog.getText(
            self,
            "Админ-доступ",
            "Введите пароль:",
            QtWidgets.QLineEdit.Password,
        )
        if not ok:
            return False
        if check_admin_password(self.cfg, text):
            return True
        QtWidgets.QMessageBox.warning(self, "Ошибка", "Неверный пароль")
        self._log_db("WARN", "admin_auth_failed", "unlock")
        return False

    def _change_admin_password(self):
        new_pass = self.admin_pass_edit.text().strip()
        if not new_pass:
            return
        if not self._ask_admin_password():
            return
        set_admin_password(self.cfg, new_pass)
        save_config(self.cfg)
        self.admin_pass_edit.clear()
        self._append_log("Пароль администратора изменён")
        self._log_db("INFO", "admin_password_changed", "")

    def _reset_config(self):
        if not self._ask_admin_password():
            return
        self.cfg = reset_config()
        self._apply_config_to_ui()
        self._append_log("Конфиг сброшен")
        self._log_db("INFO", "config_reset", "")
        self._start_camera()

    def _apply_config_to_ui(self):
        widgets = [
            self.camera_combo,
            self.weights_edit,
            self.preset_combo,
            self.frame_width_edit,
            self.frame_height_edit,
            self.conf_edit,
            self.fps_edit,
            self.db_host,
            self.db_port,
            self.db_name,
            self.db_user,
            self.db_password,
        ]
        for widget in widgets:
            widget.blockSignals(True)

        self.weights_edit.setText(self.cfg.get("weights", DEFAULT_WEIGHTS))
        self.conf_edit.setValue(float(self.cfg.get("conf", 0.8)))
        self.fps_edit.setValue(int(self.cfg.get("fps", 15)))
        self.frame_width_edit.setValue(int(self.cfg.get("frame_width", 1280)))
        self.frame_height_edit.setValue(int(self.cfg.get("frame_height", 720)))

        preset = self.cfg.get("resolution_preset", "1280x720")
        preset_idx = self.preset_combo.findText(preset)
        if preset_idx >= 0:
            self.preset_combo.setCurrentIndex(preset_idx)
        else:
            self._sync_preset_combo()

        db_cfg = self.cfg.get("db", {})
        self.db_host.setText(str(db_cfg.get("host", "localhost")))
        self.db_port.setValue(int(db_cfg.get("port", 5432)))
        self.db_name.setText(str(db_cfg.get("dbname", "giraffe")))
        self.db_user.setText(str(db_cfg.get("user", "postgres")))
        self.db_password.setText(str(db_cfg.get("password", "postgres")))

        for widget in widgets:
            widget.blockSignals(False)

        folder = self.cfg.get("snapshot_folder")
        if folder:
            self._screenshot_dir = Path(folder)
        else:
            self._screenshot_dir = DEFAULT_SCREENSHOT_DIR
        self._ensure_screenshot_dir()
        self._snapshot_sequence = self._count_existing_snapshots()

        self._refresh_cameras()

    def _update_frame(self, raw_frame: np.ndarray, frame: np.ndarray, classes: list, tracking_ok: bool):
        """Переводит кадр в QPixmap и обновляет статусы детекции."""
        self._last_frame = raw_frame
        self._last_classes = classes
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QtGui.QImage(rgb.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888).copy()
        pix = QtGui.QPixmap.fromImage(qimg)
        self.preview.setPixmap(
            pix.scaled(self.preview.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        )
        normalized = {str(c).strip().lower() for c in classes}
        box_ok = "box" in normalized
        sensor_ok = "sensor" in normalized
        docs_ok = "documentation" in normalized

        mapping = {0: box_ok, 1: sensor_ok, 2: docs_ok}
        for row in range(3):
            item = self.feature_table.item(row, 1)
            if item:
                item.setText("Да" if mapping.get(row, False) else "Нет")

        all_ok = box_ok and sensor_ok and docs_ok
        status = "ОК" if all_ok else "Ждём"
        if tracking_ok:
            now = time.time()
            if status != self._last_status or now - self._last_status_ts >= 0.5:
                self.status_header.setText(status)
                self._last_status = status
                self._last_status_ts = now

    def _persist_settings(self):
        """Сохраняем текущий выбор в конфиг и на диск."""
        data = self.camera_combo.currentData()
        if data is None:
            data = 0
        self.cfg["camera_index"] = int(data)
        self.cfg["weights"] = self.weights_edit.text().strip() or DEFAULT_WEIGHTS
        self.cfg["conf"] = float(self.conf_edit.value())
        self.cfg["fps"] = int(self.fps_edit.value())
        self.cfg["frame_width"] = int(self.frame_width_edit.value())
        self.cfg["frame_height"] = int(self.frame_height_edit.value())
        self.cfg["db"] = {
            "host": self.db_host.text().strip(),
            "port": int(self.db_port.value()),
            "dbname": self.db_name.text().strip(),
            "user": self.db_user.text().strip(),
            "password": self.db_password.text(),
        }
        self.cfg["resolution_preset"] = self.preset_combo.currentText()
        self.cfg["snapshot_folder"] = str(self._screenshot_dir)
        save_config(self.cfg)

    def _sync_preset_combo(self):
        """Выбираем пресет, если manual размеры совпадают."""
        width = int(self.frame_width_edit.value())
        height = int(self.frame_height_edit.value())
        for idx, (_, w, h) in enumerate(PRESET_RESOLUTIONS):
            if w == width and h == height:
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(idx)
                self.preset_combo.blockSignals(False)
                return
        idx = self.preset_combo.findText(PRESET_CUSTOM)
        if idx >= 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)

    def _apply_preset(self, label: str):
        """Применяем выбранный пресет к полям ширины/высоты."""
        for name, w, h in PRESET_RESOLUTIONS:
            if name == label:
                self.frame_width_edit.setValue(w)
                self.frame_height_edit.setValue(h)
                return

    def _on_preset_changed(self):
        """Обрабатываем смену пресета, сохраняем и перезапускаем поток."""
        if not self._admin_unlocked:
            return
        label = self.preset_combo.currentText()
        if label == PRESET_CUSTOM:
            return
        self._apply_preset(label)
        self._persist_settings()
        self._start_camera()

    def _on_camera_changed(self):
        """Сохраняем выбор камеры и перезапускаем worker."""
        if not self._admin_unlocked:
            return
        self._persist_settings()
        self._start_camera()
        self._log_db("INFO", "camera_changed", f"index={self.cfg.get('camera_index')}")

    def _on_weights_changed(self):
        """Сохраняем путь до весов и перезапускаем камеру."""
        if not self._admin_unlocked:
            return
        self._persist_settings()
        self._start_camera()

    def _on_conf_changed(self):
        """Перезапускаем детекцию при смене порога confidence."""
        if not self._admin_unlocked:
            return
        self._persist_settings()
        if self._worker and self._worker.isRunning():
            self._worker.update_runtime_settings(conf=float(self.conf_edit.value()))
        self._append_log(f"conf обновлен: {self.conf_edit.value():.2f}")

    def _on_fps_changed(self):
        """Перезапускаем поток при смене ограничения FPS."""
        if not self._admin_unlocked:
            return
        self._persist_settings()
        if self._worker and self._worker.isRunning():
            self._worker.update_runtime_settings(fps=int(self.fps_edit.value()))
        self._append_log(f"FPS обновлен: {int(self.fps_edit.value())}")

    def _on_resolution_changed(self):
        """Синхронизируем пресет и перезапускаем поток при смене размеров."""
        if not self._admin_unlocked:
            return
        self._sync_preset_combo()
        self._persist_settings()
        self._start_camera()

    def _on_db_changed(self):
        """Сохраняем настройки БД без перезапуска камеры."""
        if not self._admin_unlocked:
            return
        self._persist_settings()

    def _pick_weights(self):
        """Открываем диалог выбора файла весов."""
        if not self._admin_unlocked:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Выбрать веса модели",
            "",
            "Model weights (*.pt *.onnx);;All files (*.*)",
        )
        if path:
            self.weights_edit.setText(path)
            self._on_weights_changed()

    def _connect_db(self):
        """Пытаемся соединиться с PostgreSQL и обновить схему."""
        if not self._admin_unlocked:
            QtWidgets.QMessageBox.warning(self, "Доступ запрещен", "Сначала войдите в админку по паролю")
            return
        if psycopg2 is None:
            self.admin_status.setText("psycopg2 не установлен")
            return

        db = self.cfg.get("db", {})
        try:
            conn = psycopg2.connect(
                host=db.get("host"),
                port=db.get("port"),
                dbname=db.get("dbname"),
                user=db.get("user"),
                password=db.get("password"),
            )
            create_db_schema(conn)
            if self._db_conn:
                self._db_conn.close()
            self._db_conn = conn
            self.admin_status.setText("БД подключена")
            self._log_db("INFO", "db_connected", "")
        except Exception as exc:
            self.admin_status.setText(f"Ошибка БД: {exc}")

    def _save_snapshot(self):
        """Копируем последний кадр и записываем в файл + БД."""
        if self._last_frame is None:
            self.status_header.setText("Нет кадра")
            return
        payload = encode_frame_png(self._last_frame)
        if payload is None:
            self.status_header.setText("Снимок не получился")
            return
        if not self._db_conn:
            self.status_header.setText("БД не подключена")
            return
        try:
            with self._db_conn.cursor() as cur:
                next_cell = self._next_free_cell(cur)
        except Exception as exc:
            self._append_log(f"Ошибка чтения ячеек БД: {exc}")
            self.status_header.setText("БД недоступна")
            return
        if next_cell is None:
            self._send_warehouse_full_notification()
            self._append_log("Свободные ячейки закончились: A1..Z10")
            self.status_header.setText("Склад заполнен")
            return
        normalized = {str(c).strip().lower() for c in self._last_classes}
        box_ok = "box" in normalized
        sensor_ok = "sensor" in normalized
        docs_ok = "documentation" in normalized
        all_ok = box_ok and sensor_ok and docs_ok
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        status_parts = []
        if box_ok:
            status_parts.append("box")
        if sensor_ok:
            status_parts.append("sensor")
        if docs_ok:
            status_parts.append("docs")
        if not status_parts:
            status_parts.append("empty")
        status_label = "_".join(status_parts)
        self._snapshot_sequence += 1
        file_name = f"{timestamp}_{status_label}_{self._snapshot_sequence:04d}.png"
        screenshot_path = self._screenshot_dir / file_name
        saved = False
        try:
            with screenshot_path.open("wb") as fh:
                fh.write(payload)
            saved = True
        except OSError as exc:
            self._append_log(f"Не удалось сохранить снимок: {exc}")
            self.status_header.setText("Снимок не сохранён")
            return
        if not saved:
            self.status_header.setText("Снимок не сохранён")
            return
        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """
                        INSERT INTO snapshots (box_ok, sensor_ok, docs_ok, all_ok, cell)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                    (
                        bool(box_ok),
                        bool(sensor_ok),
                        bool(docs_ok),
                        bool(all_ok),
                        next_cell,
                    ),
                )
            self._db_conn.commit()
        except Exception as exc:
            self._append_log(f"Ошибка записи в БД: {exc}")
            self.status_header.setText("Снимок сохранён, но БД недоступна")
            return
        self.status_header.setText("Снимок сохранён")
        self._append_log(f"Снимок записан в {screenshot_path}, ячейка {next_cell}")

    def _ensure_screenshot_dir(self):
        """Создаем папку для скриншотов, если ее нет."""
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._append_log(f"Не удалось создать {self._screenshot_dir}")

    def _count_existing_snapshots(self) -> int:
        """Считаем .png в папке, чтобы продолжить нумерацию."""
        return sum(1 for _ in self._screenshot_dir.glob("*.png"))
