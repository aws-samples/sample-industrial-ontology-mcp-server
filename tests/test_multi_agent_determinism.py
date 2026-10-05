"""MULTI_AGENT_DETERMINISM=strict 모드 테스트."""
from __future__ import annotations

from unittest.mock import patch


def _fake_metadata(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
    """invoke_bedrock_with_metadata 모의 응답. 호출 인자를 반환값에 끼워두려면 한 번 감싸야 함."""
    return {"text": "RESULT", "stop_reason": "end_turn", "usage": {}}


def test_strict_mode_forces_temperature_zero(monkeypatch):
    monkeypatch.setenv("MULTI_AGENT_DETERMINISM", "strict")
    captured: dict = {}

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        captured["temperature"] = temperature
        captured["max_tokens"] = max_tokens
        return {"text": "RESULT", "stop_reason": "end_turn", "usage": {}}

    from tools import multi_agent_tbox as mat
    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        out = mat._invoke_bedrock("hello", max_tokens=100, temperature=0.7)

    assert out == "RESULT"
    assert captured["temperature"] == 0.0


def test_default_mode_passes_temperature_through(monkeypatch):
    monkeypatch.delenv("MULTI_AGENT_DETERMINISM", raising=False)
    captured: dict = {}

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        captured["temperature"] = temperature
        return {"text": "RESULT", "stop_reason": "end_turn", "usage": {}}

    from tools import multi_agent_tbox as mat
    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        mat._invoke_bedrock("hello", max_tokens=100, temperature=0.7)

    assert captured["temperature"] == 0.7


def test_strict_mode_logs_prompt_hash(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("MULTI_AGENT_DETERMINISM", "strict")
    from tools import multi_agent_tbox as mat
    caplog.set_level(logging.DEBUG, logger="tools.multi_agent_tbox")

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        return {"text": "OK", "stop_reason": "end_turn", "usage": {}}

    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        mat._invoke_bedrock("deterministic prompt", temperature=0.5)

    assert any("bedrock[strict]" in r.message for r in caplog.records)


def test_strict_detection_case_insensitive(monkeypatch):
    monkeypatch.setenv("MULTI_AGENT_DETERMINISM", "STRICT")
    from tools.multi_agent_tbox import _strict_determinism
    assert _strict_determinism() is True
    monkeypatch.setenv("MULTI_AGENT_DETERMINISM", "exploration")
    assert _strict_determinism() is False
