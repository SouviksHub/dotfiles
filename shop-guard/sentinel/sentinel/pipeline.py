"""MQTT listener -> rules -> clip -> Claude review -> evidence -> alerts."""

from __future__ import annotations

import datetime as dt
import json
import logging
import queue
import threading
import time

import paho.mqtt.client as mqtt

from .analyzer import Analyzer, AnalysisError
from .db import DB
from .evidence import EvidenceLocker
from .frames import sample_frames
from .frigate import Frigate
from .notify import Telegram
from .rules import Rules, face_name

log = logging.getLogger(__name__)

POS_ALERT_TOPIC = "shopguard/pos/alert"
# POS alerts whose till footage is always archived, whatever the AI score.
ALWAYS_ARCHIVE = {"unauthorized_open", "void", "kick_without_open"}
POS_CLIP_BEFORE_S, POS_CLIP_AFTER_S = 20, 40


class Pipeline:
    def __init__(self, *, rules: Rules, db: DB, frigate: Frigate, analyzer: Analyzer,
                 locker: EvidenceLocker, telegram: Telegram, frames_per_event: int):
        self.rules = rules
        self.db = db
        self.frigate = frigate
        self.analyzer = analyzer
        self.locker = locker
        self.telegram = telegram
        self.frames_per_event = frames_per_event
        self.jobs: queue.Queue[tuple[dict, list[str]]] = queue.Queue()
        self._pinged: set[str] = set()

    # ---- event intake -------------------------------------------------------

    def on_frigate_event(self, payload: dict) -> None:
        kind, ev = payload.get("type"), payload.get("after") or {}
        if ev.get("label") != "person" or ev.get("false_positive"):
            return
        if kind in ("new", "update"):
            self._early_warning(ev)
            return
        if kind != "end":
            return
        self._pinged.discard(ev["id"])
        name = face_name(ev)
        if name and name in (self.rules.staff | self.rules.watchlist):
            start = float(ev["start_time"])
            self.db.add_sighting(ev["id"], name, ev["camera"], start, float(ev.get("end_time") or start))
        reasons = self.rules.reasons(ev)
        if not reasons:
            return
        self.db.add_pending(ev, face_name(ev), reasons)
        self.jobs.put((ev, reasons))

    def on_pos_alert(self, alert: dict) -> None:
        """Drawer/void/cash alerts from the POS: tell the owner now, review the till footage next."""
        kind = alert.get("kind", "pos")
        ts = float(alert.get("ts") or time.time())
        when = self.rules.local(ts).strftime("%a %d %b %H:%M:%S")
        who = alert.get("cashier") or "-"
        self.telegram.message(f"POS ALERT: {kind.replace('_', ' ')} at {when}\nCashier: {who}\n{alert.get('detail', '')}")
        camera = self.rules.till_camera
        if not camera or kind in ("sensor_offline", "pin_lockout", "stock_loss", "cash_short"):
            return  # nothing specific to look at on camera
        # For a void, the interesting moment is the original sale, not the void itself.
        centre = float(alert.get("sale_ts") or ts)
        ev = {"id": f"pos-{kind}-{int(ts)}", "camera": camera, "label": "person", "sub_label": alert.get("cashier"),
              "start_time": centre - POS_CLIP_BEFORE_S, "end_time": centre + POS_CLIP_AFTER_S,
              "entered_zones": [], "window": True, "pos_alert": alert}
        reasons = [f"pos:{kind}"]
        self.db.add_pending(ev, alert.get("cashier"), reasons)
        # Queue once the footage exists, so the worker never idles waiting for it.
        delay = max(0.0, ev["end_time"] + 15 - time.time())
        timer = threading.Timer(delay, self.jobs.put, args=((ev, reasons),))
        timer.daemon = True
        timer.start()

    def _early_warning(self, ev: dict) -> None:
        """Real-time ping for after-hours presence, before the event even ends."""
        if ev["id"] in self._pinged or self.rules.is_open(float(ev["start_time"])):
            return
        self._pinged.add(ev["id"])
        when = self.rules.local(float(ev["start_time"])).strftime("%a %d %b %H:%M")
        who = face_name(ev) or "unrecognised person"
        self.telegram.photo(f"AFTER HOURS: {who} on {ev['camera']} at {when}. Analysis follows.",
                            self.frigate.latest_frame(ev["camera"]))

    # ---- processing ---------------------------------------------------------

    def process(self, ev: dict, reasons: list[str]) -> None:
        event_id, person = ev["id"], face_name(ev)
        local = self.rules.local(float(ev["start_time"]))
        if ev.get("window"):
            clip = self.frigate.clip_range(ev["camera"], float(ev["start_time"]), float(ev["end_time"]))
        else:
            clip = self.frigate.clip(event_id)
        frames = sample_frames(clip, count=self.frames_per_event)
        if not frames:
            raise RuntimeError("no frames could be extracted from clip")
        context = {
            "camera": ev["camera"],
            "local_start": local.isoformat(timespec="seconds"),
            "duration_s": round(float(ev.get("end_time") or ev["start_time"]) - float(ev["start_time"]), 1),
            "business_open": self.rules.is_open(float(ev["start_time"])),
            "zones_entered": ev.get("entered_zones") or [],
            "face_recognition_tag": person,
            "why_flagged": reasons,
        }
        if ev.get("pos_alert"):
            context["pos_alert"] = ev["pos_alert"]
            context["note"] = ("This clip is the till camera around a POS alert. Focus on the cash drawer and "
                               "the cashier's hands: was cash handled, and did it go to the customer or elsewhere?")
        assessment = self.analyzer.review(frames, context)
        score = assessment.suspicion_score

        evidence = None
        watched = any(r.startswith("watchlist:") for r in reasons)
        must_archive = any(r.split(":", 1)[-1] in ALWAYS_ARCHIVE for r in reasons if r.startswith("pos:"))
        snapshot = frames[len(frames) // 2][1] if ev.get("window") else None
        if score >= self.rules.evidence_min_score or watched or must_archive:
            snapshot = snapshot or self.frigate.snapshot(event_id)
            files = {"clip.mp4": clip, **({"snapshot.jpg": snapshot} if snapshot else {})}
            evidence = self.locker.store(event_id, files, {
                **context, "score": score, "summary": assessment.summary,
                "indicators": assessment.indicators, "model": self.analyzer.model,
            })
            if not ev.get("window"):
                self.frigate.retain(event_id)

        self.db.save_result(event_id, score, assessment.summary, assessment.model_dump(), evidence)
        log.info("event %s score=%s reasons=%s", event_id, score, reasons)

        if score >= self.rules.alert_min_score:
            flags = ", ".join(i for i in assessment.indicators if i != "none") or "-"
            caption = (
                f"SUSPICION {score}/10 ({assessment.confidence})\n"
                f"{ev['camera']} - {local.strftime('%a %d %b %H:%M:%S')}\n"
                f"Person: {person or 'unrecognised'}\nFlags: {flags}\n\n"
                f"{assessment.summary}\n\nCheck: {assessment.what_to_check_in_full_clip}\n"
                f"Event: {event_id}"
            )
            self.telegram.photo(caption, snapshot or self.frigate.snapshot(event_id))

    def _worker(self) -> None:
        while True:
            ev, reasons = self.jobs.get()
            try:
                self.process(ev, reasons)
            except (AnalysisError, RuntimeError, OSError) as exc:
                log.error("event %s failed: %s", ev.get("id"), exc)
                self.db.save_error(ev["id"], str(exc))
            except Exception as exc:  # keep the worker alive no matter what
                log.exception("event %s crashed", ev.get("id"))
                self.db.save_error(ev["id"], f"{type(exc).__name__}: {exc}")
            finally:
                self.jobs.task_done()

    # ---- daily report -------------------------------------------------------

    def send_daily_report(self, day: dt.date | None = None) -> str:
        tz = self.rules.tz
        day = day or dt.datetime.now(tz).date()
        start = dt.datetime.combine(day, dt.time.min, tz).timestamp()
        incidents = self.db.query(since=start, until=start + 86400, limit=500)
        for inc in incidents:
            inc["local_time"] = self.rules.local(inc["start_ts"]).strftime("%H:%M")
        report = self.analyzer.daily_report(incidents, day.strftime("%A %d %B %Y"), str(tz))
        self.telegram.message(f"Daily CCTV report - {day:%a %d %b}\n\n{report}")
        return report

    def _scheduler(self) -> None:
        # Don't fire immediately if the service (re)starts after today's report time.
        started = dt.datetime.now(self.rules.tz)
        at = self.rules.daily_report_at
        sent_for: dt.date | None = started.date() if at and started.time() >= at else None
        while True:
            at = self.rules.daily_report_at
            now = dt.datetime.now(self.rules.tz)
            if at and now.time() >= at and sent_for != now.date():
                sent_for = now.date()
                try:
                    self.send_daily_report(now.date())
                except Exception:
                    log.exception("daily report failed")
            time.sleep(30)

    # ---- lifecycle ----------------------------------------------------------

    def start(self, mqtt_host: str, mqtt_port: int) -> None:
        threading.Thread(target=self._worker, name="review-worker", daemon=True).start()
        threading.Thread(target=self._scheduler, name="daily-report", daemon=True).start()

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sentinel")

        def on_connect(c, _userdata, _flags, reason_code, _props):
            log.info("mqtt connected: %s", reason_code)
            c.subscribe("frigate/events")
            c.subscribe(POS_ALERT_TOPIC, qos=1)

        def on_message(_c, _userdata, msg):
            try:
                payload = json.loads(msg.payload)
                if msg.topic == POS_ALERT_TOPIC:
                    self.on_pos_alert(payload)
                else:
                    self.on_frigate_event(payload)
            except Exception:
                log.exception("bad frigate event")

        client.on_connect = on_connect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(mqtt_host, mqtt_port)
        client.loop_start()
        self.mqtt = client
