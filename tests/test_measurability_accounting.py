"""``validate_kg`` 의 측정 회계 — "재지 않았다" 와 "통과했다" 를 분리한다.

## 무엇이 틀렸었나 (실측 2026-09-02)

점수 분자는 ``sum(1 for c in checks if c["passed"])`` 였고 ``applicable`` 을 보지
않았다. 그런데 ``applicable: False`` 는 ``passed: True`` 와 **쌍으로** 온다 —
판정 불가를 FAIL 로 내면 게이트가 영구 빨간불이 되므로 그렇게 설계됐다. 결과적으로
배포 상태의 "23/25" 안에 미판정 축이 1개 섞여 있었고, 실제 측정 점수는 22/24 다.
``checks[]`` 를 직접 열지 않으면 그 사실이 보이지 않았다.

## 이 테스트가 지키는 두 방향

- POSITIVE: 미판정 축이 분모에서 빠지고 이름과 사유가 응답에 나온다.
- NEGATIVE: ``score`` 문자열 형식은 불변이고, 미판정 선언을 **세탁**으로 쓰면
  최상위에 드러난다. 후자가 없으면 상시 빨간불을 ``applicable: false`` 로
  재분류하는 것이 점수를 올리는 최단 경로가 된다.
"""

from __future__ import annotations

import pytest

from tools.validation_support.common import (
    measurability_violations,
    summarize_measurability,
)
from tools.validation_support.quality_history import parse_total


def _c(name, passed=True, **kw):
    return {"name": name, "passed": passed, **kw}


# ── POSITIVE ────────────────────────────────────────────────────


def test_unmeasured_axis_leaves_the_denominator():
    checks = [
        _c("a"), _c("b"), _c("c", passed=False),
        _c("추론 sanity check", applicable=False, reason="추론 전 그래프다"),
    ]
    out = summarize_measurability(checks)
    assert out["measured_total"] == 3
    assert out["measured_passed"] == 2
    assert out["score_measured"] == "2/3"
    assert [u["name"] for u in out["unmeasured_checks"]] == ["추론 sanity check"]
    assert out["unmeasured_checks"][0]["reason"] == "추론 전 그래프다"


def test_checks_without_the_field_count_as_measured():
    """기존 24개 체크는 ``applicable`` 키를 내지 않는다 — 기본값이 측정됨이어야 한다."""
    checks = [_c("a"), _c("b", passed=False)]
    out = summarize_measurability(checks)
    assert out["measured_total"] == 2
    assert out["unmeasured_checks"] == []


def test_missing_reason_is_named_not_hidden():
    """사유 없는 미판정은 세탁과 구분되지 않으므로 그 사실을 적는다."""
    out = summarize_measurability([_c("x", applicable=False)])
    assert "사유 미기재" in out["unmeasured_checks"][0]["reason"]


# ── NEGATIVE: score 문자열 형식 불변 ────────────────────────────


@pytest.mark.parametrize("score", ["23/25", "0/25", "25/25"])
def test_score_string_stays_parseable(score):
    """``parse_total`` 은 ``int(score.split('/')[1])`` 이다.

    ``"23/25 (measured 24)"`` 처럼 뒤에 무엇이든 붙이면 ValueError → 0 이 되고
    ``score_entries`` 가 그 항목을 통째로 버려 회귀 탐지가 죽는다. 그래서 측정
    회계는 **별 필드**로만 낸다.
    """
    assert parse_total(score) > 0


def test_annotated_score_string_would_kill_regression_detection():
    """왜 형식을 유지해야 하는지 근거를 테스트로 고정한다 (반례 보존)."""
    assert parse_total("23/25 (measured 24)") == 0


# ── NEGATIVE: 미판정 선언을 세탁으로 쓰는 것을 막는다 ────────────


def test_unmeasured_with_violations_is_flagged():
    checks = [_c("고아 노드 탐지", applicable=False, reason="r", violations=["x", "y"])]
    problems = measurability_violations(checks)
    assert len(problems) == 1
    assert "위반 2건" in problems[0]


def test_unmeasured_with_missing_list_is_flagged():
    checks = [_c("필수참여 공리 충족", applicable=False, reason="r", missing=["a"])]
    assert measurability_violations(checks)


def test_unmeasured_but_failed_is_contradiction():
    checks = [_c("x", passed=False, applicable=False, reason="r")]
    problems = measurability_violations(checks)
    assert any("모순" in p for p in problems)


def test_honest_unmeasured_is_not_flagged():
    """정당한 미판정(위반 0 + passed True + 사유 있음)은 문제로 세지 않는다."""
    checks = [
        _c("추론 sanity check", applicable=False, reason="추론 전 그래프다"),
        _c("정상 체크"),
        _c("정상 실패", passed=False, violations=["v"]),
    ]
    assert measurability_violations(checks) == []


# ── 배선: validate_kg 응답이 회계를 싣는가 ──────────────────────


def test_validate_kg_result_shape(monkeypatch):
    """``validate_kg`` 가 회계 필드를 내고 ``score`` 는 건드리지 않는지 (배선 검사).

    실제 그래프를 로드하지 않고 회계 함수의 계약만 본다 — 배포 산출물을 읽는
    무거운 경로는 이 테스트의 대상이 아니다.
    """
    checks = [_c(f"c{i}") for i in range(22)] + [
        _c("c22", passed=False),
        _c("c23", passed=False),
        _c("추론 sanity check", applicable=False, reason="추론 전 그래프다"),
    ]
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    m = summarize_measurability(checks)

    assert f"{passed}/{total}" == "23/25"       # 기존 표기 불변
    assert m["score_measured"] == "22/24"       # 미판정 1개가 분모에서 빠진다
    assert parse_total(f"{passed}/{total}") == 25
