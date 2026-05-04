# coding=utf-8
from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtWidgets

try:
    import psycopg2
except Exception:
    psycopg2 = None

from jiraf_app.configuration import load_config
from jiraf_app.database import create_db_schema

PORT = 50505
ALLOWED_TABLES = ["snapshots", "customer_shipments", "app_logs"]


class Listener(QtCore.QThread):
    message = QtCore.Signal(str, str)

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        sock.bind(("0.0.0.0", PORT))
        host = socket.gethostname()
        while True:
            data, addr = sock.recvfrom(4096)
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                text = str(data)

            if text.startswith("DISCOVER_ADMIN_NOTIFIER"):
                reply = f"ADMIN_NOTIFIER_HERE {host}"
                try:
                    sock.sendto(reply.encode("utf-8"), addr)
                except Exception:
                    pass
                continue

            source = f"{addr[0]}:{addr[1]}"
            self.message.emit(text, source)


class AdminNotifierWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Admin Notifier + База")
        self.resize(1220, 760)

        self._db_conn = None
        self._allowed_ips: set[str] = set()
        self._last_alert_ts = 0.0
        self._panic_cooldown_sec = 30
        self._last_full_alert_ts = 0.0
        self._full_cooldown_sec = 30

        self._manage_columns: List[str] = []
        self._manage_types: Dict[str, str] = {}

        self._build_ui()
        self._apply_styles()
        self._load_db_config()

        self.listener = Listener()
        self.listener.message.connect(self.on_message)
        self.listener.start()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QtWidgets.QFrame()
        header.setObjectName("HeaderCard")
        header_layout = QtWidgets.QVBoxLayout(header)
        header_layout.setContentsMargins(14, 12, 14, 12)

        self.title = QtWidgets.QLabel("Ожидание событий")
        self.title.setObjectName("EventTitle")
        header_layout.addWidget(self.title)

        row = QtWidgets.QHBoxLayout()
        self.status_pill = QtWidgets.QLabel("IDLE")
        self.status_pill.setObjectName("StatusPill")
        self.source_label = QtWidgets.QLabel("Источник: -")
        self.source_label.setObjectName("Muted")
        row.addWidget(self.status_pill, 0)
        row.addWidget(self.source_label, 1)
        header_layout.addLayout(row)

        self.details = QtWidgets.QLabel("Слушаю UDP 50505. События: PANIC, WAREHOUSE_FULL")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        header_layout.addWidget(self.details)
        root.addWidget(header)

        content = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        content.setChildrenCollapsible(False)
        root.addWidget(content, 1)

        left = QtWidgets.QFrame()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(QtWidgets.QLabel("История уведомлений"))
        self.history = QtWidgets.QListWidget()
        left_layout.addWidget(self.history, 1)

        right = QtWidgets.QFrame()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        right_layout.addWidget(QtWidgets.QLabel("Подключение БД"))

        db_form = QtWidgets.QGridLayout()
        self.db_host = QtWidgets.QLineEdit()
        self.db_port = QtWidgets.QSpinBox()
        self.db_port.setRange(1, 65535)
        self.db_name = QtWidgets.QLineEdit()
        self.db_user = QtWidgets.QLineEdit()
        self.db_password = QtWidgets.QLineEdit()
        self.db_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.connect_button = QtWidgets.QPushButton("Подключить БД")
        self.connect_button.clicked.connect(self.connect_db)

        db_form.addWidget(QtWidgets.QLabel("Host"), 0, 0)
        db_form.addWidget(self.db_host, 0, 1)
        db_form.addWidget(QtWidgets.QLabel("Port"), 0, 2)
        db_form.addWidget(self.db_port, 0, 3)
        db_form.addWidget(QtWidgets.QLabel("DB"), 1, 0)
        db_form.addWidget(self.db_name, 1, 1)
        db_form.addWidget(QtWidgets.QLabel("User"), 1, 2)
        db_form.addWidget(self.db_user, 1, 3)
        db_form.addWidget(QtWidgets.QLabel("Password"), 2, 0)
        db_form.addWidget(self.db_password, 2, 1, 1, 3)
        db_form.addWidget(self.connect_button, 2, 4)
        right_layout.addLayout(db_form)

        self.db_status = QtWidgets.QLabel("БД: нет подключения")
        self.db_status.setObjectName("Muted")
        right_layout.addWidget(self.db_status)

        ip_row = QtWidgets.QHBoxLayout()
        self.allowed_ips_edit = QtWidgets.QLineEdit()
        self.allowed_ips_edit.setPlaceholderText("Разрешенные IP: 192.168.1.32,192.168.1.209")
        self.apply_ip_filter_button = QtWidgets.QPushButton("Применить IP")
        self.apply_ip_filter_button.clicked.connect(self.apply_ip_filter)
        ip_row.addWidget(self.allowed_ips_edit, 1)
        ip_row.addWidget(self.apply_ip_filter_button, 0)
        right_layout.addLayout(ip_row)

        self.tabs = QtWidgets.QTabWidget()
        right_layout.addWidget(self.tabs, 1)

        self.tab_ship = QtWidgets.QWidget()
        self.tab_manage = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_ship, "Отгрузка")
        self.tabs.addTab(self.tab_manage, "Управление БД")

        self._build_ship_tab()
        self._build_manage_tab()

        self.action_status = QtWidgets.QLabel("Готово")
        self.action_status.setObjectName("Muted")
        right_layout.addWidget(self.action_status)

        content.addWidget(left)
        content.addWidget(right)
        content.setSizes([310, 900])

        self._set_status_style("IDLE")

    def _build_ship_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_ship)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        filter_row = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по ячейке: A, A1, B...")
        self.search_edit.textChanged.connect(self.reload_snapshots)
        self.refresh_button = QtWidgets.QPushButton("Обновить")
        self.refresh_button.clicked.connect(self.reload_snapshots)
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.refresh_button, 0)
        layout.addLayout(filter_row)

        self.snapshots_table = QtWidgets.QTableWidget(0, 7)
        self.snapshots_table.setHorizontalHeaderLabels(["ID", "Дата", "Ячейка", "Box", "Sensor", "Docs", "All"])
        self.snapshots_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.snapshots_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.snapshots_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.snapshots_table.verticalHeader().setVisible(False)
        self.snapshots_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.snapshots_table, 1)

        ship_row = QtWidgets.QHBoxLayout()
        self.customer_name_edit = QtWidgets.QLineEdit()
        self.customer_name_edit.setPlaceholderText("Имя заказчика")
        self.ship_button = QtWidgets.QPushButton("Отправить заказчику")
        self.ship_button.clicked.connect(self.ship_selected)
        ship_row.addWidget(self.customer_name_edit, 1)
        ship_row.addWidget(self.ship_button, 0)
        layout.addLayout(ship_row)

    def _build_manage_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_manage)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        self.manage_table_combo = QtWidgets.QComboBox()
        self.manage_table_combo.addItems(ALLOWED_TABLES)
        self.manage_load_btn = QtWidgets.QPushButton("Открыть таблицу")
        self.manage_load_btn.clicked.connect(self.reload_manage_table)
        top.addWidget(QtWidgets.QLabel("Таблица"), 0)
        top.addWidget(self.manage_table_combo, 1)
        top.addWidget(self.manage_load_btn, 0)
        layout.addLayout(top)

        self.manage_table = QtWidgets.QTableWidget(0, 0)
        self.manage_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.manage_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.manage_table.setEditTriggers(
            QtWidgets.QAbstractItemView.DoubleClicked
            | QtWidgets.QAbstractItemView.SelectedClicked
            | QtWidgets.QAbstractItemView.EditKeyPressed
        )
        self.manage_table.verticalHeader().setVisible(False)
        layout.addWidget(self.manage_table, 1)

        actions = QtWidgets.QHBoxLayout()
        self.manage_save_btn = QtWidgets.QPushButton("Сохранить строку")
        self.manage_save_btn.clicked.connect(self.save_manage_row)
        self.manage_delete_btn = QtWidgets.QPushButton("Удалить строку")
        self.manage_delete_btn.clicked.connect(self.delete_manage_row)
        self.manage_truncate_btn = QtWidgets.QPushButton("Очистить таблицу")
        self.manage_truncate_btn.clicked.connect(self.truncate_manage_table)
        actions.addWidget(self.manage_save_btn)
        actions.addWidget(self.manage_delete_btn)
        actions.addWidget(self.manage_truncate_btn)
        layout.addLayout(actions)

    def _apply_styles(self):
        self.setStyleSheet(
            """
            QWidget { background:#061b31; color:#e6edf7; font-family:'Segoe UI'; font-size:14px; }
            #HeaderCard { background:#0b2744; border:1px solid #2b4e72; border-radius:12px; }
            #EventTitle { font-size:22px; font-weight:800; color:#ffffff; }
            #Muted { color:#a7bdd6; }
            #StatusPill { font-weight:700; border-radius:10px; padding:6px 12px; min-width:120px; }
            QTabBar::tab { background:#123355; color:#dce8f6; padding:8px 12px; border-top-left-radius:6px; border-top-right-radius:6px; }
            QTabBar::tab:selected { background:#1a4770; color:#ffffff; }
            QLineEdit, QSpinBox, QComboBox { background:#0d2947; border:1px solid #34597f; border-radius:8px; padding:6px 8px; }
            QPushButton { background:#1768c5; border:1px solid #2f7cd4; border-radius:8px; padding:8px 12px; color:#ffffff; font-weight:700; }
            QPushButton:hover { background:#2381e0; }
            QTableWidget, QListWidget { background:#0b223b; border:1px solid #2a4b70; border-radius:10px; }
            QHeaderView::section { background:#123355; color:#dce8f6; border:0; padding:8px; font-weight:700; }
            QTableWidget::item { padding:6px; }
            """
        )

    def _load_db_config(self):
        cfg = load_config()
        db = cfg.get("db", {})
        self.db_host.setText(str(db.get("host", "localhost")))
        self.db_port.setValue(int(db.get("port", 5432)))
        self.db_name.setText(str(db.get("dbname", "postgres")))
        self.db_user.setText(str(db.get("user", "postgres")))
        self.db_password.setText(str(db.get("password", "")))

    def _set_status_style(self, status: str):
        styles = {
            "IDLE": "background:#34495e; color:#e7edf6;",
            "PANIC": "background:#b42318; color:#ffffff;",
            "WAREHOUSE_FULL": "background:#b54708; color:#ffffff;",
            "INFO": "background:#175cd3; color:#ffffff;",
        }
        self.status_pill.setStyleSheet(styles.get(status, styles["INFO"]))
        self.status_pill.setText(status)

    def on_message(self, text: str, source: str):
        source_ip = source.split(":", 1)[0].strip()
        if not self._allowed_ips:
            return
        if source_ip not in self._allowed_ips:
            return

        stamp = QtCore.QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
        event = str(text).strip()
        kind = "INFO"

        if event.startswith("PANIC"):
            kind = "PANIC"
            self.title.setText("ПАНИКА")
            self.details.setText(event)
        elif event.startswith("WAREHOUSE_FULL"):
            kind = "WAREHOUSE_FULL"
            self.title.setText("Склад заполнен")
            self.details.setText("Свободные ячейки A1..Z10 закончились")
        else:
            self.title.setText("Новое событие")
            self.details.setText(event)

        self._set_status_style(kind)
        self.source_label.setText(f"Источник: {source}")
        self.history.insertItem(0, f"[{stamp}] {kind} | {source} | {event}")
        while self.history.count() > 300:
            self.history.takeItem(self.history.count() - 1)

        self.raise_()
        self.activateWindow()

        now = QtCore.QTime.currentTime().msecsSinceStartOfDay() / 1000.0
        if kind == "PANIC" and now - self._last_alert_ts >= self._panic_cooldown_sec:
            self._last_alert_ts = now
            QtWidgets.QMessageBox.warning(self, "ПАНИКА", "Ожидайте администратора")
        if kind == "WAREHOUSE_FULL" and now - self._last_full_alert_ts >= self._full_cooldown_sec:
            self._last_full_alert_ts = now
            QtWidgets.QMessageBox.warning(self, "Склад заполнен", "Свободные ячейки A1..Z10 закончились")

    def apply_ip_filter(self):
        raw = self.allowed_ips_edit.text().strip()
        if not raw:
            self._allowed_ips = set()
            self.action_status.setText("IP не задан: уведомления отключены")
            return
        ips = [item.strip() for item in raw.split(",") if item.strip()]
        valid = []
        for ip in ips:
            parts = ip.split(".")
            if len(parts) != 4:
                continue
            if all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                valid.append(ip)
        self._allowed_ips = set(valid)
        if not self._allowed_ips:
            self.action_status.setText("Неверный IP. Пример: 192.168.1.32")
            return
        self.action_status.setText(f"Разрешены IP: {', '.join(sorted(self._allowed_ips))}")

    def connect_db(self):
        if psycopg2 is None:
            self.db_status.setText("БД: psycopg2 не установлен")
            return

        try:
            conn = psycopg2.connect(
                host=self.db_host.text().strip(),
                port=int(self.db_port.value()),
                dbname=self.db_name.text().strip(),
                user=self.db_user.text().strip(),
                password=self.db_password.text(),
            )
            create_db_schema(conn)
            if self._db_conn:
                self._db_conn.close()
            self._db_conn = conn
            self.db_status.setText("БД: подключено")
            self.action_status.setText("БД подключена")
            self.reload_snapshots()
            self.reload_manage_table()
        except Exception as exc:
            self.db_status.setText(f"БД: ошибка подключения ({exc})")

    def _fetch_snapshots(self, prefix: str) -> List[Tuple[Any, ...]]:
        if not self._db_conn:
            return []
        norm = prefix.strip().upper()
        pattern = f"{norm}%" if norm else "%"
        with self._db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, cell, box_ok, sensor_ok, docs_ok, all_ok
                FROM snapshots
                WHERE cell IS NOT NULL
                  AND UPPER(cell) LIKE %s
                ORDER BY cell ASC, created_at ASC;
                """,
                (pattern,),
            )
            return cur.fetchall()

    def reload_snapshots(self):
        if not self._db_conn:
            self.snapshots_table.setRowCount(0)
            return
        try:
            rows = self._fetch_snapshots(self.search_edit.text())
        except Exception as exc:
            self.action_status.setText(f"Ошибка чтения snapshots: {exc}")
            return

        self.snapshots_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                str(row[0]),
                str(row[1]),
                str(row[2]),
                "Да" if row[3] else "Нет",
                "Да" if row[4] else "Нет",
                "Да" if row[5] else "Нет",
                "Да" if row[6] else "Нет",
            ]
            for c, value in enumerate(values):
                it = QtWidgets.QTableWidgetItem(value)
                if c in (0, 3, 4, 5, 6):
                    it.setTextAlignment(QtCore.Qt.AlignCenter)
                self.snapshots_table.setItem(r, c, it)

        self.action_status.setText(f"Найдено записей: {len(rows)}")

    def _selected_snapshot_id(self) -> Optional[int]:
        row = self.snapshots_table.currentRow()
        if row < 0:
            return None
        item = self.snapshots_table.item(row, 0)
        if not item:
            return None
        try:
            return int(item.text())
        except Exception:
            return None

    def ship_selected(self):
        if not self._db_conn:
            self.action_status.setText("Сначала подключите БД")
            return

        snapshot_id = self._selected_snapshot_id()
        if snapshot_id is None:
            self.action_status.setText("Выберите запись в таблице")
            return

        customer_name = self.customer_name_edit.text().strip()
        if not customer_name:
            self.action_status.setText("Введите имя заказчика")
            return

        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, created_at, box_ok, sensor_ok, docs_ok, all_ok, cell
                    FROM snapshots
                    WHERE id = %s
                    FOR UPDATE;
                    """,
                    (snapshot_id,),
                )
                row = cur.fetchone()
                if not row:
                    self._db_conn.rollback()
                    self.action_status.setText("Запись уже обработана или удалена")
                    self.reload_snapshots()
                    return

                cur.execute(
                    """
                    INSERT INTO customer_shipments (
                        source_snapshot_id,
                        snapshot_created_at,
                        box_ok,
                        sensor_ok,
                        docs_ok,
                        all_ok,
                        cell,
                        customer_name
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        row[0],
                        row[1],
                        bool(row[2]),
                        bool(row[3]),
                        bool(row[4]),
                        bool(row[5]),
                        row[6],
                        customer_name,
                    ),
                )

                cur.execute("DELETE FROM snapshots WHERE id = %s;", (snapshot_id,))
                cur.execute(
                    """
                    INSERT INTO app_logs (level, event, details)
                    VALUES (%s, %s, %s);
                    """,
                    (
                        "INFO",
                        "snapshot_shipped",
                        f"snapshot_id={snapshot_id}, cell={row[6]}, customer={customer_name}",
                    ),
                )
            self._db_conn.commit()
        except Exception as exc:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            self.action_status.setText(f"Ошибка отправки: {exc}")
            return

        self.customer_name_edit.clear()
        self.action_status.setText(f"Запись {snapshot_id} отправлена заказчику '{customer_name}'")
        self.reload_snapshots()
        if self.manage_table_combo.currentText() in ("snapshots", "customer_shipments", "app_logs"):
            self.reload_manage_table()

    def _manage_table_name(self) -> Optional[str]:
        name = self.manage_table_combo.currentText().strip()
        if name not in ALLOWED_TABLES:
            return None
        return name

    def _fetch_table_schema(self, table: str) -> List[Tuple[str, str]]:
        with self._db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position;
                """,
                (table,),
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    def reload_manage_table(self):
        if not self._db_conn:
            self.manage_table.setRowCount(0)
            self.manage_table.setColumnCount(0)
            return
        table = self._manage_table_name()
        if not table:
            self.action_status.setText("Неизвестная таблица")
            return

        try:
            schema = self._fetch_table_schema(table)
            with self._db_conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} ORDER BY id DESC;")
                rows = cur.fetchall()
        except Exception as exc:
            self.action_status.setText(f"Ошибка чтения таблицы {table}: {exc}")
            return

        self._manage_columns = [c for c, _ in schema]
        self._manage_types = {c: t for c, t in schema}

        self.manage_table.setColumnCount(len(self._manage_columns))
        self.manage_table.setHorizontalHeaderLabels(self._manage_columns)
        self.manage_table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if isinstance(value, bool):
                    text = "true" if value else "false"
                elif value is None:
                    text = ""
                else:
                    text = str(value)
                item = QtWidgets.QTableWidgetItem(text)
                if self._manage_columns[c] in ("id", "created_at"):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.manage_table.setItem(r, c, item)

        self.manage_table.resizeColumnsToContents()
        self.action_status.setText(f"Таблица {table}: строк {len(rows)}")

    def _selected_manage_row(self) -> Optional[int]:
        row = self.manage_table.currentRow()
        if row < 0:
            return None
        return row

    def _coerce_value(self, raw: str, db_type: str):
        text = raw.strip()
        lowered = text.lower()

        if text == "":
            return None
        if db_type in ("integer", "bigint", "smallint"):
            return int(text)
        if db_type in ("boolean",):
            if lowered in ("true", "1", "yes", "y", "да"):
                return True
            if lowered in ("false", "0", "no", "n", "нет"):
                return False
            raise ValueError(f"Некорректный boolean: {text}")
        return text

    def save_manage_row(self):
        if not self._db_conn:
            self.action_status.setText("Сначала подключите БД")
            return
        table = self._manage_table_name()
        if not table:
            self.action_status.setText("Неизвестная таблица")
            return
        row_idx = self._selected_manage_row()
        if row_idx is None:
            self.action_status.setText("Выберите строку")
            return
        if "id" not in self._manage_columns:
            self.action_status.setText("В таблице нет id")
            return

        id_col = self._manage_columns.index("id")
        id_item = self.manage_table.item(row_idx, id_col)
        if not id_item:
            self.action_status.setText("Не найден id")
            return
        row_id = int(id_item.text())

        sets = []
        values: List[Any] = []
        for col_idx, col_name in enumerate(self._manage_columns):
            if col_name in ("id", "created_at"):
                continue
            cell = self.manage_table.item(row_idx, col_idx)
            raw = "" if cell is None else cell.text()
            db_type = self._manage_types.get(col_name, "text")
            try:
                coerced = self._coerce_value(raw, db_type)
            except Exception as exc:
                self.action_status.setText(f"Ошибка поля '{col_name}': {exc}")
                return
            sets.append(f"{col_name} = %s")
            values.append(coerced)

        if not sets:
            self.action_status.setText("Нет редактируемых полей")
            return

        values.append(row_id)
        sql = f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s;"

        try:
            with self._db_conn.cursor() as cur:
                cur.execute(sql, tuple(values))
                cur.execute(
                    """
                    INSERT INTO app_logs (level, event, details)
                    VALUES (%s, %s, %s);
                    """,
                    ("INFO", "admin_row_updated", f"table={table}, id={row_id}"),
                )
            self._db_conn.commit()
        except Exception as exc:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            self.action_status.setText(f"Ошибка сохранения: {exc}")
            return

        self.action_status.setText(f"Строка сохранена: {table}.id={row_id}")
        self.reload_manage_table()
        self.reload_snapshots()

    def delete_manage_row(self):
        if not self._db_conn:
            self.action_status.setText("Сначала подключите БД")
            return
        table = self._manage_table_name()
        if not table:
            self.action_status.setText("Неизвестная таблица")
            return
        row_idx = self._selected_manage_row()
        if row_idx is None:
            self.action_status.setText("Выберите строку")
            return
        if "id" not in self._manage_columns:
            self.action_status.setText("В таблице нет id")
            return

        id_col = self._manage_columns.index("id")
        id_item = self.manage_table.item(row_idx, id_col)
        if not id_item:
            self.action_status.setText("Не найден id")
            return
        row_id = int(id_item.text())

        ok = QtWidgets.QMessageBox.question(
            self,
            "Подтвердить",
            f"Удалить строку {table}.id={row_id}?",
        )
        if ok != QtWidgets.QMessageBox.Yes:
            return

        try:
            with self._db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table} WHERE id = %s;", (row_id,))
                cur.execute(
                    """
                    INSERT INTO app_logs (level, event, details)
                    VALUES (%s, %s, %s);
                    """,
                    ("WARN", "admin_row_deleted", f"table={table}, id={row_id}"),
                )
            self._db_conn.commit()
        except Exception as exc:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            self.action_status.setText(f"Ошибка удаления: {exc}")
            return

        self.action_status.setText(f"Удалено: {table}.id={row_id}")
        self.reload_manage_table()
        self.reload_snapshots()

    def truncate_manage_table(self):
        if not self._db_conn:
            self.action_status.setText("Сначала подключите БД")
            return
        table = self._manage_table_name()
        if not table:
            self.action_status.setText("Неизвестная таблица")
            return

        ok = QtWidgets.QMessageBox.question(
            self,
            "Подтвердить",
            f"Очистить всю таблицу {table}?",
        )
        if ok != QtWidgets.QMessageBox.Yes:
            return

        try:
            with self._db_conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY;")
                cur.execute(
                    """
                    INSERT INTO app_logs (level, event, details)
                    VALUES (%s, %s, %s);
                    """,
                    ("WARN", "admin_table_truncated", f"table={table}"),
                )
            self._db_conn.commit()
        except Exception as exc:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            self.action_status.setText(f"Ошибка очистки: {exc}")
            return

        self.action_status.setText(f"Таблица очищена: {table}")
        self.reload_manage_table()
        self.reload_snapshots()


def main():
    app = QtWidgets.QApplication([])
    win = AdminNotifierWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
