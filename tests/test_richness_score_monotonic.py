"""description richness 점수가 단조 증가하는가 — 개선이 감점으로 읽히지 않게.

2026-08-29 실측. 파이프라인 재실행에서 ``measure_instance_quality`` 총점이
**95.5 → 89.6** 으로 떨어졌다. 그런데 5개 메트릭은 전부 같거나 개선이었다::

    population_completeness   100.0% → 100.0%
    property_completeness      80.5% →  86.7%   개선
    description_richness        0.97 →   1.23   개선
    interlinking              100.0% → 100.0%
    datatype_consistency      100.0% → 100.0%
    ─────────────────────────────────────────
    overall                     95.5 →   89.6   하락 (?)

원인은 richness 의 점수 변환식이었다::

    richness * 100                if richness <= 1     # 0.97 → 97.0
    min(richness / 2 * 100, 100)  if richness  > 1     # 1.01 → 50.5 (!)

1.0 에서 100점이던 것이 **1.01 에서 50.5점**이 되고 2.0 에서야 100점으로 복귀하는 V자
함정이다. richness 가 0.97 → 1.23 으로 좋아졌는데 그 때문에 35.5점을 잃었다.

## 왜 위험한가

품질 점수는 회귀 감지에 쓰인다 (``quality_history``). 개선이 하락으로 기록되면
"수정이 품질을 떨어뜨렸다" 는 잘못된 결론으로 이어지고, 반대로 richness 를 1.0 아래로
낮추는 것이 점수를 올리는 **지표 매수** 경로가 된다.

## 이 테스트의 방향

"1.0 초과가 만점" 만 주장하면 전 구간을 만점으로 만들어도 통과한다. 세 축을 고정한다:

* 단조 — richness 가 커질 때 점수가 절대 낮아지지 않는다
* 연속 — 1.0 경계에서 점프/추락이 없다
* 판별 — 1.0 미만은 여전히 비례해서 감점된다 (전부 만점이 아니다)
"""
from __future__ import annotations

import pytest

from tools.kg_validation import _richness_to_score

# ── 단조: 개선이 감점되지 않는다 ────────────────────────────────────────


def test_score_is_monotonic_non_decreasing():
    """THE REGRESSION: richness 가 커질 때 점수가 낮아지지 않는다.

    이전 식은 0.97 → 97.0 인데 1.01 → 50.5 로 **떨어졌다**.
    """
    values = [0.0, 0.1, 0.5, 0.9, 0.97, 0.999, 1.0, 1.001, 1.01, 1.23, 1.5, 2.0, 5.0]
    scores = [_richness_to_score(v) for v in values]

    for (v_prev, s_prev), (v_cur, s_cur) in zip(
        zip(values, scores, strict=True), zip(values[1:], scores[1:], strict=True),
        strict=False,
    ):
        assert s_cur >= s_prev, (
            f"richness {v_prev} → {v_cur} 에서 점수가 {s_prev} → {s_cur} 로 떨어졌다"
        )


def test_no_cliff_at_one():
    """1.0 경계에서 급락이 없다 — 이전 식은 여기서 절반이 됐다."""
    just_below = _richness_to_score(0.999)
    at_one = _richness_to_score(1.0)
    just_above = _richness_to_score(1.001)

    assert abs(at_one - just_below) < 1.0, "1.0 직전과 1.0 사이에 점프가 있다"
    assert abs(just_above - at_one) < 1.0, "1.0 직후에 급락/급등이 있다"


def test_the_regression_case_scores_full():
    """실측값 1.23 이 만점인가 — 이 값 때문에 35.5점을 잃었다."""
    assert _richness_to_score(1.23) == 100.0


# ── 판별: 1.0 미만은 비례 감점 (NEGATIVE 방향) ──────────────────────────


@pytest.mark.parametrize(
    ("richness", "expected"),
    [(0.0, 0.0), (0.25, 25.0), (0.5, 50.0), (0.8, 80.0), (0.97, 97.0), (1.0, 100.0)],
)
def test_below_one_is_proportional(richness, expected):
    """1.0 미만은 비례해서 감점된다 — 전부 만점이면 판별력이 없다."""
    assert _richness_to_score(richness) == pytest.approx(expected)


def test_zero_richness_scores_zero():
    """서술이 전무하면 0점이다 — clamp 가 바닥까지 올리면 안 된다."""
    assert _richness_to_score(0.0) == 0.0


def test_negative_is_clamped_to_zero():
    """음수는 있을 수 없지만 방어한다 (음수 점수가 평균을 오염시킨다)."""
    assert _richness_to_score(-0.5) == 0.0


def test_score_never_exceeds_100():
    """상한을 넘지 않는다 — 평균 계산이 100 을 초과하면 척도가 깨진다."""
    for richness in (1.0, 2.0, 10.0, 1000.0):
        assert _richness_to_score(richness) <= 100.0


# ── 배선: 총점 계산에 쓰인다 ────────────────────────────────────────────


def test_helper_is_used_in_overall_score():
    """총점 계산이 이 헬퍼를 쓰는가 — 인라인 식이 남으면 수정이 무의미하다."""
    import pathlib

    src = pathlib.Path("tools/kg_validation.py").read_text(encoding="utf-8")

    assert "_richness_to_score(avg_desc_richness)" in src, "총점이 헬퍼를 쓰지 않는다"
    assert "avg_desc_richness / 2 * 100" not in src, "이전 V자 식이 남아 있다"


def test_deployed_richness_exceeds_one():
    """배포 산출물의 richness 가 1.0 을 넘는가 — 이 수정의 전제.

    총점 자체는 여기서 검사하지 않는다: conftest 의 ``_isolate_quality_history`` 가
    ``GENERATED_ABOX_DIR`` 를 tmp 로 돌려 ``abox_stats.json`` 을 못 읽으므로
    ``population_completeness`` 가 0 이 되고 총점이 실제(97.3)와 달라진다(77.3).
    그 격리는 배포 이력 오염을 막는 의도된 동작이라 우회하지 않는다.

    대신 이 수정이 실제로 영향을 주는 값(richness > 1.0)과 그 값의 점수 변환을
    각각 고정한다 — 총점은 파이프라인 실행에서 확인한다.
    """
    import json
    import pathlib

    if not pathlib.Path("data/generated/inferred/all_inferred.ttl").exists():
        pytest.skip("추론 산출물 없음")

    from tools.kg_validation import measure_instance_quality

    result = json.loads(measure_instance_quality(use_inferred=True))
    richness = result["metrics"]["description_richness"]["avg_ratio"]

    assert richness > 1.0, (
        f"richness {richness} — 1.0 이하면 이 수정이 영향을 주지 않는다. "
        "전제가 바뀌었으면 테스트를 갱신하라."
    )
    assert _richness_to_score(richness) == 100.0, (
        f"richness {richness} 가 만점이 아니다 — V자 식이 되살아났나"
    )
