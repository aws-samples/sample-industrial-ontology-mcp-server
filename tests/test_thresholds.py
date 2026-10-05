"""Tests for tools/validation_support/thresholds.py — Session 6."""
from __future__ import annotations

import json
from unittest.mock import patch


def test_get_tier_thresholds_structure():
    from tools.validation_support.thresholds import get_tier_thresholds
    tiers = get_tier_thresholds()
    assert "master" in tiers and "transaction" in tiers and "inferred" in tiers
    for cfg in tiers.values():
        assert "dangling_rate" in cfg
        assert "property_coverage" in cfg
        assert "concentration_alert" in cfg


def test_master_max_count():
    from tools.validation_support.thresholds import get_master_tier_max_count
    assert get_master_tier_max_count() == 500


def test_report_limits():
    from tools.validation_support.thresholds import get_report_limits
    r = get_report_limits()
    assert r["samples_per_check"] == 20
    assert r["violations_per_property"] == 5


def test_regression_thresholds():
    from tools.validation_support.thresholds import get_regression_thresholds
    r = get_regression_thresholds()
    assert r["total_score_warning_delta"] == 5
    assert r["total_score_critical_delta"] == 10


def test_kg_validation_TIER_THRESHOLDS_delegates():
    """kg_validation.TIER_THRESHOLDS가 중앙 설정 값을 반영하는지."""
    from tools.kg_validation import TIER_THRESHOLDS
    assert TIER_THRESHOLDS["master"]["dangling_rate"] == 0.01
    assert TIER_THRESHOLDS["transaction"]["property_coverage"] == 50
    assert TIER_THRESHOLDS["inferred"]["no_instance_allowed"] is True


def test_classify_class_tiers_uses_master_max():
    from tools.kg_validation import _classify_class_tiers
    stats = {"per_class": {
        "BigCls": {"instance_count": 1000},
        "SmallCls": {"instance_count": 50},
        "EmptyCls": {"instance_count": 0},
    }}
    r = _classify_class_tiers(stats)
    assert r["BigCls"] == "transaction"
    assert r["SmallCls"] == "master"
    assert r["EmptyCls"] == "inferred"


def test_fallback_to_defaults_when_file_missing(tmp_path):
    """quality_thresholds.json이 없어도 defaults로 동작."""
    from tools.validation_support import thresholds
    with patch("tools.validation_support.thresholds._THRESHOLDS_PATH",
               str(tmp_path / "nope.json")):
        thresholds.reload_cache()
        try:
            t = thresholds.get_tier_thresholds()
            assert "master" in t
            assert t["master"]["dangling_rate"] == 0.01
        finally:
            thresholds.reload_cache()  # 다른 테스트에 영향 없게 복구


def test_merged_with_defaults_partial_json(tmp_path):
    """일부 섹션만 있는 json도 defaults로 보완."""
    from tools.validation_support import thresholds
    partial = {"class_tiers": {"master": {"dangling_rate": 0.99}}}
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(partial))
    with patch("tools.validation_support.thresholds._THRESHOLDS_PATH", str(path)):
        thresholds.reload_cache()
        try:
            t = thresholds.get_tier_thresholds()
            # override 반영
            assert t["master"]["dangling_rate"] == 0.99
            # default 유지
            assert t["transaction"]["dangling_rate"] == 0.05
        finally:
            thresholds.reload_cache()
