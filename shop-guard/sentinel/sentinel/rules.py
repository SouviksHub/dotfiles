"""Decides which Frigate events deserve an AI review, and why.

Pure logic, no I/O beyond loading the YAML, so it is fully unit-testable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

Window = tuple[dt.time, dt.time]


def _parse_days(key: str) -> list[int]:
    key = key.strip().lower()
    if "-" in key:
        start, end = (DAYS.index(k) for k in key.split("-", 1))
        return list(range(start, end + 1)) if start <= end else [*range(start, 7), *range(0, end + 1)]
    return [DAYS.index(key)]


def _parse_window(text: str) -> Window:
    start, end = text.split("-", 1)
    return dt.time.fromisoformat(start.strip()), dt.time.fromisoformat(end.strip())


def face_name(event: dict) -> str | None:
    """Frigate sends sub_label as null, "name" or ["name", score]."""
    sub = event.get("sub_label")
    if isinstance(sub, (list, tuple)):
        sub = sub[0] if sub else None
    return sub or None


@dataclass
class Rules:
    tz: ZoneInfo
    hours: dict[int, list[Window]]
    sensitive_zones: set[str] = field(default_factory=set)
    watchlist: set[str] = field(default_factory=set)
    staff: set[str] = field(default_factory=set)
    analyze_all_people: bool = False
    min_duration_s: float = 3.0
    alert_min_score: int = 6
    evidence_min_score: int = 4
    daily_report_at: dt.time | None = None
    till_camera: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Rules":
        hours: dict[int, list[Window]] = {d: [] for d in range(7)}
        for key, windows in (raw.get("business_hours") or {}).items():
            for day in _parse_days(key):
                hours[day] = [_parse_window(w) for w in windows or []]
        report = raw.get("daily_report_at")
        return cls(
            tz=ZoneInfo(raw.get("timezone", "UTC")),
            hours=hours,
            sensitive_zones=set(raw.get("sensitive_zones") or []),
            watchlist=set(raw.get("watchlist") or []),
            staff=set(raw.get("staff") or []),
            analyze_all_people=bool(raw.get("analyze_all_people", False)),
            min_duration_s=float(raw.get("min_duration_s", 3)),
            alert_min_score=int(raw.get("alert_min_score", 6)),
            evidence_min_score=int(raw.get("evidence_min_score", 4)),
            daily_report_at=dt.time.fromisoformat(report) if report else None,
            till_camera=raw.get("till_camera") or None,
        )

    @classmethod
    def load(cls, path: Path) -> "Rules":
        return cls.from_dict(yaml.safe_load(path.read_text()) or {})

    def local(self, ts: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(ts, self.tz)

    def is_open(self, ts: float) -> bool:
        now = self.local(ts)
        t, day = now.time(), now.weekday()
        for start, end in self.hours[day]:
            if start < end and start <= t < end:
                return True
            if start >= end and t >= start:  # window crosses midnight, evening part
                return True
        for start, end in self.hours[(day - 1) % 7]:
            if start >= end and t < end:  # yesterday's window spilling past midnight
                return True
        return False

    def reasons(self, event: dict) -> list[str]:
        """Why this finished event should be reviewed. Empty list = skip."""
        if event.get("label") != "person" or event.get("false_positive"):
            return []
        start = float(event["start_time"])
        end = float(event.get("end_time") or start)
        after_hours = not self.is_open(start)
        if end - start < self.min_duration_s and not after_hours:
            return []

        reasons: list[str] = []
        name = face_name(event)
        if name and name in self.watchlist:
            reasons.append(f"watchlist:{name}")
        zones = set(event.get("entered_zones") or []) & self.sensitive_zones
        reasons.extend(f"zone:{z}" for z in sorted(zones))
        if after_hours:
            reasons.append("after_hours")
        if not reasons and self.analyze_all_people:
            reasons.append("person")
        return reasons
