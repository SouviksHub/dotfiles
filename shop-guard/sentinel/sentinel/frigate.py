"""Thin client for Frigate's internal API (port 5000, inside the compose network)."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class Frigate:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/api", timeout=timeout)

    def event(self, event_id: str) -> dict:
        r = self.http.get(f"/events/{event_id}")
        r.raise_for_status()
        return r.json()

    def clip(self, event_id: str, attempts: int = 4, wait_s: float = 5.0) -> bytes:
        """The clip is assembled from recording segments shortly after the event
        ends, so the first request can 404 or return an empty body."""
        last: Exception | None = None
        for _ in range(attempts):
            try:
                r = self.http.get(f"/events/{event_id}/clip.mp4")
                if r.status_code == 200 and len(r.content) > 10_000:
                    return r.content
                last = RuntimeError(f"clip not ready (HTTP {r.status_code}, {len(r.content)} bytes)")
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(wait_s)
        raise RuntimeError(f"could not fetch clip for {event_id}: {last}")

    def snapshot(self, event_id: str) -> bytes | None:
        r = self.http.get(f"/events/{event_id}/snapshot.jpg", params={"bbox": 1, "quality": 90})
        return r.content if r.status_code == 200 else None

    def stats(self) -> dict:
        r = self.http.get("/stats", timeout=15)
        r.raise_for_status()
        return r.json()

    def latest_frame(self, camera: str) -> bytes | None:
        r = self.http.get(f"/{camera}/latest.jpg", params={"quality": 85})
        return r.content if r.status_code == 200 else None

    def retain(self, event_id: str) -> None:
        """Exempt the event's recording from Frigate's retention cleanup."""
        try:
            self.http.post(f"/events/{event_id}/retain").raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("retain %s failed: %s", event_id, exc)
