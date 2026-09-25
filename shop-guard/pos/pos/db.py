"""SQLite schema and a hash-chained audit log for every business action."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    role      TEXT NOT NULL CHECK (role IN ('owner', 'cashier')),
    pin_hash  TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY,
    barcode        TEXT UNIQUE,
    name           TEXT NOT NULL,
    generic        TEXT,
    strength       TEXT,
    form           TEXT,
    manufacturer   TEXT,
    price          INTEGER NOT NULL CHECK (price >= 0),   -- paisa
    cost           INTEGER NOT NULL DEFAULT 0,            -- paisa
    stock          INTEGER NOT NULL DEFAULT 0,
    reorder_level  INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE TABLE IF NOT EXISTS shifts (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    opened_at      REAL NOT NULL,
    opening_float  INTEGER NOT NULL,
    closed_at      REAL,
    counted_cash   INTEGER,
    expected_cash  INTEGER
);
CREATE TABLE IF NOT EXISTS sales (
    id           INTEGER PRIMARY KEY,
    receipt_no   TEXT UNIQUE,
    ts           REAL NOT NULL,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    shift_id     INTEGER NOT NULL REFERENCES shifts(id),
    total        INTEGER NOT NULL,
    method       TEXT NOT NULL CHECK (method IN ('cash', 'bkash', 'nagad', 'card')),
    tendered     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'completed'
                 CHECK (status IN ('completed', 'void_pending', 'voided')),
    void_reason  TEXT,
    voided_by    INTEGER REFERENCES users(id),
    voided_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_sales_ts ON sales(ts);
CREATE TABLE IF NOT EXISTS sale_items (
    id          INTEGER PRIMARY KEY,
    sale_id     INTEGER NOT NULL REFERENCES sales(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    name        TEXT NOT NULL,
    qty         INTEGER NOT NULL CHECK (qty > 0),
    price       INTEGER NOT NULL,
    line_total  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL CHECK (reason IN ('sale', 'void', 'receive', 'count', 'adjust')),
    ref         TEXT,
    user_id     INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS payouts (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    shift_id  INTEGER NOT NULL REFERENCES shifts(id),
    user_id   INTEGER NOT NULL REFERENCES users(id),
    amount    INTEGER NOT NULL CHECK (amount > 0),
    reason    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drawer_events (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,     -- kick | open | closed | alert
    sale_id  INTEGER,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_drawer_ts ON drawer_events(ts);
CREATE TABLE IF NOT EXISTS audit (
    seq     INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    data    TEXT NOT NULL,
    prev    TEXT NOT NULL,
    hash    TEXT NOT NULL
);
"""

GENESIS = "0" * 64


def _entry_hash(seq: int, ts: float, actor: str, action: str, data: str, prev: str) -> str:
    payload = json.dumps([seq, round(ts, 6), actor, action, data, prev], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    @contextmanager
    def tx(self):
        """One atomic transaction; serialised across threads."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def audit(self, conn: sqlite3.Connection, actor: str, action: str, data: dict) -> None:
        """Append to the hash chain. Must be called inside tx()."""
        last = conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev = last["hash"] if last else GENESIS
        ts = time.time()
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO audit (seq, ts, actor, action, data, prev, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, ts, actor, action, blob, prev, _entry_hash(seq, ts, actor, action, blob, prev)),
        )

    def verify_audit(self) -> tuple[bool, list[str]]:
        problems, prev, expected_seq = [], GENESIS, 1
        for r in self.conn.execute("SELECT * FROM audit ORDER BY seq"):
            if r["seq"] != expected_seq:
                problems.append(f"gap before seq {r['seq']} (expected {expected_seq})")
            if r["prev"] != prev:
                problems.append(f"seq {r['seq']}: chain broken")
            if _entry_hash(r["seq"], r["ts"], r["actor"], r["action"], r["data"], r["prev"]) != r["hash"]:
                problems.append(f"seq {r['seq']}: entry modified")
            prev, expected_seq = r["hash"], r["seq"] + 1
        return not problems, problems

    def backup(self, dst: Path) -> None:
        """Consistent online snapshot (safe while the till is in use)."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        target = sqlite3.connect(str(tmp))
        try:
            with self.lock:
                self.conn.backup(target)
        finally:
            target.close()
        tmp.replace(dst)

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None
