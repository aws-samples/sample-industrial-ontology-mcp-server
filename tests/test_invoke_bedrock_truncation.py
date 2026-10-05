"""_invoke_bedrock 의 max_tokens truncation 자동 확장 테스트."""
from __future__ import annotations

from unittest.mock import patch


def test_no_truncation_returns_text_as_is():
    calls = {"n": 0}

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        calls["n"] += 1
        return {"text": "OK", "stop_reason": "end_turn",
                "usage": {}}

    from tools import multi_agent_tbox as mat
    with patch.object(mat, "_strict_determinism", return_value=False), \
         patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        out = mat._invoke_bedrock("hi", max_tokens=100)
    assert out == "OK"
    assert calls["n"] == 1


def test_truncation_triggers_one_extended_retry():
    calls = []

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        calls.append(max_tokens)
        # 첫 호출은 잘림, 두 번째는 정상
        if len(calls) == 1:
            return {"text": "half response", "stop_reason": "max_tokens", "usage": {}}
        return {"text": "full response", "stop_reason": "end_turn", "usage": {}}

    from tools import multi_agent_tbox as mat
    with patch.object(mat, "_strict_determinism", return_value=False), \
         patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        out = mat._invoke_bedrock("hi", max_tokens=1000)
    assert out == "full response"
    assert len(calls) == 2
    # 1.5배 확장
    assert calls[1] == 1500


def test_truncation_cap_at_32000():
    calls = []

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        calls.append(max_tokens)
        # 첫 호출만 잘리도록
        if len(calls) == 1:
            return {"text": "trunc", "stop_reason": "max_tokens", "usage": {}}
        return {"text": "ok", "stop_reason": "end_turn", "usage": {}}

    from tools import multi_agent_tbox as mat
    with patch.object(mat, "_strict_determinism", return_value=False), \
         patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        mat._invoke_bedrock("hi", max_tokens=30000)
    # 30000 * 1.5 = 45000 → 32000 상한
    assert calls[1] == 32000


def test_second_truncation_does_not_recurse_infinitely():
    calls = []

    def _fake(prompt, max_tokens=8192, temperature=None, cached_prefix=None, **kwargs):
        calls.append(max_tokens)
        # 두 번 모두 잘림 — 재귀 방지 확인용
        return {"text": "still trunc", "stop_reason": "max_tokens", "usage": {}}

    from tools import multi_agent_tbox as mat
    with patch.object(mat, "_strict_determinism", return_value=False), \
         patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_fake):
        out = mat._invoke_bedrock("hi", max_tokens=1000)
    # 2회 만 호출 (auto_extend=False 재귀 차단)
    assert len(calls) == 2
    assert out == "still trunc"
