"""Claude features for the POS: catalogue entry from a box photo, and loss analysis."""

from __future__ import annotations

import base64
import json

import anthropic

FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "brand name + strength as sold, e.g. 'Napa 500mg'"},
        "generic": {"type": "string"},
        "strength": {"type": "string"},
        "form": {"type": "string", "enum": ["tablet", "capsule", "syrup", "suspension", "injection",
                                            "cream", "ointment", "drops", "inhaler", "sachet", "other"]},
        "manufacturer": {"type": "string"},
        "pack": {"type": "string", "description": "pack size text, e.g. '10 x 10 tablets'"},
        "mrp_taka": {"type": ["number", "null"], "description": "printed MRP in taka, null if not visible"},
        "barcode": {"type": ["string", "null"], "description": "digits of a visible barcode, else null"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["name", "generic", "strength", "form", "manufacturer", "pack", "mrp_taka", "barcode", "confidence"],
    "additionalProperties": False,
}


class AIError(RuntimeError):
    pass


class PosAI:
    def __init__(self, model: str = "claude-opus-5", client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    def _create(self, *, system: str, content, effort: str, fmt: dict | None = None) -> str:
        output_config: dict = {"effort": effort}
        if fmt:
            output_config["format"] = fmt
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if self.model in FALLBACK_MODELS else {}
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, thinking={"type": "adaptive"},
                output_config=output_config, system=system,
                messages=[{"role": "user", "content": content}], **extra)
        except anthropic.APIStatusError as exc:
            raise AIError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError(f"cannot reach Claude API: {exc}") from exc
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise AIError(f"no usable answer (stop_reason={resp.stop_reason})")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text:
            raise AIError("empty response")
        return text

    def product_from_photo(self, image: bytes, media_type: str = "image/jpeg") -> dict:
        system = ("You read medicine and retail packaging photographed in a Bangladeshi pharmacy and "
                  "extract catalogue fields. Copy text exactly as printed (Bengali or English); "
                  "use null when a field is not visible rather than guessing.")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": "Extract the product fields from this package."},
        ]
        return json.loads(self._create(system=system, content=content, effort="low",
                                       fmt={"type": "json_schema", "schema": PRODUCT_SCHEMA}))

    def loss_analysis(self, report: dict, period: str, sightings: list | None = None) -> str:
        system = (
            "You are a retail loss-prevention analyst for a small Bangladeshi pharmacy whose owner "
            "lives abroad. Amounts are integer paisa (100 paisa = 1 taka); present them in taka. "
            "From the POS data, identify where cash or stock is leaking and which staff member each "
            "signal points to: shift cash variances, voids (especially repeated, late, or high-value), "
            "payouts, drawer alerts (unauthorised opens, drawer left open, sensor offline, kick "
            "without open), and stock-count losses. Distinguish patterns from one-offs, rank "
            "concerns by likely money lost, and for each give the specific footage or records the "
            "owner should check next. Say plainly when the data is too thin to conclude anything. "
            "Plain text, under 400 words.")
        payload = {"period": period, "pos": report, "face_recognition_sightings": sightings or []}
        return self._create(system=system, content=json.dumps(payload, default=str), effort="high")
