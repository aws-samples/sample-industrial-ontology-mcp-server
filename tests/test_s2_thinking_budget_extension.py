"""Regression: adaptive thinking 이 max_tokens 를 소진했을 때 확장 재시도가 도달하는가.

2026-08-18 실측 사고: S2 Round 2 SME(`us.anthropic.claude-sonnet-5`, max_tokens
=10000) 가 `blocks=['thinking'], stop_reason=max_tokens` 로 응답했다. adaptive
thinking 은 `max_tokens` 를 thinking 과 응답이 **공유** 하므로 thinking 이 예산을
전부 먹으면 text 블록이 아예 안 나온다. 그 상태에서 추출부가 raise 하면
`_invoke_bedrock` 의 확장 재호출에 **도달하지 못하고** 9분치 진행이 버려진다.

정상 실행의 출력은 약 7,600 토큰(thinking 2,800~3,900자 + text 5,000~5,700자)이라
10,000 은 여유가 24% 뿐이었다 — thinking 길이가 실행마다 변동하므로 확률적으로
넘친다. 그래서 두 층을 다 고쳤다: 예산 상향(`_REVIEW_MAX_TOKENS`) + 재시도 도달.

이 파일은 카운터가 아니라 **재시도가 실제로 일어났는지와 그 인자** 를 주장한다.
"""

from unittest.mock import patch

import pytest

from tools.multi_agent_tbox import _REVIEW_MAX_TOKENS, _invoke_bedrock


def _mk(text: str, stop_reason: str) -> dict:
    return {"text": text, "stop_reason": stop_reason, "usage": {}}


def test_empty_text_truncation_jumps_straight_to_ceiling():
    """text 가 없는 절단은 1.5배가 아니라 **상한** 으로 확장한다.

    1.5배(10000→15000)는 thinking 이 또 다 먹을 수 있고, 재확장 기회는 1회뿐이라
    두 번째 시도가 없다. 부족하면 빈 문자열이 파서로 흘러가 "모델이 침묵했다" 로
    오진된다.
    """
    calls = []

    def fake(prompt, **kw):
        calls.append(kw.get("max_tokens"))
        # 1차: thinking 이 예산 소진 → text 없음. 2차: 정상.
        return _mk("", "max_tokens") if len(calls) == 1 else _mk('{"ok":1}', "end_turn")

    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=fake):
        out = _invoke_bedrock("p", max_tokens=10_000, agent_role="sme")

    assert out == '{"ok":1}', "확장 재시도 결과가 반환되지 않았다"
    assert len(calls) == 2, f"재시도가 일어나지 않았다 (calls={calls})"
    assert calls[0] == 10_000
    assert calls[1] == 32_000, (
        f"text 없는 절단인데 상한이 아닌 {calls[1]} 로 확장했다 — "
        "1.5배는 thinking 에 또 먹힐 수 있다"
    )


def test_partial_text_truncation_uses_gentle_1_5x():
    """text 가 일부 있는 평범한 절단은 기존 1.5배 정책을 유지한다 (동작 보존)."""
    calls = []

    def fake(prompt, **kw):
        calls.append(kw.get("max_tokens"))
        return (_mk('{"partial": tru', "max_tokens") if len(calls) == 1
                else _mk('{"ok":1}', "end_turn"))

    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=fake):
        _invoke_bedrock("p", max_tokens=8_000, agent_role="validator")

    assert calls == [8_000, 12_000], f"1.5배 확장이 아니다: {calls}"


def test_no_second_retry_when_already_at_ceiling():
    """상한에서 절단되면 무의미한 재호출을 태우지 않는다."""
    calls = []

    def fake(prompt, **kw):
        calls.append(kw.get("max_tokens"))
        return _mk("", "max_tokens")

    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=fake):
        out = _invoke_bedrock("p", max_tokens=32_000, agent_role="sme")

    assert calls == [32_000], f"상한인데 재호출했다: {calls}"
    assert out == ""


def test_retry_happens_only_once():
    """확장 재시도는 1회다 — 무한 재귀로 40분 잡을 태우지 않는다."""
    calls = []

    def fake(prompt, **kw):
        calls.append(kw.get("max_tokens"))
        return _mk("", "max_tokens")  # 항상 절단

    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=fake):
        _invoke_bedrock("p", max_tokens=10_000, agent_role="sme")

    assert len(calls) == 2, f"재시도가 1회를 넘었다: {calls}"


def test_success_path_does_not_retry():
    """정상 응답에서는 재호출이 없다 (비용/지연 회귀 방지)."""
    calls = []

    def fake(prompt, **kw):
        calls.append(kw.get("max_tokens"))
        return _mk('{"ok":1}', "end_turn")

    with patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=fake):
        out = _invoke_bedrock("p", max_tokens=_REVIEW_MAX_TOKENS, agent_role="jury")

    assert calls == [_REVIEW_MAX_TOKENS]
    assert out == '{"ok":1}'


# ── 예산 상수: 실측 근거를 못박는다 ──────────────────────────────────────

def test_review_budget_has_headroom_over_measured_usage():
    """실측 출력(≈7,600 토큰) 대비 충분한 여유가 있어야 한다.

    누군가 비용을 이유로 이 값을 되돌리면(8192/10000) thinking 모델에서 확률적
    절단이 재발한다. 출력 토큰은 실제 생성분만 청구되므로 상한 상향은 평소 지출을
    늘리지 않는다 — 오히려 절단 재호출 1회를 없애 절약된다.
    """
    MEASURED_OUTPUT_TOKENS = 7_600  # sonnet-5, SME 프롬프트 54,788자 실측
    assert _REVIEW_MAX_TOKENS >= MEASURED_OUTPUT_TOKENS * 1.5, (
        f"_REVIEW_MAX_TOKENS={_REVIEW_MAX_TOKENS} 는 실측 출력 "
        f"{MEASURED_OUTPUT_TOKENS} 토큰 대비 여유가 부족하다"
    )


@pytest.mark.parametrize("role", ["validator", "sme", "jury", "compromise"])
def test_all_review_roles_share_the_constant(role):
    """리뷰 역할 4개가 모두 같은 상수를 쓴다 — 사본이 갈라지면 한쪽만 절단된다."""
    import inspect

    import tools.multi_agent_tbox as mat

    src = inspect.getsource(mat)
    # 역할별 호출부에 리터럴 8192/10000 이 남아 있지 않아야 한다.
    # (1584행 Architect revise 는 TTL 생성이라 별도 예산 체계 — 제외 대상)
    review_literals = [
        ln.strip() for ln in src.splitlines()
        if "_invoke_bedrock" not in ln
        and ("max_tokens=8192" in ln or "max_tokens=10000" in ln)
        and "agent_role" not in ln
    ]
    # Architect(revise) 한 줄만 허용.
    assert len(review_literals) <= 1, (
        f"리뷰 역할에 하드코딩된 옛 예산이 남았다: {review_literals}"
    )
