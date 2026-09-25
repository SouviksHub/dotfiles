"""Turns face-recognition sightings into a per-day shift log (who was in, first/last seen)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def shift_log(sightings: list[dict], tz: ZoneInfo) -> list[dict]:
    days: dict[tuple[dt.date, str], dict] = {}
    for s in sightings:
        start = dt.datetime.fromtimestamp(s["start_ts"], tz)
        end = dt.datetime.fromtimestamp(s["end_ts"], tz)
        key = (start.date(), s["name"])
        row = days.setdefault(key, {"date": start.date(), "name": s["name"], "first": start,
                                    "last": end, "sightings": 0, "cameras": set()})
        row["first"] = min(row["first"], start)
        row["last"] = max(row["last"], end)
        row["sightings"] += 1
        row["cameras"].add(s["camera"])
    rows = sorted(days.values(), key=lambda r: (r["date"], r["first"]), reverse=True)
    for r in rows:
        r["cameras"] = sorted(r["cameras"])
    return rows


def present_at(sightings: list[dict], ts: float, slack_s: float = 1800) -> list[str]:
    """Enrolled people seen within `slack_s` of a moment: the suspect list for a shortage."""
    return sorted({s["name"] for s in sightings if s["start_ts"] - slack_s <= ts <= s["end_ts"] + slack_s})
