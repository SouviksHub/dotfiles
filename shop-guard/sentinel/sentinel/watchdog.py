"""Tamper watchdog: camera offline / covered / moved, recorder heartbeat, DB backups.

A thief who knows about the cameras attacks the cameras first. Every check here turns
that attack into a Telegram alert within a few minutes, and the external heartbeat
(healthchecks.io) covers the one case this box cannot report itself: the box dying.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

SIZE = (160, 90)
COVERED_STD = 6.0      # grey-level std-dev below this = lens covered / blacked out / blinded
MOVED_CORR = 0.35      # edge-map correlation below this vs every reference = view changed
MAX_REFERENCES = 4     # e.g. day + night-IR views per camera


def to_gray(jpeg: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize(SIZE)
    return np.asarray(img, dtype=np.float32)


def edges(gray: np.ndarray) -> np.ndarray:
    """Gradient magnitude: scene structure, largely independent of lighting level."""
    gy, gx = np.gradient(gray)
    return np.hypot(gx, gy)


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom else 0.0


def is_covered(gray: np.ndarray) -> bool:
    return float(gray.std()) < COVERED_STD


def best_match(gray: np.ndarray, references: list[np.ndarray]) -> float:
    e = edges(gray)
    return max((correlation(e, edges(r)) for r in references), default=1.0)


@dataclass
class Condition:
    """Debounced alert state: fires after `needed` consecutive bad checks, once."""
    needed: int
    count: int = 0
    alerted: bool = False

    def update(self, bad: bool) -> str | None:
        if bad:
            self.count += 1
            if self.count >= self.needed and not self.alerted:
                self.alerted = True
                return "raised"
            return None
        self.count = 0
        if self.alerted:
            self.alerted = False
            return "cleared"
        return None


@dataclass
class CameraState:
    offline: Condition = field(default_factory=lambda: Condition(needed=2))
    covered: Condition = field(default_factory=lambda: Condition(needed=3))
    moved: Condition = field(default_factory=lambda: Condition(needed=3))


class Watchdog:
    def __init__(self, *, frigate, telegram, rules, db, ref_dir: Path, backup_dir: Path,
                 healthcheck_url: str = "", interval_s: float = 60.0):
        self.frigate = frigate
        self.telegram = telegram
        self.rules = rules
        self.db = db
        self.ref_dir = ref_dir
        self.backup_dir = backup_dir
        self.healthcheck_url = healthcheck_url.rstrip("/")
        self.interval_s = interval_s
        self.state: dict[str, CameraState] = {}
        self.frigate_down = Condition(needed=2)
        self.problems: list[str] = []
        self.ref_dir.mkdir(parents=True, exist_ok=True)

    # ---- references ---------------------------------------------------------

    def references(self, camera: str) -> list[np.ndarray]:
        return [to_gray(p.read_bytes()) for p in sorted(self.ref_dir.glob(f"{camera}-*.jpg"))]

    def add_reference(self, camera: str, jpeg: bytes | None = None) -> int:
        """Store the current view as a known-good view (call after aiming, and once at night)."""
        jpeg = jpeg or self.frigate.latest_frame(camera)
        if not jpeg:
            raise RuntimeError(f"no frame from {camera}")
        existing = sorted(self.ref_dir.glob(f"{camera}-*.jpg"))
        for old in existing[: max(0, len(existing) - MAX_REFERENCES + 1)]:
            old.unlink()
        (self.ref_dir / f"{camera}-{int(time.time())}.jpg").write_bytes(jpeg)
        return min(len(existing) + 1, MAX_REFERENCES)

    def reset_references(self, camera: str) -> None:
        for p in self.ref_dir.glob(f"{camera}-*.jpg"):
            p.unlink()

    # ---- checks -------------------------------------------------------------

    def _notify(self, camera: str, what: str, transition: str, jpeg: bytes | None = None) -> None:
        when = dt.datetime.now(self.rules.tz).strftime("%a %d %b %H:%M")
        if transition == "raised":
            self.telegram.photo(f"TAMPER ALERT: {camera} {what} ({when})", jpeg)
        else:
            self.telegram.message(f"Resolved: {camera} no longer {what} ({when})")

    def check_once(self) -> list[str]:
        problems: list[str] = []
        try:
            stats = self.frigate.stats()
        except Exception as exc:  # Frigate down or unreachable
            if self.frigate_down.update(True) == "raised":
                self.telegram.message(f"TAMPER ALERT: recorder (Frigate) not responding: {exc}")
            return ["frigate unreachable"]
        if self.frigate_down.update(False) == "cleared":
            self.telegram.message("Resolved: recorder (Frigate) responding again")

        for camera, cam_stats in (stats.get("cameras") or {}).items():
            st = self.state.setdefault(camera, CameraState())
            offline = float(cam_stats.get("camera_fps") or 0) < 0.5
            if t := st.offline.update(offline):
                self._notify(camera, "is OFFLINE (unplugged, no power or no network)", t)
            if offline:
                problems.append(f"{camera} offline")
                continue

            jpeg = self.frigate.latest_frame(camera)
            if not jpeg:
                continue
            gray = to_gray(jpeg)
            covered = is_covered(gray)
            if t := st.covered.update(covered):
                self._notify(camera, "is COVERED or blacked out", t, jpeg)
            if covered:
                problems.append(f"{camera} covered")
                continue

            refs = self.references(camera)
            if not refs:
                self.add_reference(camera, jpeg)
                continue
            moved = best_match(gray, refs) < MOVED_CORR
            if t := st.moved.update(moved):
                self._notify(camera, "has been MOVED or its view changed", t, jpeg)
            if moved:
                problems.append(f"{camera} moved")
        return problems

    def heartbeat(self, problems: list[str]) -> None:
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url + ("/fail" if problems else "")
        try:
            httpx.post(url, content="; ".join(problems) or "ok", timeout=10)
        except httpx.HTTPError as exc:
            log.warning("heartbeat failed: %s", exc)

    def backup_db(self) -> Path:
        """Daily consistent SQLite snapshot into the evidence tree (so it goes off-site)."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dst = self.backup_dir / f"sentinel-{dt.date.today():%Y-%m-%d}.db"
        self.db.backup(dst)
        return dst

    def run_forever(self) -> None:
        last_backup: dt.date | None = None
        while True:
            try:
                self.problems = self.check_once()
                self.heartbeat(self.problems)
                if last_backup != dt.date.today():
                    self.backup_db()
                    last_backup = dt.date.today()
            except Exception:
                log.exception("watchdog cycle failed")
            time.sleep(self.interval_s)

    def start(self) -> None:
        threading.Thread(target=self.run_forever, name="watchdog", daemon=True).start()
