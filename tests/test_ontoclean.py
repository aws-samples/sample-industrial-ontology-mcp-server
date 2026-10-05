"""Tests for tools/ontoclean.py — B."""
from __future__ import annotations

import json

from rdflib import Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.ontoclean import (
    OC_IDENTITY,
    OC_RIGIDITY,
    OC_UNITY,
    analyze_ontoclean,
    check_identity_constraint,
    check_rigidity_constraint,
    check_unity_constraint,
)


def _cls(name: str) -> URIRef:
    return URIRef(f"{DOMAIN_NS}{name}")


def _tbox_with(meta: list[tuple[str, str, str]], pairs: list[tuple[str, str]]):
    """meta: (class, metaproperty, value); pairs: (sub, parent)."""
    g = _new_graph()
    classes = set()
    for name, _, _ in meta:
        classes.add(name)
    for s, p in pairs:
        classes.add(s)
        classes.add(p)
    for name in classes:
        g.add((_cls(name), RDF.type, OWL.Class))
    prop_map = {"rigidity": OC_RIGIDITY, "unity": OC_UNITY, "identity": OC_IDENTITY}
    for name, mp, val in meta:
        g.add((_cls(name), prop_map[mp], Literal(val)))
    for sub, par in pairs:
        g.add((_cls(sub), RDFS.subClassOf, _cls(par)))
    return g


def test_rigidity_violation_detected():
    pairs = [("Passenger", "Person")]
    meta = {
        "Person": {"rigidity": "-R"},
        "Passenger": {"rigidity": "+R"},
    }
    violations = check_rigidity_constraint(meta, pairs)
    # C1: +R can't be subclass of -R
    assert len(violations) == 1
    assert violations[0]["rule"] == "C1_rigidity"
    assert violations[0]["severity"] == "critical"


def test_rigidity_no_violation_when_both_rigid():
    pairs = [("A", "B")]
    meta = {"A": {"rigidity": "+R"}, "B": {"rigidity": "+R"}}
    assert check_rigidity_constraint(meta, pairs) == []


def test_unity_violation():
    pairs = [("Body", "Amount")]
    meta = {"Body": {"unity": "+U"}, "Amount": {"unity": "-U"}}
    violations = check_unity_constraint(meta, pairs)
    assert len(violations) == 1
    assert violations[0]["rule"] == "C2_unity"


def test_identity_violation():
    pairs = [("Dog", "Entity")]
    meta = {"Dog": {"identity": "+I"}, "Entity": {"identity": "-I"}}
    violations = check_identity_constraint(meta, pairs)
    assert len(violations) == 1
    assert violations[0]["rule"] == "C3_identity"


def test_no_violation_when_unlabeled():
    """Annotation 없는 쌍은 위반으로 카운트되지 않음."""
    pairs = [("X", "Y")]
    meta = {}
    assert check_rigidity_constraint(meta, pairs) == []
    assert check_unity_constraint(meta, pairs) == []
    assert check_identity_constraint(meta, pairs) == []


def test_analyze_ontoclean_end_to_end(tmp_path):
    g = _tbox_with(
        meta=[
            ("Person", "rigidity", "-R"),
            ("Passenger", "rigidity", "+R"),
        ],
        pairs=[("Passenger", "Person"), ("Student", "Person")],
    )
    path = tmp_path / "tbox.ttl"
    g.serialize(destination=str(path), format="turtle")
    report = analyze_ontoclean(str(path))
    assert report["violations_total"] >= 1
    assert report["classes_labeled"] == 2
    # Student는 unlabeled
    assert "Student" in report["unlabeled_sample"]


def test_analyze_ontoclean_no_meta(tmp_path):
    g = _new_graph()
    g.add((_cls("A"), RDF.type, OWL.Class))
    path = tmp_path / "t.ttl"
    g.serialize(destination=str(path), format="turtle")
    r = analyze_ontoclean(str(path))
    assert r["violations_total"] == 0
    assert r["classes_labeled"] == 0
    assert r["labeling_coverage_pct"] == 0.0


def test_mcp_tool(tmp_path):
    from tools.ontoclean import validate_ontoclean
    g = _new_graph()
    g.add((_cls("A"), RDF.type, OWL.Class))
    path = tmp_path / "t.ttl"
    g.serialize(destination=str(path), format="turtle")
    raw = validate_ontoclean(str(path))
    data = json.loads(raw)
    assert data["success"] is True


def test_mcp_tool_missing_tbox():
    """없는 T-Box 는 **실패**로 보고돼야 한다 (성공으로 감싸지 않는다).

    이 테스트는 원래 ``success is True`` 를 주장했다 — ``analyze_ontoclean`` 이
    ``{"error": ...}`` 를 돌려주는데 그것을 ``success_response`` 로 감싸는 동작을
    고정한 것이다. 그 동작이 배포 트리에
    ``{"error": "T-Box not found: /nonexistent/t.ttl"}`` 를 **보고서로** 남겼다
    (실측 2026-09-02). 같은 자리에서 ``foops_fair`` 는 이미 error_response 로
    내린다. 손으로 적은 기대값이 결함을 지키고 있었으므로 기대값을 바꾼다.
    """
    from tools.ontoclean import validate_ontoclean
    raw = validate_ontoclean("/nonexistent/t.ttl")
    data = json.loads(raw)
    assert data["success"] is False
    assert "error" in data


def test_missing_tbox_does_not_persist_a_report(tmp_path, monkeypatch):
    """실패한 분석이 배포 보고서를 덮어쓰지 않는다 (오염 경로 차단)."""
    import tools.ontoclean as oc
    reports = tmp_path / "reports"
    reports.mkdir()
    target = reports / "ontoclean_report.json"
    target.write_text('{"keep": true}', encoding="utf-8")
    monkeypatch.setattr(oc, "GENERATED_REPORTS_DIR", str(reports))

    oc.validate_ontoclean("/nonexistent/t.ttl")
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}
