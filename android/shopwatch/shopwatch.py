#!/usr/bin/env python3
"""shopwatch: person/zone/tamper alerts for Tapo (RTSP) cameras on a phone.

Pipeline per camera
  ffmpeg (low-res substream, N fps) -> motion gate -> YOLO person detector
    -> rules (after-hours intrusion, zone dwell, camera down/covered)
    -> Telegram alert with annotated snapshot + HD clip saved from a rolling buffer
A second ffmpeg per camera stream-copies the HD stream into 60 s segments
(no decode, ~0 CPU). Segments older than buffer_minutes are deleted unless an
event hard-linked them into its evidence folder.

Usage: shopwatch.py config.toml
"""
import datetime as dt
import json
import logging
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import onnxruntime as ort

W, H = 640, 360          # detection frame size
SEG_SECONDS = 60         # HD buffer segment length
log = logging.getLogger("shopwatch")


# ------------------------------------------------------------------ config
class Cfg:
    def __init__(self, path):
        with open(path, "rb") as f:
            c = tomllib.load(f)
        g = c.get("general", {})
        self.tz = ZoneInfo(g.get("timezone", "UTC"))
        self.data = Path(os.path.expanduser(g.get("data_dir", "~/shopwatch/data")))
        self.model = os.path.expanduser(g.get("model", "~/shopwatch/yolo11n-320.onnx"))
        self.open_hours = g.get("open_hours", "09:00-21:00")
        self.fps = float(g.get("detect_fps", 2))
        self.threads = int(g.get("threads", 3))
        self.conf = float(g.get("person_conf", 0.45))
        self.buffer_min = int(g.get("buffer_minutes", 20))
        self.retention_days = int(g.get("event_retention_days", 30))
        self.min_free_gb = float(g.get("min_free_gb", 3))
        self.record_audio = bool(g.get("record_audio", False))
        t = c.get("telegram", {})
        self.tg_token, self.tg_chat = t.get("bot_token", ""), str(t.get("chat_id", ""))
        self.tg_api = t.get("api_base", "https://api.telegram.org")
        l = c.get("llm", {})
        self.llm_url = l.get("base_url", "")
        self.llm_key = l.get("api_key", "") or _local_ai_key()
        self.report_time = l.get("daily_report_time", "")
        self.cameras = c.get("camera", [])
        if not self.cameras:
            sys.exit("config: no [[camera]] entries")

    def now(self):
        return dt.datetime.now(self.tz)

    def is_open(self, t=None):
        t = (t or self.now()).time()
        a, b = (dt.time.fromisoformat(x.strip()) for x in self.open_hours.split("-"))
        return a <= t < b if a <= b else (t >= a or t < b)   # handles overnight


def _local_ai_key():
    p = Path.home() / ".config/local-ai/env"
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith("API_KEY="):
                return line.split("=", 1)[1]
    return ""


# ------------------------------------------------------------------ state
class State:
    """Arm mode shared between cameras and the Telegram command handler."""
    def __init__(self, cfg):
        self.cfg, self.mode = cfg, "auto"      # auto | armed | disarmed
        self.cams = {}

    def armed(self):
        if self.mode == "auto":
            return not self.cfg.is_open()
        return self.mode == "armed"


class EventDB:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS events(
            id TEXT PRIMARY KEY, ts REAL, camera TEXT, kind TEXT,
            detail TEXT, dir TEXT)""")
        self.db.commit()

    def add(self, *row):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", row)
            self.db.commit()

    def since(self, ts):
        with self.lock:
            return self.db.execute(
                "SELECT ts,camera,kind,detail FROM events WHERE ts>=? ORDER BY ts",
                (ts,)).fetchall()


# ------------------------------------------------------------------ detector
class Detector:
    """YOLO (ultralytics export, output [1, 84, N]) restricted to class 0 = person."""
    def __init__(self, path, threads, size=320):
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.size = size
        self.lock = threading.Lock()   # one inference at a time: cores are the limit

    def persons(self, frame, conf):
        s = self.size
        r = min(s / frame.shape[1], s / frame.shape[0])
        nw, nh = round(frame.shape[1] * r), round(frame.shape[0] * r)
        px, py = (s - nw) // 2, (s - nh) // 2
        canvas = np.full((s, s, 3), 114, np.uint8)
        canvas[py:py + nh, px:px + nw] = cv2.resize(frame, (nw, nh))
        x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        with self.lock:
            out = self.sess.run(None, {self.inp: np.ascontiguousarray(x)})[0][0]
        score = out[4]
        keep = score > conf
        if not keep.any():
            return []
        cx, cy, w, h = out[0][keep], out[1][keep], out[2][keep], out[3][keep]
        sc = score[keep]
        x1, y1 = (cx - w / 2 - px) / r, (cy - h / 2 - py) / r
        boxes = np.stack([x1, y1, w / r, h / r], 1)
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), sc.tolist(), conf, 0.45)
        return [(*boxes[i], float(sc[i])) for i in np.array(idx).flatten()]


# ------------------------------------------------------------------ alerts
class Telegram:
    def __init__(self, cfg):
        self.cfg, self.q = cfg, queue.Queue(maxsize=100)
        self.on = bool(cfg.tg_token and cfg.tg_chat)
        if self.on:
            threading.Thread(target=self._worker, daemon=True).start()

    def send(self, text, jpeg=None):
        log.info("ALERT %s", text.replace("\n", " | "))
        if self.on:
            try:
                self.q.put_nowait((text, jpeg))
            except queue.Full:
                log.warning("telegram queue full, dropping alert")

    def _api(self, method, fields=None, files=None, timeout=30):
        url = f"{self.cfg.tg_api}/bot{self.cfg.tg_token}/{method}"
        if not files:
            data = urllib.parse.urlencode(fields or {}).encode()
            req = urllib.request.Request(url, data=data)
        else:
            b = uuid.uuid4().hex
            parts = []
            for k, v in (fields or {}).items():
                parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"'
                             f"\r\n\r\n{v}\r\n".encode())
            for k, (fname, blob) in files.items():
                parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; '
                             f'filename="{fname}"\r\nContent-Type: image/jpeg\r\n\r\n'
                             .encode() + blob + b"\r\n")
            parts.append(f"--{b}--\r\n".encode())
            req = urllib.request.Request(
                url, data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def _worker(self):
        while True:
            text, jpeg = self.q.get()
            for attempt in range(5):
                try:
                    if jpeg is not None:
                        self._api("sendPhoto", {"chat_id": self.cfg.tg_chat,
                                                "caption": text[:1000]},
                                  {"photo": ("alert.jpg", jpeg)})
                    else:
                        self._api("sendMessage", {"chat_id": self.cfg.tg_chat,
                                                  "text": text[:4000]})
                    break
                except Exception as e:
                    log.warning("telegram send failed (%s), retry %d", e, attempt)
                    time.sleep(2 ** attempt * 3)

    def poll_commands(self, handler):
        """Long-poll getUpdates; only the configured chat may issue commands."""
        offset = 0
        while True:
            try:
                r = self._api("getUpdates", {"offset": offset, "timeout": 50}, timeout=60)
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    if str(m.get("chat", {}).get("id")) == self.cfg.tg_chat and m.get("text"):
                        handler(m["text"].strip())
            except Exception as e:
                log.warning("telegram poll failed: %s", e)
                time.sleep(10)


# ------------------------------------------------------------------ camera
def _ffmpeg_input(url):
    return (["-rtsp_transport", "tcp", "-timeout", "10000000"]
            if url.startswith("rtsp") else ["-re"]) + ["-i", url]


class Camera:
    def __init__(self, spec, cfg, state, det, tg, db):
        self.name = spec["name"]
        self.detect_url = spec["detect_url"]
        self.record_url = spec.get("record_url", "")
        self.cfg, self.state, self.det, self.tg, self.db = cfg, state, det, tg, db
        self.zones = []
        for z in spec.get("zones", []):
            poly = (np.array(z["polygon"], np.float32) * [W, H]).astype(np.int32)
            self.zones.append({"name": z["name"], "poly": poly,
                               "dwell": float(z.get("dwell_seconds", 8)),
                               "when": z.get("when", "always"),
                               "entered": None, "seen": 0.0, "fired": 0.0})
        self.buf = cfg.data / "buffer" / self.name
        self.buf.mkdir(parents=True, exist_ok=True)
        self.last_frame_t = time.time()
        self.last_frame = None
        self.down_alerted = False
        self.dark_since = None
        self.covered_alerted = False
        self.person_hits = 0
        self.last_person_t = 0.0
        self.intrusion_fired = 0.0
        self.last_detect = 0.0
        self.bg = None

    # ---- threads
    def start(self):
        threading.Thread(target=self._detect_loop, daemon=True).start()
        if self.record_url:
            threading.Thread(target=self._record_loop, daemon=True).start()

    def _record_loop(self):
        audio = ["-c:a", "copy"] if self.cfg.record_audio else ["-an"]
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *_ffmpeg_input(self.record_url),
               "-map", "0:v", *(["-map", "0:a?"] if self.cfg.record_audio else []),
               "-c:v", "copy", *audio, "-f", "segment",
               "-segment_time", str(SEG_SECONDS), "-reset_timestamps", "1",
               "-strftime", "1", str(self.buf / "%Y%m%d-%H%M%S.mkv")]
        while True:
            subprocess.run(cmd)
            log.warning("[%s] recorder exited, restarting in 5 s", self.name)
            time.sleep(5)

    def _detect_loop(self):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *_ffmpeg_input(self.detect_url),
               "-an", "-vf", f"fps={self.cfg.fps},scale={W}:{H}",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        size = W * H * 3
        while True:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            try:
                while True:
                    raw = p.stdout.read(size)
                    if len(raw) < size:
                        break
                    frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3)
                    self.last_frame_t, self.last_frame = time.time(), frame
                    if self.down_alerted:
                        self.down_alerted = False
                        self.tg.send(f"✅ {self.name}: camera back online")
                    try:
                        self._process(frame)
                    except Exception:
                        log.exception("[%s] frame processing failed", self.name)
            finally:
                p.kill()
                p.wait()
            log.warning("[%s] detect stream ended, reconnecting", self.name)
            time.sleep(3)

    # ---- per-frame logic
    def _process(self, frame):
        now = time.time()
        gray = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(frame, (160, 90)),
                                             cv2.COLOR_BGR2GRAY), (5, 5), 0)
        self._check_covered(gray, now, frame)

        g = gray.astype(np.float32)
        if self.bg is None:
            self.bg = g
        motion = float((cv2.absdiff(g, self.bg) > 25).mean())
        cv2.accumulateWeighted(g, self.bg, 0.05)
        if motion > 0.6:              # IR day/night switch or lights toggled
            self.bg = g               # re-baseline, and detect on the next frame:
            self.last_detect = 0.0    # lights switching on is itself suspicious
            return
        # A person standing still makes no motion; keep detecting for 15 s
        # after the last sighting so dwell timers don't reset.
        # A 30 s heartbeat detection catches anyone already standing still.
        if (motion < 0.002 and now - self.last_person_t > 15
                and now - self.last_detect < 30):
            self.person_hits = 0
            return

        self.last_detect = now
        people = self.det.persons(frame, self.cfg.conf)
        if people:
            self.last_person_t = now
            self.person_hits += 1
        else:
            self.person_hits = 0

        # after-hours intrusion: 2 consecutive positive frames, 60 s cooldown
        if self.state.armed() and self.person_hits >= 2 and now - self.intrusion_fired > 60:
            self.intrusion_fired = now
            best = max(p[4] for p in people)
            self._event("INTRUSION", f"{len(people)} person(s) while armed "
                        f"(conf {best:.2f})", frame, people)

        is_open = self.cfg.is_open()
        for z in self.zones:
            if (z["when"] == "open" and not is_open) or (z["when"] == "closed" and is_open):
                z["entered"] = None
                continue
            inside = any(cv2.pointPolygonTest(z["poly"], (float(x + w / 2), float(y + h)),
                                              False) >= 0 for x, y, w, h, _ in people)
            if inside:
                z["seen"] = now
                z["entered"] = z["entered"] or now
            elif z["entered"] and now - z["seen"] > 3:   # tolerate short misses
                z["entered"] = None
            if (z["entered"] and now - z["entered"] >= z["dwell"]
                    and now - z["fired"] > 120):
                z["fired"] = now
                self._event("ZONE", f"person in '{z['name']}' for "
                            f"{now - z['entered']:.0f}s", frame, people)

    def _check_covered(self, gray, now, frame):
        """Lens covered/sprayed or pointed at a wall: near-uniform image for 20 s."""
        if gray.std() < 6:
            self.dark_since = self.dark_since or now
            if now - self.dark_since > 20 and not self.covered_alerted:
                self.covered_alerted = True
                self._event("TAMPER", "image nearly uniform for 20 s "
                            "(lens covered or camera moved?)", frame, [])
        else:
            self.dark_since, self.covered_alerted = None, False

    def watchdog(self):
        if not self.down_alerted and time.time() - self.last_frame_t > 60:
            self.down_alerted = True
            self._event("CAMERA_DOWN", "no frames for 60 s (unplugged, Wi-Fi, "
                        "or stream credentials)", self.last_frame, [])

    # ---- evidence
    def annotate(self, frame, people):
        img = frame.copy()
        for z in self.zones:
            cv2.polylines(img, [z["poly"]], True, (0, 200, 255), 2)
            cv2.putText(img, z["name"], tuple(int(v) for v in z["poly"][0]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
        for x, y, w, h, c in people:
            cv2.rectangle(img, (int(x), int(y)), (int(x + w), int(y + h)), (0, 0, 255), 2)
            cv2.putText(img, f"{c:.2f}", (int(x), int(y) - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        return img

    def _event(self, kind, detail, frame, people):
        ts = self.cfg.now()
        eid = f"{ts:%Y%m%d-%H%M%S}-{self.name}-{kind.lower()}"
        edir = self.cfg.data / "events" / eid
        edir.mkdir(parents=True, exist_ok=True)
        jpeg = None
        if frame is not None:
            ok, buf = cv2.imencode(".jpg", self.annotate(frame, people),
                                   [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                jpeg = buf.tobytes()
                (edir / "snapshot.jpg").write_bytes(jpeg)
        self.db.add(eid, ts.timestamp(), self.name, kind, detail, str(edir))
        icon = {"INTRUSION": "🚨", "ZONE": "⚠️", "TAMPER": "🛑",
                "CAMERA_DOWN": "📵"}.get(kind, "•")
        self.tg.send(f"{icon} {kind} · {self.name} · {ts:%H:%M:%S}\n{detail}", jpeg)
        if self.record_url:
            Janitor.harvest_later(self, ts.timestamp(), edir)


# ------------------------------------------------------------------ storage
class Janitor:
    """Deletes old buffer segments, links event clips, enforces free space."""
    pending = []
    lock = threading.Lock()

    @classmethod
    def harvest_later(cls, cam, t, edir):
        with cls.lock:   # wait until the post-event segment has been closed
            cls.pending.append((t + SEG_SECONDS + 75, cam, t, edir))

    @staticmethod
    def _seg_start(p):
        try:
            return time.mktime(time.strptime(p.stem, "%Y%m%d-%H%M%S"))
        except ValueError:
            return None

    def __init__(self, cfg, cams):
        self.cfg, self.cams = cfg, cams

    def run(self):
        while True:
            try:
                self._harvest()
                self._prune_buffer()
                self._prune_events()
                for c in self.cams:
                    c.watchdog()
            except Exception:
                log.exception("janitor")
            time.sleep(15)

    def _harvest(self):
        now = time.time()
        with self.lock:
            due = [p for p in self.pending if p[0] <= now]
            self.pending[:] = [p for p in self.pending if p[0] > now]
        for _, cam, t, edir in due:
            n = 0
            for seg in sorted(cam.buf.glob("*.mkv")):
                s = self._seg_start(seg)
                if s is not None and t - 60 - SEG_SECONDS <= s <= t + 60:
                    try:
                        os.link(seg, edir / seg.name)
                        n += 1
                    except FileExistsError:
                        pass
            log.info("[%s] linked %d clip segment(s) into %s", cam.name, n, edir.name)

    def _prune_buffer(self):
        # Segment names are local time; time.mktime parses them in the process TZ.
        cutoff = time.time() - self.cfg.buffer_min * 60
        for cam in self.cams:
            for seg in cam.buf.glob("*.mkv"):
                s = self._seg_start(seg)
                if s is not None and s < cutoff:
                    seg.unlink(missing_ok=True)

    def _prune_events(self):
        root = self.cfg.data / "events"
        if not root.exists():
            return
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
        cutoff = time.time() - self.cfg.retention_days * 86400
        for d in dirs:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
        while dirs and shutil.disk_usage(root).free < self.cfg.min_free_gb * 1e9:
            shutil.rmtree(dirs.pop(0), ignore_errors=True)


# ------------------------------------------------------------------ report
def daily_report(cfg, db, tg):
    start = cfg.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.since(start.timestamp())
    counts = {}
    for _, cam, kind, _ in rows:
        counts[(cam, kind)] = counts.get((cam, kind), 0) + 1
    plain = "\n".join(f"{c} {k}: {n}" for (c, k), n in sorted(counts.items())) or "no events"
    text = f"📋 Daily report {start:%Y-%m-%d}\n{plain}"
    if cfg.llm_url and rows:
        lines = "\n".join(
            f"{dt.datetime.fromtimestamp(ts, cfg.tz):%H:%M} {cam} {kind}: {d}"
            for ts, cam, kind, d in rows[-150:])
        try:
            req = urllib.request.Request(
                cfg.llm_url.rstrip("/") + "/chat/completions",
                data=json.dumps({"messages": [
                    {"role": "system", "content":
                     "You summarise a shop's security event log for the owner. "
                     "Be factual and brief. Group repeated events, point out "
                     "unusual times or patterns, and list what to review first. "
                     "Only use facts from the log."},
                    {"role": "user", "content": lines}],
                    "temperature": 0.2, "max_tokens": 400}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {cfg.llm_key}"})
            with urllib.request.urlopen(req, timeout=600) as r:
                text += "\n\n" + json.load(r)["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning("LLM summary failed: %s", e)
    tg.send(text)


def scheduler(cfg, db, tg):
    last = None
    while True:
        now = cfg.now()
        if cfg.report_time and now.strftime("%H:%M") == cfg.report_time and last != now.date():
            last = now.date()
            daily_report(cfg, db, tg)
        time.sleep(20)


# ------------------------------------------------------------------ main
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Cfg(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    os.environ["TZ"] = str(cfg.tz)
    time.tzset()
    cfg.data.mkdir(parents=True, exist_ok=True)
    state, db = State(cfg), EventDB(cfg.data / "events.db")
    tg = Telegram(cfg)
    det = Detector(cfg.model, cfg.threads)
    cams = [Camera(s, cfg, state, det, tg, db) for s in cfg.cameras]
    for c in cams:
        state.cams[c.name] = c
        c.start()

    def on_command(text):
        cmd, *arg = text.split()
        cmd = cmd.lower().split("@")[0]
        if cmd in ("/arm", "/disarm", "/auto"):
            state.mode = {"/arm": "armed", "/disarm": "disarmed", "/auto": "auto"}[cmd]
            tg.send(f"mode = {state.mode} (armed now: {state.armed()})")
        elif cmd == "/status":
            lines = [f"mode={state.mode} armed={state.armed()} open={cfg.is_open()}"]
            for c in cams:
                age = time.time() - c.last_frame_t
                lines.append(f"{c.name}: {'OK' if age < 10 else f'no frame {age:.0f}s'}")
            free = shutil.disk_usage(cfg.data).free / 1e9
            lines.append(f"free disk {free:.1f} GB")
            tg.send("\n".join(lines))
        elif cmd == "/snap":
            for c in cams:
                if (not arg or c.name == arg[0]) and c.last_frame is not None:
                    ok, b = cv2.imencode(".jpg", c.annotate(c.last_frame, []))
                    tg.send(f"📷 {c.name}", b.tobytes() if ok else None)
        elif cmd == "/report":
            daily_report(cfg, db, tg)
        else:
            tg.send("/status /snap [cam] /arm /disarm /auto /report")

    if tg.on:
        threading.Thread(target=tg.poll_commands, args=(on_command,), daemon=True).start()
    threading.Thread(target=scheduler, args=(cfg, db, tg), daemon=True).start()
    tg.send(f"shopwatch started: {', '.join(c.name for c in cams)} · "
            f"hours {cfg.open_hours} · armed now: {state.armed()}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Janitor(cfg, cams).run()


if __name__ == "__main__":
    main()
