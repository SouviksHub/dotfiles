"""Wires the POS to the printer, the drawer sensor (MQTT) and Sentinel (MQTT alerts).

Topics
  shopguard/drawer/state      <- ESP32: "open" | "closed" (retained)
  shopguard/drawer/heartbeat  <- ESP32: every 15 s
  shopguard/pos/sale          -> every completed sale
  shopguard/pos/alert         -> drawer / void / cash-short / stock-loss alerts;
                                 Sentinel reviews the till camera around `ts`
"""

from __future__ import annotations

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from .drawer import Alert, DrawerMonitor
from .printer import Printer

log = logging.getLogger(__name__)

STATE, HEARTBEAT = "shopguard/drawer/state", "shopguard/drawer/heartbeat"
SALE_TOPIC, ALERT_TOPIC = "shopguard/pos/sale", "shopguard/pos/alert"


class Bridge:
    def __init__(self, printer: Printer, monitor: DrawerMonitor | None = None):
        self.printer = printer
        self.monitor = monitor or DrawerMonitor()
        self.pos = None
        self.client: mqtt.Client | None = None
        self._lock = threading.Lock()

    # POS -> world ------------------------------------------------------------

    def on_pos_event(self, kind: str, payload: dict) -> None:
        if kind == "drawer_kick":
            now = time.time()
            with self._lock:
                self.monitor.kick(now, payload.get("sale_id"), payload.get("reason", ""), payload.get("user", ""))
            if self.pos:
                self.pos.record_drawer_event(now, "kick", payload.get("sale_id"), payload.get("reason", ""))
            if payload.get("reason") != "sale":   # sales kick together with the receipt
                self.printer.kick()
        elif kind == "sale":
            self._publish(SALE_TOPIC, payload)
        elif kind == "alert":
            self._publish(ALERT_TOPIC, payload)

    def _publish(self, topic: str, payload: dict) -> None:
        if self.client:
            self.client.publish(topic, json.dumps(payload), qos=1)

    def _emit(self, alerts: list[Alert]) -> None:
        for a in alerts:
            log.warning("drawer alert: %s %s", a.kind, a.detail)
            if self.pos:
                self.pos.record_drawer_event(a.ts, "alert", a.sale_id, f"{a.kind}: {a.detail}")
            self._publish(ALERT_TOPIC, {"kind": a.kind, "ts": a.ts, "detail": a.detail, "sale_id": a.sale_id})

    # sensor -> POS -------------------------------------------------------------

    def on_message(self, _c, _u, msg) -> None:
        now = time.time()
        with self._lock:
            if msg.topic == STATE:
                state = msg.payload.decode().strip().lower()
                alerts = self.monitor.sensor(now, state)
                if self.pos:
                    self.pos.record_drawer_event(now, state)
            elif msg.topic == HEARTBEAT:
                alerts = self.monitor.heartbeat(now)
            else:
                return
        self._emit(alerts)

    def _ticker(self) -> None:
        while True:
            with self._lock:
                alerts = self.monitor.tick(time.time())
            self._emit(alerts)
            time.sleep(2)

    def start(self, host: str, port: int) -> None:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pos")
        c.on_connect = lambda cl, *_: (cl.subscribe(STATE, qos=1), cl.subscribe(HEARTBEAT))
        c.on_message = self.on_message
        c.reconnect_delay_set(1, 30)
        c.connect_async(host, port)
        c.loop_start()
        self.client = c
        threading.Thread(target=self._ticker, name="drawer-ticker", daemon=True).start()
