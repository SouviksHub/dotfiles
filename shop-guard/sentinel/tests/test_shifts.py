import datetime as dt
from zoneinfo import ZoneInfo

from sentinel.shifts import present_at, shift_log

TZ = ZoneInfo("Asia/Dhaka")


def ts(d, h, m):
    return dt.datetime(2026, 2, d, h, m, tzinfo=TZ).timestamp()


SIGHTINGS = [
    {"name": "Ravi", "camera": "till", "start_ts": ts(10, 9, 5), "end_ts": ts(10, 9, 30)},
    {"name": "Ravi", "camera": "stock", "start_ts": ts(10, 21, 40), "end_ts": ts(10, 21, 55)},
    {"name": "Karim", "camera": "till", "start_ts": ts(10, 14, 0), "end_ts": ts(10, 14, 20)},
    {"name": "Ravi", "camera": "till", "start_ts": ts(11, 10, 0), "end_ts": ts(11, 10, 5)},
]


def test_shift_log_groups_by_local_day():
    rows = shift_log(SIGHTINGS, TZ)
    ravi_10 = next(r for r in rows if r["name"] == "Ravi" and r["date"] == dt.date(2026, 2, 10))
    assert ravi_10["first"].strftime("%H:%M") == "09:05"
    assert ravi_10["last"].strftime("%H:%M") == "21:55"
    assert ravi_10["sightings"] == 2 and ravi_10["cameras"] == ["stock", "till"]
    assert rows[0]["date"] == dt.date(2026, 2, 11)  # newest first


def test_present_at_builds_suspect_list():
    assert present_at(SIGHTINGS, ts(10, 14, 10)) == ["Karim"]
    assert present_at(SIGHTINGS, ts(10, 21, 30)) == ["Ravi"]       # within 30 min slack
    assert present_at(SIGHTINGS, ts(10, 12, 0)) == []
