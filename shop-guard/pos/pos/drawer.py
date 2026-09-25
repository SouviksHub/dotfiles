"""Cash-drawer reconciliation: every physical opening must be explained by a POS kick.

Inputs are the POS's own kicks and the ESP32 reed-switch reports. Pure state machine,
no I/O, so every tamper rule is unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Alert:
    kind: str       # unauthorized_open | open_too_long | sensor_offline | kick_without_open
    ts: float
    detail: str
    sale_id: int | None = None


@dataclass
class Kick:
    ts: float
    sale_id: int | None
    reason: str
    user: str


class DrawerMonitor:
    def __init__(self, *, kick_window_s: float = 6, max_open_s: float = 60,
                 sensor_timeout_s: float = 90, expect_open_s: float = 10):
        self.kick_window_s = kick_window_s
        self.max_open_s = max_open_s
        self.sensor_timeout_s = sensor_timeout_s
        self.expect_open_s = expect_open_s
        self.state: str | None = None
        self.opened_at: float | None = None
        self.open_kick: Kick | None = None
        self.last_kick: Kick | None = None
        self.last_seen: float | None = None
        self._open_alerted = False
        self._offline_alerted = False

    def kick(self, ts: float, sale_id: int | None, reason: str, user: str) -> list[Alert]:
        self.last_kick = Kick(ts, sale_id, reason, user)
        return []

    def heartbeat(self, ts: float) -> list[Alert]:
        self.last_seen = ts
        self._offline_alerted = False
        return []

    def sensor(self, ts: float, state: str) -> list[Alert]:
        self.heartbeat(ts)
        alerts: list[Alert] = []
        if state == "open" and self.state != "open":
            self.state, self.opened_at, self._open_alerted = "open", ts, False
            k = self.last_kick
            if k and 0 <= ts - k.ts <= self.kick_window_s:
                self.open_kick, self.last_kick = k, None      # authorised; a kick opens the drawer once
            else:
                self.open_kick = None
                alerts.append(Alert("unauthorized_open", ts,
                                    "drawer opened without a POS sale (key, force, or cut sensor wire)"))
        elif state == "closed":
            self.state, self.opened_at, self.open_kick = "closed", None, None
        return alerts

    def tick(self, now: float) -> list[Alert]:
        alerts: list[Alert] = []
        if self.state == "open" and self.opened_at and not self._open_alerted \
                and now - self.opened_at > self.max_open_s:
            self._open_alerted = True
            sale = self.open_kick.sale_id if self.open_kick else None
            alerts.append(Alert("open_too_long", now,
                                f"drawer open for {int(now - self.opened_at)}s", sale))
        if self.last_seen is not None and not self._offline_alerted \
                and now - self.last_seen > self.sensor_timeout_s:
            self._offline_alerted = True
            alerts.append(Alert("sensor_offline", now,
                                f"drawer sensor silent for {int(now - self.last_seen)}s (unplugged or tampered)"))
        k = self.last_kick
        if k and now - k.ts > self.expect_open_s:
            self.last_kick = None
            alerts.append(Alert("kick_without_open", now,
                                f"POS opened the drawer ({k.reason} by {k.user}) but the sensor saw no opening",
                                k.sale_id))
        return alerts
