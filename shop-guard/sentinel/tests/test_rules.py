import datetime as dt
from zoneinfo import ZoneInfo

from sentinel.rules import Rules, face_name

TZ = ZoneInfo("Australia/Sydney")
RULES = Rules.from_dict({
    "timezone": "Australia/Sydney",
    "business_hours": {"mon-fri": ["08:30-19:00"], "sat": ["22:00-02:00"], "sun": []},
    "sensitive_zones": ["cash_counter"],
    "watchlist": ["Ravi"],
    "min_duration_s": 3,
})


def ts(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


def ev(start, dur=10, **kw):
    return {"id": "e1", "camera": "front", "label": "person", "start_time": start,
            "end_time": start + dur, **kw}


# 2026-09-21 is a Monday, 2026-09-26 Saturday, 2026-09-27 Sunday.
def test_business_hours():
    assert RULES.is_open(ts(2026, 9, 21, 9, 0))
    assert not RULES.is_open(ts(2026, 9, 21, 19, 0))
    assert not RULES.is_open(ts(2026, 9, 21, 8, 29))


def test_window_crossing_midnight():
    assert RULES.is_open(ts(2026, 9, 26, 23, 0))   # Saturday evening part
    assert RULES.is_open(ts(2026, 9, 27, 1, 30))   # spills into Sunday
    assert not RULES.is_open(ts(2026, 9, 27, 2, 0))


def test_routine_person_skipped():
    assert RULES.reasons(ev(ts(2026, 9, 21, 10, 0))) == []


def test_sensitive_zone_and_watchlist():
    e = ev(ts(2026, 9, 21, 10, 0), entered_zones=["cash_counter", "aisle"], sub_label=["Ravi", 0.93])
    assert RULES.reasons(e) == ["watchlist:Ravi", "zone:cash_counter"]


def test_after_hours_ignores_min_duration():
    assert RULES.reasons(ev(ts(2026, 9, 21, 23, 0), dur=1)) == ["after_hours"]


def test_short_blip_during_hours_skipped():
    assert RULES.reasons(ev(ts(2026, 9, 21, 10, 0), dur=1, entered_zones=["cash_counter"])) == []


def test_non_person_and_false_positive_skipped():
    assert RULES.reasons(ev(ts(2026, 9, 21, 23, 0), label="car")) == []
    assert RULES.reasons(ev(ts(2026, 9, 21, 23, 0), false_positive=True)) == []


def test_face_name_shapes():
    assert face_name({"sub_label": ["Ravi", 0.9]}) == "Ravi"
    assert face_name({"sub_label": "Ravi"}) == "Ravi"
    assert face_name({"sub_label": None}) is None
    assert face_name({"sub_label": []}) is None
