"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    frigate_url: str
    mqtt_host: str
    mqtt_port: int
    rules_path: Path
    evidence_dir: Path
    db_path: Path
    claude_model: str
    claude_effort: str
    frames_per_event: int
    telegram_token: str
    telegram_chat_id: str
    dashboard_user: str
    dashboard_password: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            frigate_url=_env("FRIGATE_URL", "http://frigate:5000"),
            mqtt_host=_env("MQTT_HOST", "mqtt"),
            mqtt_port=int(_env("MQTT_PORT", "1883")),
            rules_path=Path(_env("RULES_PATH", "rules.yaml")),
            evidence_dir=Path(_env("EVIDENCE_DIR", "evidence")),
            db_path=Path(_env("DB_PATH", "sentinel.db")),
            claude_model=_env("CLAUDE_MODEL", "claude-opus-5"),
            claude_effort=_env("CLAUDE_EFFORT", "medium"),
            frames_per_event=int(_env("FRAMES_PER_EVENT", "10")),
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            dashboard_user=_env("DASHBOARD_USER", "owner"),
            dashboard_password=_env("DASHBOARD_PASSWORD"),
        )
