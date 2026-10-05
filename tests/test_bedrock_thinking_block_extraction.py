"""Regression: adaptive-thinking 모델의 응답에서 text 를 잃지 않는가.

배경 (2026-08-18 실측): ``invoke_bedrock_with_metadata`` 는 응답을
``resp_body["content"][0]["text"]`` 로 읽어 **첫 블록이 텍스트라고 가정** 했다.
adaptive thinking 이 기본 ON 인 모델(``us.anthropic.claude-opus-5`` /
``claude-sonnet-5``)은 첫 블록이 ``{"type": "thinking"}`` 이라 이 가정이
``KeyError: 'text'`` 로 깨진다. 실측: ``us.anthropic.claude-opus-5`` 는
blocks=``['thinking', 'text']`` 을 반환하고 Jury 호출이 즉사했다.

이 파일이 주장하는 것은 카운터가 아니라 **산출물** 이다: 정당한 응답에서 본문을
그대로 얻는가(보존), 그리고 본문이 없을 때 조용한 빈 문자열로 넘기지 않는가.
호출부는 반환값을 JSON/TTL 로 파싱하므로 빈 문자열은 "모델이 침묵했다" 와
"우리가 파싱을 못 했다" 를 구분 불가하게 만든다.
"""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from tools.bedrock import _extract_text, invoke_bedrock_with_metadata


def _resp(body: dict) -> dict:
    """boto3 invoke_model 반환 형태로 감싼다."""
    return {"body": BytesIO(json.dumps(body).encode("utf-8"))}


# ── 보존: 정당한 응답에서 본문을 잃지 않는가 ─────────────────────────────

def test_thinking_block_first_still_yields_text():
    """opus-5 / sonnet-5 실측 형태 — thinking 이 앞서도 text 를 얻는다."""
    body = {
        "content": [
            {"type": "thinking", "thinking": "내부 추론"},
            {"type": "text", "text": '{"production_ready": true}'},
        ],
        "stop_reason": "end_turn",
    }
    assert _extract_text(body, "us.anthropic.claude-opus-5") == '{"production_ready": true}'


def test_legacy_single_text_block_unchanged():
    """기존 모델(sonnet-4-6 등) 응답 형태의 동작이 바뀌지 않는다."""
    body = {"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn"}
    assert _extract_text(body, "us.anthropic.claude-sonnet-4-6") == "hello"


def test_block_without_type_treated_as_text():
    """``type`` 키가 없는 블록은 text 로 간주한다.

    예전 코드는 ``content[0]["text"]`` 로 type 을 아예 보지 않았다. type 을
    **요구** 하도록 좁히면 그 관용에 의존하던 호출/픽스처가 깨진다 (실측: 그
    엄격한 형태가 기존 테스트 2건을 깼다). 관용을 명문화해 다음 사람이 "엄격하게
    고치는" 회귀를 막는다.
    """
    body = {"content": [{"text": "hello"}], "stop_reason": "end_turn"}
    assert _extract_text(body, "m") == "hello"


def test_multiple_text_blocks_are_concatenated_not_truncated():
    """text 블록이 쪼개지면 **전부** 이어붙인다 — 첫 조각만 취하면 조용한 절단이다."""
    body = {
        "content": [
            {"type": "thinking", "thinking": "x"},
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ],
        "stop_reason": "end_turn",
    }
    assert _extract_text(body, "m") == "part1part2"


# ── NEGATIVE: 본문이 없을 때 조용히 넘기지 않는가 ────────────────────────

@pytest.mark.parametrize("body,label", [
    ({"content": [], "stop_reason": "refusal"}, "안전 분류기 거부"),
    ({"content": [{"type": "thinking", "thinking": "x"}], "stop_reason": "end_turn"},
     "thinking 만 있고 text 없음 (절단 아님 → 계약 변경)"),
    ({"stop_reason": "end_turn"}, "content 키 자체가 없음"),
])
def test_no_text_block_raises_instead_of_returning_empty(body, label):
    """text 블록이 없으면 raise 한다 — 빈 문자열은 파싱 실패와 구분 불가하다.

    단 ``stop_reason == "max_tokens"`` 는 예외다 (아래 별도 테스트) — 그건 복구
    가능한 절단이라 호출부의 확장 재시도로 넘겨야 한다.
    """
    with pytest.raises(RuntimeError) as exc:
        _extract_text(body, "us.anthropic.claude-opus-5")
    msg = str(exc.value)
    # 진단에 필요한 3요소가 메시지에 있어야 한다: 모델 / stop_reason / 블록 종류.
    assert "us.anthropic.claude-opus-5" in msg, label
    assert body.get("stop_reason", "") in msg, label
    assert "refusal" in msg, f"{label} — refusal 원인 안내가 없다"


def test_max_tokens_truncation_returns_empty_for_retry_not_raise():
    """thinking 이 예산을 소진한 절단은 **빈 문자열** 로 넘겨 재시도 경로를 살린다.

    2026-08-18 실측: S2 Round 2 SME(sonnet-5) 가 blocks=['thinking'] +
    stop_reason=max_tokens 로 응답했는데, 여기서 raise 하면 호출부의
    ``max_tokens`` 확장 재호출에 **도달하지 못해** 9분치 진행이 버려진다.
    thinking 길이는 실행마다 변동하므로(같은 요청이 5/5 성공하기도 함) 재시도
    경로를 살리는 것이 유일하게 신뢰할 수 있는 대응이다.
    """
    body = {"content": [{"type": "thinking", "thinking": "예산을 다 씀"}],
            "stop_reason": "max_tokens"}
    assert _extract_text(body, "us.anthropic.claude-sonnet-5") == ""


def test_max_tokens_with_partial_text_still_returns_that_text():
    """절단이어도 text 가 일부 있으면 그것을 돌려준다 (버리지 않는다)."""
    body = {"content": [{"type": "thinking", "thinking": "x"},
                        {"type": "text", "text": '{"partial": tru'}],
            "stop_reason": "max_tokens"}
    assert _extract_text(body, "m") == '{"partial": tru'


def test_refusal_is_distinguishable_from_contract_change():
    """stop_reason 이 메시지에 실려 refusal 과 계약 변경을 갈라볼 수 있다."""
    refusal = {"content": [], "stop_reason": "refusal"}
    other = {"content": [{"type": "tool_use", "id": "x"}], "stop_reason": "tool_use"}
    with pytest.raises(RuntimeError, match="stop_reason=refusal"):
        _extract_text(refusal, "m")
    with pytest.raises(RuntimeError, match="stop_reason=tool_use"):
        _extract_text(other, "m")


# ── 배선: 실제 호출 경로가 이 헬퍼를 지나는가 ────────────────────────────

@patch("tools.bedrock._bedrock_client")
def test_invoke_wrapper_survives_thinking_block(mock_client_fn):
    """``invoke_bedrock_with_metadata`` 가 thinking 선행 응답을 처리한다.

    함수 경계 아래 배선을 확인한다 — 헬퍼만 고치고 호출부가 옛 인덱싱을 유지하면
    단위 테스트는 초록인데 산출물은 안 바뀐다 (이 리포에서 반복된 실패 유형).
    """
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _resp({
        "content": [
            {"type": "thinking", "thinking": "내부 추론"},
            {"type": "text", "text": "REAL_BODY"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    mock_client_fn.return_value = mock_client

    result = invoke_bedrock_with_metadata("prompt", model_id="us.anthropic.claude-opus-5")
    assert result["text"] == "REAL_BODY"
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["input_tokens"] == 10
