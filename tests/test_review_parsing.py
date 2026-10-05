"""_extract_json_candidate / _parse_review_json 단위 테스트."""
from __future__ import annotations

import json

from tools.multi_agent_tbox import _extract_json_candidate, _parse_review_json

# ── _extract_json_candidate ────────────────────────────

def test_extract_pure_json_object():
    assert _extract_json_candidate('{"a":1}') == '{"a":1}'


def test_extract_pure_json_array():
    assert _extract_json_candidate('[1,2,3]') == '[1,2,3]'


def test_extract_from_json_code_block():
    wrapped = '```json\n{"ok":true}\n```'
    assert _extract_json_candidate(wrapped) == '{"ok":true}'


def test_extract_from_plain_code_block():
    wrapped = "```\n[1, 2]\n```"
    assert _extract_json_candidate(wrapped) == '[1, 2]'


def test_extract_from_mixed_text_falls_back_to_first_brace_block():
    text = '설명 문장. 출력:\n{"x":1}\n끝.'
    assert _extract_json_candidate(text) == '{"x":1}'


def test_extract_prefers_earlier_object_over_later_array():
    text = 'prose {"a":1} and also [2]'
    # 객체가 먼저 시작하므로 객체 우선 반환.
    out = _extract_json_candidate(text)
    assert json.loads(out) == {"a": 1}


def test_extract_empty_code_block_falls_through():
    # 빈 코드블록이면 원문 전체 반환 경로 (호출자가 JSON 아님을 감지)
    text = "```\n\n```\n{\"k\":2}"
    out = _extract_json_candidate(text)
    assert '"k":2' in out


# ── _parse_review_json ─────────────────────────────────

def test_parse_valid_json_returns_dict():
    out = _parse_review_json('{"issues":[],"approved":true,"summary":"ok"}', "Test")
    assert out["approved"] is True


def test_parse_failure_without_retry_returns_error_marker():
    out = _parse_review_json("not json at all", "Test")
    assert out["parse_error"] is True
    assert out["issues"] == []
    assert out["approved"] is False


def test_parse_failure_with_retry_recovers():
    call_count = {"n": 0}

    def retry_fn(prompt: str) -> str:
        call_count["n"] += 1
        return '{"issues":[{"severity":"high"}],"approved":false,"summary":"retried"}'

    out = _parse_review_json(
        "not json",
        "Test",
        retry_invoker=retry_fn,
        retry_prompt="JSON 만 출력",
    )
    assert call_count["n"] == 1
    assert out["summary"] == "retried"
    assert out["issues"][0]["severity"] == "high"


def test_parse_failure_retry_also_fails_returns_marker():
    def bad_retry(prompt: str) -> str:
        return "still not json"

    out = _parse_review_json(
        "not json",
        "Test",
        retry_invoker=bad_retry,
        retry_prompt="JSON 만 출력",
    )
    assert out["parse_error"] is True


def test_parse_failure_retry_raises_is_swallowed():
    def raising_retry(prompt: str) -> str:
        raise RuntimeError("network down")

    out = _parse_review_json(
        "not json",
        "Test",
        retry_invoker=raising_retry,
        retry_prompt="JSON 만 출력",
    )
    assert out["parse_error"] is True


def test_parse_handles_code_block_with_trailing_prose():
    # 현장 LLM 응답 흔한 패턴: 코드블록 뒤에 해설
    raw = '```json\n{"issues":[],"approved":true,"summary":"ok"}\n```\n설명: 이상 없음.'
    out = _parse_review_json(raw, "Test")
    assert out["approved"] is True
