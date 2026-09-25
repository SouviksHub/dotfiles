import json
from types import SimpleNamespace

import pytest

from sentinel.analyzer import Analyzer, AnalysisError


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(text, stop_reason="end_turn"):
    resp = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    msgs = FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


GOOD = json.dumps({
    "summary": "Person opens till with no customer and pockets notes.",
    "actions": [{"t": 3.5, "description": "opens till"}, {"t": 6.0, "description": "hand to pocket"}],
    "indicators": ["till_opened_without_customer", "cash_taken_or_pocketed"],
    "suspicion_score": 14, "confidence": "medium",
    "innocent_explanations": ["making change for a float"],
    "what_to_check_in_full_clip": "Seconds 3-7: does the note go into the pocket?",
})


def test_review_builds_request_and_parses():
    client, msgs = fake_client(GOOD)
    a = Analyzer("claude-opus-5", "medium", client=client).review(
        [(1.0, b"\xff\xd8jpeg"), (2.0, b"\xff\xd8jpeg")], {"camera": "front"})
    assert a.suspicion_score == 10  # clamped
    call = msgs.calls[0]
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    images = [c for c in call["messages"][0]["content"] if c["type"] == "image"]
    assert len(images) == 2


def test_no_fallback_param_on_other_models():
    client, msgs = fake_client(GOOD)
    Analyzer("claude-sonnet-5", "low", client=client).review([(1.0, b"x")], {})
    assert "fallbacks" not in msgs.calls[0]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stop_reasons_raise(stop):
    client, _ = fake_client(GOOD, stop_reason=stop)
    with pytest.raises(AnalysisError):
        Analyzer("claude-opus-5", "medium", client=client).review([(1.0, b"x")], {})
