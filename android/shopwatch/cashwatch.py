"""Cash-counter analytics from body pose (YOLO11n-pose, 17 COCO keypoints).

Detecting a banknote in a hand is not feasible on a 640x360 CCTV substream.
Wrist trajectories are: every cash-handling act is a sequence of wrist visits
to a small set of places. This module turns those visits into rules:

  EXCHANGE  staff wrist in the counter-top exchange zone while a customer is present
  DRAWER    staff wrist in the drawer zone (or the reed-switch sensor reports open)
  POCKET    staff wrist at a trouser/hip pocket or a shirt (chest) pocket

  CASH_TO_POCKET    EXCHANGE -> POCKET before any DRAWER in that transaction   (high)
  DRAWER_TO_POCKET  DRAWER -> POCKET within N s, with no EXCHANGE in between     (high)
  NO_SALE_OPEN      DRAWER with no customer within +/-20 s                      (medium)
  DRAWER_LEFT_OPEN  sensor reports drawer open > 60 s                           (medium)
  HANDS_UP          staff holds both wrists above head >= 2 s (armed robbery)   (high)
  TXN / POCKET      each transaction and every pocket touch, logged for daily stats (low)

A single pocket touch is not proof: phones and handkerchiefs live in pockets too.
The high rules key on *sequence*, and daily per-counter pocket rates are compared
with the counter's own 7-day baseline, so the ones to review stand out.
"""
import threading
from collections import deque

import cv2
import numpy as np

NOSE, LSH, RSH, LWR, RWR, LHIP, RHIP = 0, 5, 6, 9, 10, 11, 12
SKELETON = [(5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12),
            (11, 13), (13, 15), (12, 14), (14, 16), (0, 5), (0, 6)]


# ------------------------------------------------------------------ model
class PoseDetector:
    """ultralytics pose export: output [1, 56, N] = box(4) score(1) 17x(x, y, conf)."""
    def __init__(self, path, threads, size=320):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.size = size
        self.lock = threading.Lock()

    def run(self, frame, conf, roi=None):
        """Returns [{'box': (x, y, w, h), 'score': s, 'kps': (17, 3)}] in frame pixels.
        roi = (x0, y0, x1, y1) normalised: crop first so the counter is seen at
        native resolution instead of being downscaled with the whole frame."""
        fh, fw = frame.shape[:2]
        ox = oy = 0
        if roi:
            ox, oy = int(roi[0] * fw), int(roi[1] * fh)
            frame = frame[oy:int(roi[3] * fh), ox:int(roi[2] * fw)]
        s = self.size
        r = min(s / frame.shape[1], s / frame.shape[0])
        nw, nh = round(frame.shape[1] * r), round(frame.shape[0] * r)
        px, py = (s - nw) // 2, (s - nh) // 2
        canvas = np.full((s, s, 3), 114, np.uint8)
        canvas[py:py + nh, px:px + nw] = cv2.resize(frame, (nw, nh))
        x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        with self.lock:
            out = self.sess.run(None, {self.inp: np.ascontiguousarray(x)})[0][0]
        keep = out[4] > conf
        if not keep.any():
            return []
        o = out[:, keep]
        boxes = np.stack([(o[0] - o[2] / 2 - px) / r + ox, (o[1] - o[3] / 2 - py) / r + oy,
                          o[2] / r, o[3] / r], 1)
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), o[4].tolist(), conf, 0.45)
        res = []
        for i in np.array(idx).flatten():
            k = o[5:, i].reshape(17, 3).copy()
            k[:, 0] = (k[:, 0] - px) / r + ox
            k[:, 1] = (k[:, 1] - py) / r + oy
            res.append({"box": tuple(boxes[i]), "score": float(o[4, i]), "kps": k})
        return res


def draw_pose(img, people, kp_conf=0.35):
    for p in people:
        k = p["kps"]
        for a, b in SKELETON:
            if k[a, 2] > kp_conf and k[b, 2] > kp_conf:
                cv2.line(img, (int(k[a, 0]), int(k[a, 1])), (int(k[b, 0]), int(k[b, 1])),
                         (255, 200, 0), 2)
        for j in (LWR, RWR):
            if k[j, 2] > kp_conf:
                cv2.circle(img, (int(k[j, 0]), int(k[j, 1])), 5, (0, 0, 255), -1)
    return img


# ------------------------------------------------------------------ geometry
def _pt(k, j, c):
    return k[j, :2] if k[j, 2] >= c else None


def _mid(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (a + b) / 2


def _inside(poly, p):
    return poly is not None and p is not None and \
        cv2.pointPolygonTest(poly, (float(p[0]), float(p[1])), False) >= 0


class Body:
    """Pose features the rules need, computed once per person per frame."""
    def __init__(self, person, kp_conf):
        k = person["kps"]
        self.box = person["box"]
        self.ls, self.rs = _pt(k, LSH, kp_conf), _pt(k, RSH, kp_conf)
        self.lh, self.rh = _pt(k, LHIP, kp_conf), _pt(k, RHIP, kp_conf)
        self.nose = _pt(k, NOSE, kp_conf)
        self.wrists = {"L": _pt(k, LWR, kp_conf), "R": _pt(k, RWR, kp_conf)}
        sh, hp = _mid(self.ls, self.rs), _mid(self.lh, self.rh)
        self.torso = float(np.linalg.norm(sh - hp)) if sh is not None and hp is not None else None
        x, y, w, h = self.box
        # Torso position decides staff vs customer: feet are usually hidden by the counter.
        self.anchor = hp if hp is not None else sh if sh is not None else np.array([x + w / 2, y + h / 2])
        self.hips_visible = self.lh is not None or self.rh is not None

    def pocket(self, w, hip_r=0.40, chest_r=0.22):
        """Wrist at a hip/trouser pocket or at the shirt chest pocket."""
        if w is None or not self.torso or self.torso < 12:
            return False
        L = self.torso
        for hip in (self.lh, self.rh):
            if hip is not None and np.linalg.norm(w - hip) < hip_r * L:
                return True
        for sh, hip in ((self.ls, self.lh), (self.rs, self.rh)):
            if sh is not None and hip is not None:
                chest = sh + 0.30 * (hip - sh)
                if np.linalg.norm(w - chest) < chest_r * L:
                    return True
        return False

    def hands_up(self):
        l, r = self.wrists["L"], self.wrists["R"]
        if l is None or r is None:
            return False
        top = self.nose if self.nose is not None else _mid(self.ls, self.rs)
        return top is not None and l[1] < top[1] and r[1] < top[1]


# ------------------------------------------------------------------ tracking
class Tracker:
    """Nearest-anchor tracker: enough to attribute events to one staff member."""
    def __init__(self, max_dist=90, ttl=3.0):
        self.tracks, self.next_id, self.max_dist, self.ttl = {}, 1, max_dist, ttl

    def update(self, t, bodies):
        ids, used = [], set()
        for b in bodies:
            best, bd = None, self.max_dist
            for tid, (pos, _) in self.tracks.items():
                d = float(np.linalg.norm(pos - b.anchor))
                if d < bd and tid not in used:
                    best, bd = tid, d
            if best is None:
                best, self.next_id = self.next_id, self.next_id + 1
            used.add(best)
            self.tracks[best] = (b.anchor, t)
            ids.append(best)
        self.tracks = {k: v for k, v in self.tracks.items() if t - v[1] < self.ttl}
        return ids


# ------------------------------------------------------------------ rules
class Debounce:
    """Rising edge after n consecutive true frames."""
    def __init__(self, n):
        self.n, self.run = n, 0

    def __call__(self, v):
        self.run = self.run + 1 if v else 0
        return self.run == self.n


class Counter:
    def __init__(self, spec, W, H, emit):
        """emit(kind, detail, severity, track_id) with severity high|medium|low."""
        def poly(name):
            p = spec.get(name)
            return None if p is None else (np.array(p, np.float32) * [W, H]).astype(np.int32)
        self.staff_z, self.cust_z = poly("staff_zone"), poly("customer_zone")
        self.drawer_z, self.exch_z = poly("drawer_zone"), poly("exchange_zone")
        self.roi = spec.get("roi")
        self.kp_conf = float(spec.get("keypoint_conf", 0.35))
        self.person_conf = float(spec.get("person_conf", 0.40))
        self.frames = int(spec.get("confirm_frames", 2))
        self.pocket_after_drawer = float(spec.get("pocket_after_drawer_s", 10))
        self.txn_timeout = float(spec.get("txn_idle_s", 20))
        self.use_sensor = bool(spec.get("drawer_sensor", False))
        self.emit = emit
        self.tracker = Tracker()
        self.deb = {}                      # (tid, what) -> Debounce
        self.handsup_since = {}
        self.txn = None
        self.last_customer = -1e9
        self.customer_hist = deque(maxlen=600)   # timestamps customer seen, ~1/s
        self.last_drawer = -1e9
        self.last_exchange = -1e9
        self.pending_nosale = []
        self.sensor_open_since = None
        self.sensor_left_open_fired = False
        self.fired = {}
        self.hips_seen = self.staff_frames = 0   # camera-placement diagnostic

    # -- helpers
    def _edge(self, tid, what, v):
        d = self.deb.setdefault((tid, what), Debounce(self.frames))
        return d(v)

    def _fire(self, t, kind, detail, sev, tid, cooldown=30):
        if t - self.fired.get(kind, -1e9) < cooldown:
            return
        self.fired[kind] = t
        self.emit(kind, detail, sev, tid)

    def _customer_near(self, t0, window=20):
        return any(abs(c - t0) <= window for c in self.customer_hist)

    def hip_visibility(self):
        return self.hips_seen / self.staff_frames if self.staff_frames else None

    # -- inputs
    def sensor(self, t, is_open):
        if is_open and self.sensor_open_since is None:
            self.sensor_open_since, self.sensor_left_open_fired = t, False
            self._drawer_edge(t, None)
        elif not is_open:
            self.sensor_open_since = None
            self.last_drawer = t

    def update(self, t, people):
        bodies = [Body(p, self.kp_conf) for p in people if p["score"] >= self.person_conf]
        ids = self.tracker.update(t, bodies)

        if any(_inside(self.cust_z, b.anchor) for b in bodies):
            self.last_customer = t
            if not self.customer_hist or t - self.customer_hist[-1] >= 1:
                self.customer_hist.append(t)
        customer_present = t - self.last_customer < 3

        for b, tid in zip(bodies, ids):
            if not _inside(self.staff_z, b.anchor):
                continue
            self.staff_frames += 1
            self.hips_seen += b.hips_visible
            wr = [w for w in b.wrists.values() if w is not None]

            in_drawer = any(_inside(self.drawer_z, w) for w in wr)
            if in_drawer:
                self.last_drawer = t
            if not self.use_sensor and self._edge(tid, "drawer", in_drawer):
                self._drawer_edge(t, tid)

            in_exch = customer_present and any(_inside(self.exch_z, w) for w in wr)
            if in_exch:
                self.last_exchange = t
            if self._edge(tid, "exchange", in_exch):
                if self.txn is None:
                    self.txn = {"start": t, "drawer": None, "pocket": 0, "tid": tid}
                self.txn["last"] = t

            if self._edge(tid, "pocket", any(b.pocket(w) for w in wr)):
                self._pocket_edge(t, tid)

            if b.hands_up():
                self.handsup_since.setdefault(tid, t)
                if t - self.handsup_since[tid] >= 2:
                    self._fire(t, "HANDS_UP", f"staff #{tid} hands above head "
                               f"{t - self.handsup_since[tid]:.0f}s"
                               + (" with customer present" if customer_present else ""),
                               "high", tid, cooldown=120)
            else:
                self.handsup_since.pop(tid, None)

        if self.txn and customer_present:
            self.txn["last"] = max(self.txn["last"], t)
        self._tick(t)

    def tick(self, t):
        """Call periodically even with no frames (sensor-only timers)."""
        self._tick(t)

    # -- rules
    def _drawer_edge(self, t, tid):
        self.last_drawer = t
        if self.txn is not None:
            self.txn["drawer"] = self.txn["drawer"] or t
        else:
            self.pending_nosale.append((t, tid))

    def _pocket_edge(self, t, tid):
        self.emit("POCKET", f"staff #{tid}", "low", tid)
        if self.txn is not None and self.txn["drawer"] is None:
            self.txn["pocket"] += 1
            self._fire(t, "CASH_TO_POCKET",
                       f"staff #{tid}: hand to pocket {t - self.txn['start']:.0f}s after "
                       "taking payment, before the cash drawer", "high", tid)
        elif (t - self.last_drawer <= self.pocket_after_drawer
              and self.last_drawer > self.last_exchange):
            self._fire(t, "DRAWER_TO_POCKET",
                       f"staff #{tid}: hand to pocket {t - self.last_drawer:.0f}s after "
                       "the drawer, with no hand-over to a customer in between",
                       "high", tid)

    def _tick(self, t):
        if self.txn and t - self.txn["last"] > self.txn_timeout:
            tx, self.txn = self.txn, None
            self.emit("TXN", f"drawer={'yes' if tx['drawer'] else 'no'} "
                      f"pocket={tx['pocket']} dur={tx['last'] - tx['start']:.0f}s",
                      "low", tx["tid"])
        keep = []
        for t0, tid in self.pending_nosale:
            if t - t0 < 20:
                keep.append((t0, tid))
            elif not self._customer_near(t0):
                self._fire(t, "NO_SALE_OPEN", "cash drawer opened with no customer "
                           f"within 20 s (at -{t - t0:.0f}s)", "medium", tid)
        self.pending_nosale = keep
        if (self.sensor_open_since is not None and not self.sensor_left_open_fired
                and t - self.sensor_open_since > 60):
            self.sensor_left_open_fired = True
            self._fire(t, "DRAWER_LEFT_OPEN", f"open {t - self.sensor_open_since:.0f}s",
                       "medium", None)
