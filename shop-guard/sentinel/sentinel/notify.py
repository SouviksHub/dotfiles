"""Telegram alerts. No-ops (logged) when the bot is not configured."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}" if token and chat_id else ""

    def _post(self, method: str, **kwargs) -> None:
        if not self.base:
            log.info("telegram not configured; skipped %s", method)
            return
        try:
            httpx.post(f"{self.base}/{method}", timeout=30, **kwargs).raise_for_status()
        except httpx.HTTPError as exc:
            log.error("telegram %s failed: %s", method, exc)

    def photo(self, caption: str, jpeg: bytes | None) -> None:
        if not jpeg:
            return self.message(caption)
        self._post(
            "sendPhoto",
            data={"chat_id": self.chat_id, "caption": caption[:CAPTION_LIMIT]},
            files={"photo": ("snapshot.jpg", jpeg, "image/jpeg")},
        )

    def message(self, text: str) -> None:
        for i in range(0, len(text), MESSAGE_LIMIT):
            self._post("sendMessage", data={"chat_id": self.chat_id, "text": text[i:i + MESSAGE_LIMIT]})
