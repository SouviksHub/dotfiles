"""SQLite store of reviewed events."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    event_id     TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    person       TEXT,
    start_ts     REAL NOT NULL,
    end_ts       REAL,
    zones        TEXT NOT NULL DEFAULT '[]',
    reasons      TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL,          -- pending | done | error
    score        INTEGER,
    summary      TEXT,
    assessment   TEXT,                   -- full JSON from the analyzer
    evidence     TEXT,                   -- ledger entry JSON, if archived
    error        TEXT,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_start ON incidents(start_ts);
CREATE INDEX IF NOT EXISTS idx_incidents_person ON incidents(person);
"""

JSON_COLUMNS = ("zones", "reasons", "assessment", "evidence")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _write(self, sql: str, args: tuple) -> None:
        with self._lock, self.conn:
            self.conn.execute(sql, args)

    def add_pending(self, event: dict, person: str | None, reasons: list[str]) -> None:
        self._write(
            """INSERT INTO incidents (event_id, camera, person, start_ts, end_ts, zones, reasons, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
               ON CONFLICT(event_id) DO UPDATE SET person=excluded.person, end_ts=excluded.end_ts,
                 zones=excluded.zones, reasons=excluded.reasons, status='pending', updated_at=excluded.updated_at""",
            (event["id"], event["camera"], person, event["start_time"], event.get("end_time"),
             json.dumps(event.get("entered_zones") or []), json.dumps(reasons), time.time()),
        )

    def save_result(self, event_id: str, score: int, summary: str, assessment: dict, evidence: dict | None) -> None:
        self._write(
            """UPDATE incidents SET status='done', score=?, summary=?, assessment=?, evidence=?, error=NULL,
               updated_at=? WHERE event_id=?""",
            (score, summary, json.dumps(assessment), json.dumps(evidence) if evidence else None, time.time(), event_id),
        )

    def save_error(self, event_id: str, error: str) -> None:
        self._write("UPDATE incidents SET status='error', error=?, updated_at=? WHERE event_id=?",
                    (error[:2000], time.time(), event_id))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        out = dict(row)
        for col in JSON_COLUMNS:
            out[col] = json.loads(out[col]) if out.get(col) else None
        return out

    def get(self, event_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM incidents WHERE event_id=?", (event_id,)).fetchone()
        return self._row(row) if row else None

    def query(self, *, since: float = 0, until: float | None = None, person: str | None = None,
              min_score: int | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM incidents WHERE start_ts >= ?", [since]
        if until is not None:
            sql += " AND start_ts < ?"
            args.append(until)
        if person:
            sql += " AND person = ?"
            args.append(person)
        if min_score is not None:
            sql += " AND score >= ?"
            args.append(min_score)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def people(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT person FROM incidents WHERE person IS NOT NULL ORDER BY person")
        return [r[0] for r in rows]
