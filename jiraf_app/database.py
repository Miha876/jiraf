# coding=utf-8
from __future__ import annotations

from typing import Optional

"""Создание схемы БД, используемой для хранения снимков и логов."""


def create_db_schema(conn) -> None:  # psycopg2 connection
    """Убедиться, что таблицы существуют с нужными полями."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(0),
                box_ok BOOLEAN,
                sensor_ok BOOLEAN,
                docs_ok BOOLEAN,
                all_ok BOOLEAN,
                cell TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_logs (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(0),
                level TEXT,
                event TEXT,
                details TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_shipments (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(0),
                source_snapshot_id INTEGER,
                snapshot_created_at TIMESTAMPTZ,
                box_ok BOOLEAN,
                sensor_ok BOOLEAN,
                docs_ok BOOLEAN,
                all_ok BOOLEAN,
                cell TEXT,
                customer_name TEXT NOT NULL
            );
            """
        )
        # Обновляем таблицу, чтобы перекрыть старые схемы
        cur.execute("ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS box_ok BOOLEAN;")
        cur.execute("ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS sensor_ok BOOLEAN;")
        cur.execute("ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS docs_ok BOOLEAN;")
        cur.execute("ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS all_ok BOOLEAN;")
        cur.execute("ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS cell TEXT;")
        cur.execute("ALTER TABLE snapshots DROP COLUMN IF EXISTS image;")
        cur.execute("ALTER TABLE app_logs ADD COLUMN IF NOT EXISTS level TEXT;")
        cur.execute("ALTER TABLE app_logs ADD COLUMN IF NOT EXISTS event TEXT;")
        cur.execute("ALTER TABLE app_logs ADD COLUMN IF NOT EXISTS details TEXT;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS source_snapshot_id INTEGER;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS snapshot_created_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS box_ok BOOLEAN;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS sensor_ok BOOLEAN;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS docs_ok BOOLEAN;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS all_ok BOOLEAN;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS cell TEXT;")
        cur.execute("ALTER TABLE customer_shipments ADD COLUMN IF NOT EXISTS customer_name TEXT;")
    conn.commit()


def log_event(conn, level: str, event: str, details: Optional[str] = None) -> None:
    """Записывает событие в таблицу app_logs (без исключений наружу)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_logs (level, event, details)
                VALUES (%s, %s, %s)
                """,
                (level, event, details),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
