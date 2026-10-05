"""Tests for tools/validation_support/checks/statistical.py — Session 13."""
from __future__ import annotations

import json

from rdflib import XSD, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.validation_support.checks.statistical import (
    _NUMERIC_XSD,
    check_numeric_outliers,
    check_relationship_outliers,
    check_string_patterns,
    check_value_ranges,
)


def _cls(n):
    return URIRef(f"{DOMAIN_NS}{n}")


def _inst(n):
    return URIRef(f"{DOMAIN_INST_NS}{n}")


def test_numeric_xsd_constants():
    assert str(XSD.decimal) in _NUMERIC_XSD
    assert str(XSD.integer) in _NUMERIC_XSD


def test_value_ranges_no_rules_file(tmp_path):
    r = check_value_ranges(_new_graph(), rules_dir=str(tmp_path))
    assert r["passed"] is True
    assert "건너뜀" in r["message"]


def test_value_ranges_no_violations(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "value_ranges.json").write_text(json.dumps({
        "ranges": {"pressure": {"min": 0, "max": 100, "description": "bar"}}
    }))
    g = _new_graph()
    g.add((_inst("A"), _cls("pressure"),
           Literal("50", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=str(rules_dir))
    assert r["passed"] is True


def test_numeric_outliers_small_sample_skipped():
    g = _new_graph()
    g.add((_inst("A"), _cls("v"), Literal(100, datatype=XSD.decimal)))
    g.add((_inst("B"), _cls("v"), Literal(200, datatype=XSD.decimal)))
    # n=2 < 3 이므로 skip
    r = check_numeric_outliers(g)
    assert r["checked_properties"] == 0


def test_numeric_outliers_detected():
    g = _new_graph()
    # 일반 값 10회 + 튀는 값 1개
    for i in range(10):
        g.add((_inst(f"N{i}"), _cls("v"),
               Literal(100 + i, datatype=XSD.integer)))
    g.add((_inst("Outlier"), _cls("v"),
           Literal(10000, datatype=XSD.integer)))
    r = check_numeric_outliers(g)
    assert r["outlier_count"] >= 1


def test_string_patterns_no_strings():
    r = check_string_patterns(_new_graph())
    assert r["passed"] is True
    assert "없음" in r["message"]


def test_string_patterns_invalid_values():
    g = _new_graph()
    g.add((_inst("A"), _cls("name"), Literal("valid", datatype=XSD.string)))
    g.add((_inst("B"), _cls("name"), Literal("n/a", datatype=XSD.string)))
    g.add((_inst("C"), _cls("name"), Literal("null", datatype=XSD.string)))
    r = check_string_patterns(g)
    issues = [w["issue"] for w in r["warnings"]]
    assert any("무효 문자열" in s for s in issues)


def test_relationship_outliers_no_ops():
    r = check_relationship_outliers(_new_graph())
    assert r["passed"] is True


def test_relationship_outliers_detected():
    g = _new_graph()
    # 대부분의 subject는 1~2개 관계, 하나는 과도
    for i in range(10):
        g.add((_inst(f"S{i}"), _cls("hasX"), _inst("T")))
    # outlier: 100개 관계
    for i in range(100):
        g.add((_inst("Hub"), _cls("hasX"), _inst(f"T{i}")))
    r = check_relationship_outliers(g)
    assert r["outlier_count"] >= 1
    assert any(o["subject"] == "Hub" for o in r["outliers"])


def test_reexports_from_kg_validation():
    from tools.kg_validation import (
        _NUMERIC_XSD as kv_numeric,
    )
    from tools.kg_validation import (
        _check_numeric_outliers,
        _check_relationship_outliers,
        _check_string_patterns,
        _check_value_ranges,
    )
    assert kv_numeric is _NUMERIC_XSD
    assert callable(_check_numeric_outliers)
    assert callable(_check_relationship_outliers)
    assert callable(_check_string_patterns)
    assert callable(_check_value_ranges)


def test_package_reexports():
    from tools.validation_support.checks import (
        check_numeric_outliers as a,
    )
    from tools.validation_support.checks import (
        check_relationship_outliers as b,
    )
    from tools.validation_support.checks import (
        check_string_patterns as c,
    )
    from tools.validation_support.checks import (
        check_value_ranges as d,
    )
    for fn in (a, b, c, d):
        assert callable(fn)
