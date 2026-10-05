"""tests for M9: Metric Auto-calibration — smooth gradient scoring.

_smooth_score 함수의 경계값, 연속성, 단조감소 특성을 검증한다.
"""


# **실제 구현을 import 한다.** 예전엔 이 파일이 함수 본문을 복제해 테스트했고
# ("테스트용으로 복제" 주석), 그러면 tools/tbox_metrics.py 가 바뀌어도 이 테스트는
# 절대 실패하지 않는다 — 커버리지 연극이었다 (2026-08-08 규명). 그래서 실제
# 구현을 클로저에서 모듈 레벨로 빼내고 여기서 직접 가져온다.
from tools.tbox_metrics import smooth_score as _smooth_score


class TestSmoothScoreBasic:
    """기본 동작: 범위 내 만점, 범위 밖 감소."""

    def test_inside_range_returns_max(self):
        assert _smooth_score(3, 2, 5, 20) == 20
        assert _smooth_score(2, 2, 5, 20) == 20
        assert _smooth_score(5, 2, 5, 20) == 20

    def test_below_range_reduces_score(self):
        score = _smooth_score(1.5, 2, 5, 20)
        assert 0 < score < 20

    def test_above_range_reduces_score(self):
        score = _smooth_score(7, 2, 5, 20)
        assert 0 < score < 20

    def test_far_outside_returns_zero(self):
        assert _smooth_score(100, 2, 5, 20) == 0
        assert _smooth_score(-100, 2, 5, 20) == 0

    def test_never_negative(self):
        for v in [-10, -1, 0, 50, 100, 1000]:
            assert _smooth_score(v, 2, 5, 20) >= 0


class TestSmoothScoreMonotonicity:
    """범위 밖에서 거리가 멀어질수록 점수가 단조감소해야 한다."""

    def test_monotone_below(self):
        """ideal_min 아래: 값이 작아질수록 점수가 줄어야 함."""
        scores = [_smooth_score(v, 2, 5, 20) for v in [1.5, 1.0, 0.5, 0.0]]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], f"단조감소 위반: {scores}"

    def test_monotone_above(self):
        """ideal_max 위: 값이 커질수록 점수가 줄어야 함."""
        scores = [_smooth_score(v, 2, 5, 20) for v in [6, 7, 8, 10]]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], f"단조감소 위반: {scores}"


class TestSmoothScoreContinuity:
    """경계에서 불연속 점프가 없어야 한다 (binary scoring 대비 핵심 개선점)."""

    def test_no_large_jump_at_lower_boundary(self):
        """하한 경계 바로 안/밖 점수 차이가 max_points의 25% 이내."""
        inside = _smooth_score(2.0, 2, 5, 20)
        outside = _smooth_score(1.9, 2, 5, 20)
        assert inside - outside <= 5, f"경계 점프 과다: {inside} -> {outside}"

    def test_no_large_jump_at_upper_boundary(self):
        """상한 경계 바로 안/밖 점수 차이가 max_points의 25% 이내."""
        inside = _smooth_score(5.0, 2, 5, 20)
        outside = _smooth_score(5.1, 2, 5, 20)
        assert inside - outside <= 5, f"경계 점프 과다: {inside} -> {outside}"


class TestSmoothScoreFalloff:
    """falloff 파라미터가 감소 속도를 제어하는지 검증."""

    def test_higher_falloff_reduces_faster(self):
        slow = _smooth_score(1, 2, 5, 20, falloff=1.0)
        fast = _smooth_score(1, 2, 5, 20, falloff=4.0)
        assert slow >= fast

    def test_default_falloff_is_2(self):
        explicit = _smooth_score(1, 2, 5, 20, falloff=2.0)
        default = _smooth_score(1, 2, 5, 20)
        assert explicit == default


class TestSmoothScoreEdgeCases:
    """경계 조건 및 특수 입력값."""

    def test_zero_ideal_min(self):
        """ideal_min=0일 때 division by zero 없이 동작."""
        assert _smooth_score(0, 0, 5, 15) == 15
        score = _smooth_score(-1, 0, 5, 15)
        assert score >= 0

    def test_float_values(self):
        """실수 메트릭 (RR, AR) 정확 처리."""
        assert _smooth_score(0.45, 0.3, 0.6, 15) == 15
        score = _smooth_score(0.2, 0.3, 0.6, 15)
        assert 0 < score < 15

    def test_single_point_range(self):
        """ideal_min == ideal_max인 단일점 범위."""
        assert _smooth_score(5, 5, 5, 20) == 20
        assert _smooth_score(4, 5, 5, 20) < 20


class TestMetricScoringIntegration:
    """measure_tbox_metrics의 scoring dict 로직을 시뮬레이션하여 통합 검증."""

    def test_dit_scoring(self):
        """DIT: 2~5 범위 만점, 벗어나면 감소."""
        assert _smooth_score(3, 2, 5, 20) == 20
        assert _smooth_score(1, 2, 5, 20) < 20
        assert _smooth_score(7, 2, 5, 20) < 20

    def test_noc_scoring(self):
        """NOC: 2~10 범위."""
        assert _smooth_score(5, 2, 10, 15) == 15
        assert _smooth_score(0, 2, 10, 15) < 15
        assert _smooth_score(15, 2, 10, 15) < 15

    def test_rr_scoring(self):
        """RR: 0.3~0.6 범위."""
        assert _smooth_score(0.45, 0.3, 0.6, 15) == 15
        assert _smooth_score(0.1, 0.3, 0.6, 15) < 15

    def test_ar_scoring(self):
        """AR: 3~15 범위."""
        assert _smooth_score(8, 3, 15, 15) == 15
        assert _smooth_score(1, 3, 15, 15) < 15
        assert _smooth_score(20, 3, 15, 15) < 15

    def test_axiom_continuous_capped(self):
        """Axiom: min(ratio, 1.0) * 15 로 연속 + 상한 캡."""
        axiom_target = 2
        # 목표 달성
        assert round(min(3.0 / axiom_target, 1.0) * 15, 1) == 15
        # 목표 미달
        score = round(min(1.0 / axiom_target, 1.0) * 15, 1)
        assert score == 7.5
        # 0이면 0점
        assert round(min(0 / max(axiom_target, 1), 1.0) * 15, 1) == 0.0

    def test_total_score_range(self):
        """모든 메트릭이 이상적일 때 총점이 85 이상 (grade A 가능)."""
        dit_s = _smooth_score(3, 2, 5, 20)
        noc_s = _smooth_score(5, 2, 10, 15)
        rr_s = _smooth_score(0.45, 0.3, 0.6, 15)
        ar_s = _smooth_score(8, 3, 15, 15)
        annotation_s = round(100 / 100 * 20, 1)
        axiom_s = round(min(3.0 / 2, 1.0) * 15, 1)
        total = dit_s + noc_s + rr_s + ar_s + annotation_s + axiom_s
        assert total == 100

    def test_worst_case_above_zero(self):
        """모든 메트릭이 최악이어도 총점 >= 0."""
        dit_s = _smooth_score(0, 2, 5, 20)
        noc_s = _smooth_score(0, 2, 10, 15)
        rr_s = _smooth_score(0, 0.3, 0.6, 15)
        ar_s = _smooth_score(0, 3, 15, 15)
        annotation_s = round(0 / 100 * 20, 1)
        axiom_s = round(min(0 / max(2, 1), 1.0) * 15, 1)
        total = dit_s + noc_s + rr_s + ar_s + annotation_s + axiom_s
        assert total >= 0
