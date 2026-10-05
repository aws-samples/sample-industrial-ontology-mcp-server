"""D4 — _compute_stats 의 value_stats dict 추가 테스트.

percentile 을 명시적 라벨 (p10/p50/p90) 로 노출해 Claude 가 자연어 → SPARQL
변환 시 "쿼리 값이 알려진 범위 밖" 을 자동 판단할 수 있도록 한다.

기존 필드 (min/max/example_values/distinct_values/distinct_count) 는 backward
compat 을 위해 유지하고, value_stats 는 추가 전용.
"""

from tools.semantic_dictionary import _compute_stats


def test_value_stats_decimal_numeric():
    """Numeric DP — min/max/p10/p50/p90/count 포함."""
    values = [str(v) for v in range(1, 101)]  # 1~100
    stats = _compute_stats(values, "decimal")

    assert "value_stats" in stats
    vs = stats["value_stats"]
    assert vs["count"] == 100
    assert vs["min"] == 1.0
    assert vs["max"] == 100.0
    # p10 ≈ 10.9, p50 ≈ 50.5, p90 ≈ 90.1 (선형보간 기준)
    assert 9.5 < vs["p10"] < 11.5
    assert 49 < vs["p50"] < 52
    assert 89 < vs["p90"] < 91


def test_value_stats_percentiles_round_interpolation_residue():
    """선형보간 percentile 과 example_values 는 소수 6자리로 저장."""
    stats = _compute_stats(["0.01", "2.04"], "decimal")

    vs = stats["value_stats"]
    assert [vs["p10"], vs["p50"], vs["p90"]] == [0.213, 1.025, 1.837]
    assert stats["example_values"] == [0.213, 1.025, 1.837]


def test_value_stats_empty_values():
    """빈 values → value_stats 없음 (early return) 또는 count=0."""
    stats = _compute_stats([], "decimal")
    assert stats["count"] == 0
    # empty 는 value_stats 없어도 됨 (early return)
    assert "value_stats" not in stats or stats["value_stats"]["count"] == 0


def test_value_stats_string_small_distinct():
    """≤20 distinct string — distinct_values + value_stats.distinct_count."""
    values = ["A", "B", "A", "C"]
    stats = _compute_stats(values, "string")

    # 기존 필드 유지
    assert stats["distinct_values"] == ["A", "B", "C"]

    # 신규 필드
    vs = stats["value_stats"]
    assert vs["distinct_count"] == 3
    assert vs["count"] == 4


def test_value_stats_string_large_distinct():
    """>20 distinct string — distinct_count + example_values + value_stats."""
    values = [f"v{i}" for i in range(30)]
    stats = _compute_stats(values, "string")

    # 기존
    assert stats["distinct_count"] == 30
    assert "example_values" in stats

    # 신규
    assert stats["value_stats"]["distinct_count"] == 30
    assert stats["value_stats"]["count"] == 30


def test_value_stats_datetime():
    """dateTime — min/max/p50 (median)."""
    values = [
        "2025-01-01T00:00:00",
        "2025-06-01T00:00:00",
        "2025-12-01T00:00:00",
    ]
    stats = _compute_stats(values, "dateTime")

    vs = stats["value_stats"]
    assert vs["min"] == "2025-01-01T00:00:00"
    assert vs["max"] == "2025-12-01T00:00:00"
    assert "p50" in vs


def test_value_stats_backward_compat_example_values_preserved():
    """기존 example_values 리스트 필드가 value_stats 추가 후에도 그대로."""
    values = [str(v) for v in range(1, 101)]
    stats = _compute_stats(values, "decimal")

    # 기존 필드 보존
    assert "example_values" in stats
    assert len(stats["example_values"]) == 3  # p10, p50, p90 순서
    assert "min" in stats
    assert "max" in stats

    # 신규 필드
    assert "value_stats" in stats
