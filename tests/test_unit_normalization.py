"""A3 — Unit Normalization Auto-Recommend tests.

Deterministic heuristic: max/min ratio > UNIT_MISMATCH_RATIO (default 50)
on numeric DP values with ≥5 positive samples → emit unit_warning.
Keyword matching (length / temperature / weight) provides a unit hint.
No LLM — purely value distribution + lexical hints.
"""

from __future__ import annotations

import os
from unittest.mock import patch

# ── helper-level tests ─────────────────────────────


def test_no_mismatch_when_range_narrow():
    """Narrow range on length keyword → None (no warning)."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [10, 15, 20, 12, 18]  # ratio ~2x
    assert _detect_unit_mismatch(values, "diameter") is None


def test_skips_small_samples():
    """<5 positive samples → None regardless of ratio."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    assert _detect_unit_mismatch([1, 100], "diameter") is None


def test_detects_mm_cm_mix_diameter():
    """mm/cm mixed on diameter → warning with mm suggestion."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [0.5, 1.5, 10, 20, 25]  # ratio = 50x
    result = _detect_unit_mismatch(values, "diameter")
    assert result is not None
    assert result["severity"] == "warning"
    assert "mm" in result["suggestion"]
    assert result["ratio"] >= 50
    assert "sample_low" in result and "sample_high" in result


def test_temperature_hint():
    """temperature keyword → celsius suggestion."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [25, 1500, 1800, 20, 30]  # ratio 90x
    result = _detect_unit_mismatch(values, "temperature")
    assert result is not None
    assert "celsius" in result["suggestion"]


def test_weight_hint():
    """weight keyword → kg suggestion."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [0.5, 1500, 2, 1800, 1]  # ratio 3600x
    result = _detect_unit_mismatch(values, "weight")
    assert result is not None
    assert "kg" in result["suggestion"]


def test_no_hint_for_unknown_prop():
    """Unknown prop_name → warning emitted but no specific unit hint."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    # Need ≥5 positive values to trigger the check.
    values = [0.5, 100, 200, 150, 250]
    result = _detect_unit_mismatch(values, "value")
    assert result is not None
    # no mm/celsius/kg hint for unknown prop
    assert "mm" not in result["suggestion"]
    assert "celsius" not in result["suggestion"]
    assert "kg" not in result["suggestion"]


def test_env_var_disables():
    """UNIT_MISMATCH_RATIO=0 → always None."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    with patch.dict(os.environ, {"UNIT_MISMATCH_RATIO": "0"}):
        assert _detect_unit_mismatch([1, 10, 100, 1000, 10000], "diameter") is None


def test_env_var_custom_threshold():
    """Custom threshold UNIT_MISMATCH_RATIO=10 changes when warning fires."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    with patch.dict(os.environ, {"UNIT_MISMATCH_RATIO": "10"}):
        # Only 2 samples → None (below sample floor regardless of threshold).
        assert _detect_unit_mismatch([1, 15], "diameter") is None
        # 5 samples, ratio 15x → exceeds threshold 10 → warning.
        result = _detect_unit_mismatch([1, 2, 3, 4, 15], "diameter")
        assert result is not None
        assert result["severity"] == "warning"


def test_filters_zeros_and_negatives():
    """Zero/negative values filtered — need 5+ positive remaining or None."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    # 6 positive values (0 and -5 dropped) → ratio 50/0.5 = 100x warning.
    values = [0, -5, 0.5, 10, 20, 30, 40, 50]
    result = _detect_unit_mismatch(values, "diameter")
    assert result is not None
    assert result["ratio"] >= 50

    # Only 4 positives (0 dropped) → below min sample → None.
    values_short = [0, 0, 1, 2, 3, 100]
    assert _detect_unit_mismatch(values_short, "diameter") is None


# ── integration: _compute_stats wires warning into stats dict ─────────


def test_integration_compute_stats_contains_warning():
    """_compute_stats with mixed-unit numeric DP values returns unit_warning in stats."""
    from tools.semantic_dictionary import _compute_stats

    # Mixed mm/cm on a length-keyword prop, 5 positives, ratio 50x.
    values = ["0.5", "1.5", "10", "20", "25"]
    stats = _compute_stats(values, "decimal", prop_name="diameter")
    assert "unit_warning" in stats
    assert stats["unit_warning"]["severity"] == "warning"
    assert "mm" in stats["unit_warning"]["suggestion"]


def test_integration_compute_stats_no_warning_normal_values():
    """Normal narrow-range values → no unit_warning key."""
    from tools.semantic_dictionary import _compute_stats

    values = ["10", "15", "20", "12", "18"]
    stats = _compute_stats(values, "decimal", prop_name="diameter")
    assert "unit_warning" not in stats


def test_integration_compute_stats_backward_compat_without_prop_name():
    """_compute_stats still callable without prop_name kwarg."""
    from tools.semantic_dictionary import _compute_stats

    values = ["10", "20", "30"]
    stats = _compute_stats(values, "decimal")
    assert stats["count"] == 3
    assert stats["min"] == 10.0
    assert stats["max"] == 30.0


# ── False positive reduction — 자연 고변동 DP 는 skip ─────────────────


def test_cost_keyword_skipped():
    """repairCostUsd 같은 금액 DP 는 자연스럽게 고변동 — warning 없음."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    # $10 ~ $950 — 95x ratio 지만 금액이라 정상
    values = [10, 25, 100, 500, 950]
    assert _detect_unit_mismatch(values, "repairCostUsd") is None


def test_price_keyword_skipped():
    """price 키워드도 skip."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [5, 50, 500, 5000, 50000]
    assert _detect_unit_mismatch(values, "itemPrice") is None


def test_stock_keyword_skipped():
    """currentStock / availableStock 은 자연 고변동 — skip."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [1, 10, 100, 1000, 5000]
    assert _detect_unit_mismatch(values, "currentStock") is None
    assert _detect_unit_mismatch(values, "availableStock") is None


def test_count_keyword_skipped():
    """count / quantity 키워드 skip."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    values = [1, 5, 50, 500, 5000]
    assert _detect_unit_mismatch(values, "alarmCount") is None
    assert _detect_unit_mismatch(values, "orderQuantity") is None


def test_duration_keyword_skipped():
    """duration / seconds / minutes / hours 도 skip."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    # 1초 ~ 1시간
    values = [1, 10, 100, 1000, 3600]
    assert _detect_unit_mismatch(values, "downtimeSeconds") is None
    assert _detect_unit_mismatch(values, "totalDuration") is None


def test_physical_measurement_still_warns():
    """물리 측정 DP (length / temperature / weight 등) 는 여전히 warning."""
    from tools.semantic_dictionary import _detect_unit_mismatch

    # 이전 테스트와 동일한 케이스 — 회귀 가드
    values = [0.5, 1.5, 10, 20, 25]  # mm/cm mix
    assert _detect_unit_mismatch(values, "thicknessMm") is not None

    values2 = [25, 1500, 1800, 20, 30]  # Celsius vs Kelvin
    assert _detect_unit_mismatch(values2, "temperatureC") is not None
