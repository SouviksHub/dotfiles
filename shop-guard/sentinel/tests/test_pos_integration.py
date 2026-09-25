import time

import sentinel.pipeline as pipeline_mod
from sentinel.analyzer import Assessment
from sentinel.db import DB
from sentinel.evidence import EvidenceLocker
from sentinel.pipeline import Pipeline
from sentinel.rules import Rules


class FakeFrigate:
    def __init__(self):
        self.ranges = []

    def clip_range(self, camera, start, end):
        self.ranges.append((camera, start, end))
        return b"mp4" * 5000

    def snapshot(self, event_id):
        raise AssertionError("window clips must not ask Frigate for an event snapshot")

    def retain(self, event_id):
        raise AssertionError("window clips have no Frigate event to retain")


class FakeAnalyzer:
    model = "fake"

    def __init__(self, score):
        self.score, self.contexts = score, []

    def review(self, frames, context):
        self.contexts.append(context)
        return Assessment(summary="Cashier pockets notes from open drawer.", actions=[], indicators=["cash_taken_or_pocketed"],
                          suspicion_score=self.score, confidence="medium", innocent_explanations=[],
                          what_to_check_in_full_clip="seconds 20-30")


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def photo(self, caption, jpeg):
        self.sent.append(("photo", caption))

    def message(self, text):
        self.sent.append(("msg", text))


def make(tmp_path, monkeypatch, score=2, till="front"):
    monkeypatch.setattr(pipeline_mod, "sample_frames", lambda clip, count: [(1.0, b"a"), (2.0, b"mid"), (3.0, b"c")])
    fr, an, tg = FakeFrigate(), FakeAnalyzer(score), FakeTelegram()
    p = Pipeline(rules=Rules.from_dict({"timezone": "Asia/Dhaka", "till_camera": till}), db=DB(tmp_path / "s.db"),
                 frigate=fr, analyzer=an, locker=EvidenceLocker(tmp_path / "ev"), telegram=tg, frames_per_event=3)
    return p, fr, an, tg


def test_unauthorized_open_is_reviewed_and_always_archived(tmp_path, monkeypatch):
    p, fr, an, tg = make(tmp_path, monkeypatch, score=2)     # low score, still archived
    ts = time.time() - 120                                    # footage already on disk
    p.on_pos_alert({"kind": "unauthorized_open", "ts": ts, "detail": "drawer opened without a POS sale"})
    assert tg.sent[0][0] == "msg" and "unauthorized open" in tg.sent[0][1]
    ev, reasons = p.jobs.get(timeout=2)
    p.process(ev, reasons)
    cam, start, end = fr.ranges[0]
    assert cam == "front" and abs(start - (ts - 20)) < 1 and abs(end - (ts + 40)) < 1
    assert "pos_alert" in an.contexts[0] and "cash drawer" in an.contexts[0]["note"]
    row = p.db.get(ev["id"])
    assert row["status"] == "done" and row["evidence"] and row["reasons"] == ["pos:unauthorized_open"]
    assert p.locker.verify()[0]


def test_void_reviews_original_sale_time_and_alerts_on_high_score(tmp_path, monkeypatch):
    p, fr, an, tg = make(tmp_path, monkeypatch, score=8)
    now = time.time() - 60
    sale_ts = now - 3000
    p.on_pos_alert({"kind": "void", "ts": now, "sale_ts": sale_ts, "cashier": "Ravi", "detail": "void_pending"})
    ev, reasons = p.jobs.get(timeout=2)
    p.process(ev, reasons)
    assert abs(fr.ranges[0][1] - (sale_ts - 20)) < 1
    assert p.db.get(ev["id"])["person"] == "Ravi"
    assert any(kind == "photo" and "SUSPICION 8/10" in text for kind, text in tg.sent)


def test_future_window_is_delayed_not_blocking(tmp_path, monkeypatch):
    p, *_ = make(tmp_path, monkeypatch)
    p.on_pos_alert({"kind": "unauthorized_open", "ts": time.time(), "detail": "x"})
    assert p.jobs.empty()                                     # queued later by a timer


def test_non_visual_alerts_only_notify(tmp_path, monkeypatch):
    p, fr, an, tg = make(tmp_path, monkeypatch)
    for kind in ("sensor_offline", "cash_short", "stock_loss", "pin_lockout"):
        p.on_pos_alert({"kind": kind, "ts": time.time() - 100, "detail": "d"})
    assert p.jobs.empty() and len(tg.sent) == 4


def test_no_till_camera_configured(tmp_path, monkeypatch):
    p, *_ , tg = make(tmp_path, monkeypatch, till=None)
    p.on_pos_alert({"kind": "unauthorized_open", "ts": time.time() - 100, "detail": "d"})
    assert p.jobs.empty() and len(tg.sent) == 1
