"""Tests for T2 — OntoClean 자동 annotation 확장.

`_heuristic_ontoclean_labels` 는 이름 suffix + owl:hasKey 기반으로
rigidity / unity / identity / dependence 라벨을 자동 생성한다.
`_inject_ontoclean_annotations` 은 labels.json 수동 entry 우선으로
auto fallback 을 병합해 새 도메인 클래스도 기본 라벨을 갖게 한다.
"""
from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF

from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import (
    _heuristic_ontoclean_labels,
    _inject_ontoclean_annotations,
)

STEEL_STR = str(DOMAIN_NS)


def _cls(local: str) -> URIRef:
    return URIRef(STEEL_STR + local)


def _graph_with(*locals_: str) -> Graph:
    g = Graph()
    for name in locals_:
        g.add((_cls(name), RDF.type, OWL.Class))
    return g


def test_status_suffix_gets_anti_rigid():
    g = _graph_with("EquipmentStatus")
    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    assert "EquipmentStatus" in auto
    assert auto["EquipmentStatus"]["rigidity"] == "-R"


def test_master_suffix_strong_identity():
    g = _graph_with("EquipmentMaster")
    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    meta = auto["EquipmentMaster"]
    assert meta["identity"] == "+I"
    assert meta["rigidity"] == "+R"
    assert meta["dependence"] == "-D"


def test_event_suffix_dependent():
    g = _graph_with("AlarmEvent")
    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    meta = auto["AlarmEvent"]
    assert meta["dependence"] == "+D"
    assert meta["identity"] == "+I"


def test_manual_takes_priority():
    g = _graph_with("EquipmentMaster")
    auto = _heuristic_ontoclean_labels(
        g, STEEL_STR, manual={"EquipmentMaster"}
    )
    # 수동 등록된 클래스는 auto dict 에서 제외된다.
    assert "EquipmentMaster" not in auto


def test_external_namespace_skipped():
    """FOAF 같은 외부 네임스페이스 클래스는 도메인 prefix 밖이라 휴리스틱 대상 아님."""
    g = Graph()
    foaf_person = URIRef("http://xmlns.com/foaf/0.1/Person")
    g.add((foaf_person, RDF.type, OWL.Class))
    # 도메인 네임스페이스 클래스도 함께 넣어 one-match 확인.
    g.add((_cls("EquipmentMaster"), RDF.type, OWL.Class))

    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    # 외부 클래스는 local name 으로도 키가 없어야 함.
    assert "Person" not in auto
    assert "EquipmentMaster" in auto


def test_owl_haskey_forces_identity():
    """owl:hasKey 가 있으면 suffix 와 무관하게 identity=+I."""
    g = _graph_with("CustomThing")
    # CustomThing 은 suffix 휴리스틱상 identity=-I 가 기본이어야 하지만,
    # owl:hasKey 선언이 있으면 +I 로 승격.
    g.add((_cls("CustomThing"), OWL.hasKey, URIRef(STEEL_STR + "someProperty")))
    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    assert auto["CustomThing"]["identity"] == "+I"


def test_collection_suffix_aggregate():
    g = _graph_with("AlertGroup")
    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    assert auto["AlertGroup"]["unity"] == "-U"


def test_integration_inject_produces_auto_triples():
    """labels.json 에 없는 신규 클래스도 auto fallback 으로 annotation 된다."""
    g = Graph()
    # labels.json 에 없을 법한 신규 suffix-Status 클래스 (manual 에 없음).
    new_cls = "NewEquipmentStatus"
    g.add((_cls(new_cls), RDF.type, OWL.Class))

    stats = _inject_ontoclean_annotations(g, STEEL_STR)

    assert stats.get("ontoclean_classes_auto_labeled", 0) >= 1

    # rigidity=-R triple 이 실제로 주입됐는지 확인.
    oc_ns = STEEL_STR.rstrip("#") + "-ontoclean#"
    p_rigidity = URIRef(oc_ns + "rigidity")
    objs = list(g.objects(_cls(new_cls), p_rigidity))
    assert Literal("-R") in objs


def test_status_suffix_with_haskey_combines_rules():
    """Status suffix 이면서 owl:hasKey 가 있으면 rigidity=-R, identity=+I 동시 성립."""
    g = _graph_with("SpecialStatus")
    g.add((_cls("SpecialStatus"), OWL.hasKey, URIRef(STEEL_STR + "statusId")))
    auto = _heuristic_ontoclean_labels(g, STEEL_STR, manual=set())
    meta = auto["SpecialStatus"]
    assert meta["rigidity"] == "-R"
    assert meta["identity"] == "+I"  # hasKey 가 강제
    assert meta["dependence"] == "+D"  # Status 는 dependent suffix 에도 포함


def test_inject_flags_auto_labeled_classes():
    """휴리스틱으로 라벨링된 클래스는 steel-oc:autoLabel true 플래그가 주입된다."""
    g = _graph_with("RareMonitoring")
    _inject_ontoclean_annotations(g, STEEL_STR)
    oc_ns = STEEL_STR.rstrip("#") + "-ontoclean#"
    p_auto = URIRef(oc_ns + "autoLabel")
    objs = list(g.objects(_cls("RareMonitoring"), p_auto))
    assert Literal(True) in objs


def test_inject_flag_absent_for_manual_label():
    """labels.json 에 등록된 클래스는 autoLabel 플래그가 붙지 않는다."""
    # labels.json 에 존재하는 EquipmentMaster 로 테스트.
    g = _graph_with("EquipmentMaster")
    _inject_ontoclean_annotations(g, STEEL_STR)
    oc_ns = STEEL_STR.rstrip("#") + "-ontoclean#"
    p_auto = URIRef(oc_ns + "autoLabel")
    objs = list(g.objects(_cls("EquipmentMaster"), p_auto))
    assert Literal(True) not in objs


def test_analyze_ontoclean_separates_manual_and_auto(tmp_path):
    """analyze_ontoclean 이 manual vs auto coverage 를 분리 보고한다."""
    from tools.ontoclean import analyze_ontoclean

    g = Graph()
    # Manual-labeled (labels.json 에 존재)
    g.add((_cls("EquipmentMaster"), RDF.type, OWL.Class))
    # Auto-labeled (신규 suffix)
    g.add((_cls("NovelMonitoring"), RDF.type, OWL.Class))
    _inject_ontoclean_annotations(g, STEEL_STR)

    tbox_path = tmp_path / "tbox.ttl"
    tbox_path.write_text(g.serialize(format="turtle"))
    report = analyze_ontoclean(str(tbox_path))

    assert report["classes_manual_labeled"] >= 1
    assert report["classes_auto_labeled"] >= 1
    assert report["manual_coverage_pct"] > 0
    assert report["auto_coverage_pct"] > 0
