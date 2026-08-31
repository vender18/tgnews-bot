"""Хранилище. Postgres (Neon) в проде, SQLite локально — один и тот же SQL.

Время везде хранится строкой ISO-8601 в UTC: одинаково сортируется и сравнивается
в обоих диалектах, не зависит от типов времени драйвера.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources_state (
    id            TEXT PRIMARY KEY,
    active        INTEGER NOT NULL DEFAULT 1,
    weight        REAL,
    etag          TEXT,
    last_modified TEXT,
    last_fetch    TEXT,
    last_ok       TEXT,
    last_error    TEXT,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    muted_until   TEXT,
    items_total   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    id           {PK},
    source_id    TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    title        TEXT,
    text         TEXT,
    url          TEXT,
    publisher    TEXT,
    lang         TEXT,
    section      TEXT,
    published_at TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    simhash      TEXT,
    entities     TEXT,
    channel      TEXT,
    geo          TEXT,
    cluster_id   INTEGER,
    dropped      INTEGER NOT NULL DEFAULT 0,
    drop_reason  TEXT,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS posts_cluster_idx ON posts (cluster_id);
CREATE INDEX IF NOT EXISTS posts_published_idx ON posts (published_at);
CREATE INDEX IF NOT EXISTS posts_channel_idx ON posts (channel, dropped);

CREATE TABLE IF NOT EXISTS clusters (
    id               {PK},
    channel          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    first_seen_at    TEXT,
    title            TEXT,
    simhash          TEXT,
    entities         TEXT,
    tokens           INTEGER NOT NULL DEFAULT 0,
    source_count     INTEGER NOT NULL DEFAULT 1,
    base_score       REAL,
    llm_score        REAL,
    score            REAL,
    llm_note         TEXT,
    status           TEXT NOT NULL DEFAULT 'new',
    drop_reason      TEXT,
    urgent           INTEGER NOT NULL DEFAULT 0,
    divergence       INTEGER NOT NULL DEFAULT 0,
    headline         TEXT,
    summary          TEXT,
    published_at     TEXT,
    published_kind   TEXT,
    votes_up         INTEGER NOT NULL DEFAULT 0,
    votes_down       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS clusters_status_idx ON clusters (status, channel);
CREATE INDEX IF NOT EXISTS clusters_updated_idx ON clusters (updated_at);

CREATE TABLE IF NOT EXISTS publications (
    id          {PK},
    channel     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    slot        TEXT,
    message_id  BIGINT,
    cluster_ids TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publications_created_idx ON publications (created_at);

CREATE TABLE IF NOT EXISTS votes (
    id         {PK},
    cluster_id INTEGER,
    source_id  TEXT,
    vote       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    slot_key TEXT PRIMARY KEY,
    ran_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id         {PK},
    day        TEXT NOT NULL,
    model      TEXT NOT NULL,
    purpose    TEXT,
    in_tokens  INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_day_idx ON llm_usage (day);

CREATE TABLE IF NOT EXISTS user_sources (
    id         TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS url_cache (
    src        TEXT PRIMARY KEY,
    resolved   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DB:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or cfg.database_url()
        self.dialect = "sqlite" if self.url.startswith("sqlite") else "pg"
        self._conn = self._connect()
        self.migrate()

    # --- подключение -------------------------------------------------------

    def _connect(self):
        if self.dialect == "sqlite":
            path = Path(self.url.replace("sqlite:///", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        import psycopg
        from psycopg.rows import dict_row

        url = self.url
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        # Neon на бесплатном тарифе засыпает — первое подключение может отвалиться
        last: Exception | None = None
        for attempt in range(4):
            try:
                return psycopg.connect(url, row_factory=dict_row, autocommit=True,
                                       connect_timeout=20)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2 + attempt * 3)
        raise RuntimeError(f"Не удалось подключиться к БД: {last}")

    def migrate(self) -> None:
        pk = ("INTEGER PRIMARY KEY AUTOINCREMENT" if self.dialect == "sqlite"
              else "SERIAL PRIMARY KEY")
        sql = SCHEMA.replace("{PK}", pk)
        cur = self._conn.cursor()
        for statement in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(statement)
        if self.dialect == "sqlite":
            self._conn.commit()
        cur.close()
        # колонки, добавленные после первого развёртывания
        self._ensure_column("clusters", "tokens", "INTEGER NOT NULL DEFAULT 0")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            if self.dialect == "sqlite":
                self._conn.commit()
        except Exception:  # noqa: BLE001 — колонка уже есть
            if self.dialect == "pg":
                self._conn.rollback() if not self._conn.autocommit else None
        finally:
            cur.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # --- примитивы ---------------------------------------------------------

    def _prepare(self, sql: str) -> str:
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(self._prepare(sql), tuple(params))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        cur = self._conn.cursor()
        cur.execute(self._prepare(sql), tuple(params))
        if self.dialect == "sqlite":
            self._conn.commit()
        cur.close()

    def insert_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int | None:
        """INSERT ... RETURNING id — поддерживают и Postgres, и SQLite 3.35+."""
        cur = self._conn.cursor()
        cur.execute(self._prepare(sql), tuple(params))
        row = cur.fetchone()
        if self.dialect == "sqlite":
            self._conn.commit()
        cur.close()
        if row is None:
            return None
        return int(dict(row)["id"])

    # --- удобные обёртки ---------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM kv WHERE k = ?", (key,))
        if not row or row["v"] is None:
            return default
        try:
            return json.loads(row["v"])
        except json.JSONDecodeError:
            return row["v"]

    def kv_set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        self.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT (k) DO UPDATE SET v = excluded.v",
            (key, payload),
        )

    def slot_done(self, slot_key: str) -> bool:
        return self.one("SELECT 1 AS x FROM schedule_runs WHERE slot_key = ?", (slot_key,)) is not None

    def mark_slot(self, slot_key: str, ran_at: str) -> None:
        self.execute(
            "INSERT INTO schedule_runs (slot_key, ran_at) VALUES (?, ?) "
            "ON CONFLICT (slot_key) DO NOTHING",
            (slot_key, ran_at),
        )


_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def chunks(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
