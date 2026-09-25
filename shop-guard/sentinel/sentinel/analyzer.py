"""Claude-powered review of sampled CCTV frames, plus Q&A and daily reports."""

from __future__ import annotations

import base64
import json
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, field_validator

log = logging.getLogger(__name__)

# Server-side refusal fallback: supported on these models only.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

INDICATORS = [
    "cash_taken_or_pocketed",
    "item_concealed_on_body_or_bag",
    "till_opened_without_customer",
    "goods_handed_over_without_payment",
    "item_removed_from_restricted_area",
    "stock_carried_out_of_view",
    "camera_obstructed_or_repositioned",
    "after_hours_presence",
    "looking_around_before_acting",
    "none",
]

REVIEW_SYSTEM = """\
You are a loss-prevention analyst reviewing CCTV stills from a small pharmacy/office.
The frames are sampled in time order from one tracked-person event. Your output goes \
to the business owner, who will watch the actual footage before acting on anything.

Report only what is visible. For each notable action give the frame timestamp. Do not \
guess identity: refer to people by appearance ("person in blue polo"), except when the \
event context supplies a face-recognition tag, which you may repeat as "tagged as <name>".

Theft patterns worth flagging in retail: cash moved from the till/counter to a pocket, \
bag or clothing; the till opened with no customer present; items slipped into pockets, \
sleeves or bags; goods handed to someone who does not pay; items taken from restricted \
stock (e.g. controlled-drug cabinet, dispensary shelves) without a visible script/workflow; stock carried \
toward exits, toilets or out of camera view; covering, turning or blocking the camera; \
checking whether anyone is watching immediately before one of these acts.

Also list plausible innocent explanations for what you saw (restocking, giving change, \
putting away a personal phone). A still-frame sample can miss the decisive moment, so \
say so when the key action falls between frames.

suspicion_score rubric (integer 0-10):
0-2 routine work, nothing unusual.
3-5 ambiguous; something worth a quick look at the full clip.
6-7 a specific concerning action is visible.
8-10 a theft action is clearly visible in one or more frames.
Use "none" as the only indicator when nothing applies."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentence plain-English account"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "number"}, "description": {"type": "string"}},
                "required": ["t", "description"],
                "additionalProperties": False,
            },
        },
        "indicators": {"type": "array", "items": {"type": "string", "enum": INDICATORS}},
        "suspicion_score": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "innocent_explanations": {"type": "array", "items": {"type": "string"}},
        "what_to_check_in_full_clip": {"type": "string"},
    },
    "required": ["summary", "actions", "indicators", "suspicion_score", "confidence",
                 "innocent_explanations", "what_to_check_in_full_clip"],
    "additionalProperties": False,
}


class Action(BaseModel):
    t: float
    description: str


class Assessment(BaseModel):
    summary: str
    actions: list[Action]
    indicators: list[str]
    suspicion_score: int
    confidence: Literal["low", "medium", "high"]
    innocent_explanations: list[str]
    what_to_check_in_full_clip: str

    @field_validator("suspicion_score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(10, v))


class AnalysisError(RuntimeError):
    pass


class Analyzer:
    def __init__(self, model: str, effort: str, client: anthropic.Anthropic | None = None):
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def _create(self, *, system: str, content: list | str, effort: str, fmt: dict | None = None):
        output_config: dict = {"effort": effort}
        if fmt:
            output_config["format"] = fmt
        kwargs: dict = {}
        if self.model in FALLBACK_MODELS:
            kwargs = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config=output_config,
                system=system,
                messages=[{"role": "user", "content": content}],
                **kwargs,
            )
        except anthropic.RateLimitError as exc:
            raise AnalysisError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AnalysisError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalysisError(f"cannot reach Claude API: {exc}") from exc
        if resp.stop_reason == "refusal":
            raise AnalysisError("model declined to analyse this clip")
        if resp.stop_reason == "max_tokens":
            raise AnalysisError("response truncated at max_tokens")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text:
            raise AnalysisError(f"empty response (stop_reason={resp.stop_reason})")
        return text

    def review(self, frames: list[tuple[float, bytes]], context: dict) -> Assessment:
        content: list[dict] = [{"type": "text", "text": "Event context:\n" + json.dumps(context, indent=2)}]
        for t, jpeg in frames:
            content.append({"type": "text", "text": f"Frame at t={t}s"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.standard_b64encode(jpeg).decode()},
            })
        content.append({"type": "text", "text": "Assess this event."})
        text = self._create(system=REVIEW_SYSTEM, content=content, effort=self.effort,
                            fmt={"type": "json_schema", "schema": REVIEW_SCHEMA})
        return Assessment.model_validate_json(text)

    def answer(self, question: str, incidents: list[dict], tz_name: str) -> str:
        system = (
            "You answer the business owner's questions about CCTV incidents that an AI "
            "reviewer has already summarised. Use only the incident log provided. Cite "
            "incident ids in square brackets, e.g. [1727312345.1-abc]. If the log cannot "
            "answer the question, say what footage the owner should look at instead. "
            f"All times are {tz_name} local time."
        )
        content = f"Incident log (JSON lines):\n{_log_lines(incidents)}\n\nQuestion: {question}"
        return self._create(system=system, content=content, effort="medium")

    def daily_report(self, incidents: list[dict], day_label: str, tz_name: str) -> str:
        system = (
            "Write a short daily CCTV report for a small-business owner, as plain text for "
            "Telegram (no markdown tables). Lead with anything scoring 6 or more, then "
            "patterns (same person, same zone, same time of day), then a one-line count of "
            "routine activity. Cite incident ids. Keep it under 250 words. "
            f"Times are {tz_name} local time."
        )
        content = f"Incidents for {day_label} (JSON lines):\n{_log_lines(incidents)}"
        return self._create(system=system, content=content, effort="low")


def _log_lines(incidents: list[dict]) -> str:
    rows = []
    for inc in incidents:
        a = inc.get("assessment") or {}
        rows.append(json.dumps({
            "id": inc["event_id"], "camera": inc["camera"], "local_time": inc.get("local_time"),
            "person": inc.get("person"), "zones": inc.get("zones"), "reasons": inc.get("reasons"),
            "score": inc.get("score"), "summary": inc.get("summary"),
            "indicators": a.get("indicators"),
        }))
    return "\n".join(rows) or "(no incidents)"
