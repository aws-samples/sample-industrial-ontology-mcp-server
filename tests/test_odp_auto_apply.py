"""Tests for ODP 자동 적용 확장 (작업코드 T4; docs/reference/task-glossary.md).

`_apply_odp_abstract_groups` 는 `rules/domain/abstract_group_hints.json` 의
abstract_class 정의를 그래프에 결정적으로 주입한다:
- abstract_class 없으면 생성 (+ label, scopeNote, autoLabel flag).
- child_name_patterns / child_examples 매칭 자식에 subClassOf 주입.
- 이미 명시적 부모가 있는 자식은 skip (기존 구조 존중).
- 자식이 여러 group 에 매칭되면 첫 group 만 적용 (first-group-wins).
"""
from __future__ import annotations

from unittest.mock import patch

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import _apply_odp_abstract_groups

STEEL_STR = str(DOMAIN_NS)


def _cls(local: str) -> URIRef:
    return URIRef(STEEL_STR + local)


def _graph_with(*locals_: str) -> Graph:
    g = Graph()
    for name in locals_:
        g.add((_cls(name), RDF.type, OWL.Class))
    return g


def test_no_hints_file_returns_zero_stats():
    """hints.json 이 비어있으면 stats 전부 0, 그래프 변화 없음."""
    g = _graph_with("ChemicalAnalysis")
    before = len(g)
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[],
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    assert stats["odp_groups_applied"] == 0
    assert stats["odp_groups_skipped"] == 0
    assert stats["odp_abstract_classes_created"] == 0
    assert stats["odp_subclass_links_added"] == 0
    assert stats["odp_children_matched"] == 0
    assert len(g) == before


def test_creates_abstract_class_when_missing():
    """abstract_class 가 그래프에 없으면 생성."""
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": ["Analysis"],
        "child_examples": ["ChemicalAnalysis"],
        "rationale": "품질 검사 그룹",
    }
    g = _graph_with("ChemicalAnalysis")
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    qm = _cls("QualityManagement")
    assert (qm, RDF.type, OWL.Class) in g
    assert stats["odp_abstract_classes_created"] == 1
    assert stats["odp_groups_applied"] == 1


def test_existing_abstract_class_not_recreated():
    """이미 있는 abstract_class 는 재생성 안 하고 subClassOf 만 추가."""
    g = _graph_with("QualityManagement", "ChemicalAnalysis")
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": [],
        "child_examples": ["ChemicalAnalysis"],
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    assert stats["odp_abstract_classes_created"] == 0
    assert stats["odp_subclass_links_added"] == 1
    qm = _cls("QualityManagement")
    assert (_cls("ChemicalAnalysis"), RDFS.subClassOf, qm) in g


def test_pattern_matching_adds_subclassof():
    """child_name_patterns 매칭 (substring) 으로 자식 탐지 + subClassOf 주입."""
    g = _graph_with("QualityManagement", "ChemicalAnalysis", "NDTResults")
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": ["Analysis", "NDT"],
        "child_examples": [],
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    qm = _cls("QualityManagement")
    assert (_cls("ChemicalAnalysis"), RDFS.subClassOf, qm) in g
    assert (_cls("NDTResults"), RDFS.subClassOf, qm) in g
    assert stats["odp_children_matched"] == 2


def test_explicit_parent_respected():
    """이미 명시적 rdfs:subClassOf 부모가 있는 자식은 건드리지 않음."""
    g = _graph_with("QualityManagement", "ChemicalAnalysis", "SomeOtherParent")
    g.add((_cls("ChemicalAnalysis"), RDFS.subClassOf, _cls("SomeOtherParent")))
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": [],
        "child_examples": ["ChemicalAnalysis"],
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    qm = _cls("QualityManagement")
    assert (_cls("ChemicalAnalysis"), RDFS.subClassOf, qm) not in g
    assert stats["odp_children_matched"] == 0


def test_first_group_wins():
    """자식이 여러 group 에 매칭되면 첫 group 만 적용 (deterministic)."""
    g = _graph_with("DualPatternClass")
    groups = [
        {
            "abstract_class": "GroupA",
            "child_name_patterns": ["Pattern"],
            "child_examples": [],
        },
        {
            "abstract_class": "GroupB",
            "child_name_patterns": ["DualPattern"],
            "child_examples": [],
        },
    ]
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=groups,
    ):
        _apply_odp_abstract_groups(g, STEEL_STR)
    a = _cls("GroupA")
    b = _cls("GroupB")
    assert (_cls("DualPatternClass"), RDFS.subClassOf, a) in g
    assert (_cls("DualPatternClass"), RDFS.subClassOf, b) not in g


def test_autocreated_flag_injected():
    """생성된 abstract class 는 steel-oc:autoCreated true 플래그 보유 (T4 전용)."""
    g = _graph_with("ChemicalAnalysis")
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": [],
        "child_examples": ["ChemicalAnalysis"],
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        _apply_odp_abstract_groups(g, STEEL_STR)
    oc_ns = STEEL_STR.rstrip("#") + "-ontoclean#"
    p_auto_created = URIRef(oc_ns + "autoCreated")
    assert Literal(True) in list(
        g.objects(_cls("QualityManagement"), p_auto_created)
    )


def test_autocreated_distinct_from_autolabel():
    """autoCreated (T4 provenance) 와 autoLabel (T2 OntoClean) 은 분리된 predicate.

    동일 `autoLabel` 을 공유하면 T2 가 manual 로 승격 시 제거해 T4 흔적이 사라진다.
    """
    g = _graph_with("ChemicalAnalysis")
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": [],
        "child_examples": ["ChemicalAnalysis"],
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        _apply_odp_abstract_groups(g, STEEL_STR)
    qm = _cls("QualityManagement")
    oc_ns = STEEL_STR.rstrip("#") + "-ontoclean#"
    # T4 가 `autoCreated` 를 썼고, `autoLabel` 은 건드리지 않음.
    assert Literal(True) in list(
        g.objects(qm, URIRef(oc_ns + "autoCreated"))
    )
    assert not list(g.objects(qm, URIRef(oc_ns + "autoLabel")))


def test_no_match_group_skipped():
    """자식 0명 매칭 → odp_groups_skipped++, abstract class 생성 안 함."""
    g = _graph_with("UnrelatedClass")
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": ["Analysis"],
        "child_examples": [],
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    assert stats["odp_groups_skipped"] == 1
    assert stats["odp_abstract_classes_created"] == 0
    assert stats["odp_groups_applied"] == 0
    qm = _cls("QualityManagement")
    assert (qm, RDF.type, OWL.Class) not in g


def test_metadata_on_created_abstract_class():
    """생성된 abstract class 에 rdfs:label (en) + skos:scopeNote (ko) 주입."""
    g = _graph_with("ChemicalAnalysis")
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": [],
        "child_examples": ["ChemicalAnalysis"],
        "rationale": "품질 검사 공통 부모",
    }
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        _apply_odp_abstract_groups(g, STEEL_STR)
    qm = _cls("QualityManagement")
    labels = list(g.objects(qm, RDFS.label))
    assert Literal("QualityManagement", lang="en") in labels
    scope_notes = list(g.objects(qm, SKOS.scopeNote))
    assert any("품질 검사 공통 부모" in str(n) for n in scope_notes)


def test_example_in_later_group_blocked_by_first_group_win():
    """첫 그룹이 pattern 으로 잡은 자식을 두 번째 그룹이 example 로 재배정 못 한다.

    리뷰 피드백(§2.1): pattern 매칭과 example 매칭의 first-group-wins 가
    동일하게 작동해야 한다.
    """
    g = _graph_with("SharedTarget")
    groups = [
        {
            "abstract_class": "PatternGroup",
            "child_name_patterns": ["Shared"],
            "child_examples": [],
        },
        {
            "abstract_class": "ExampleGroup",
            "child_name_patterns": [],
            "child_examples": ["SharedTarget"],
        },
    ]
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=groups,
    ):
        stats = _apply_odp_abstract_groups(g, STEEL_STR)
    pg = _cls("PatternGroup")
    eg = _cls("ExampleGroup")
    # PatternGroup 이 먼저 SharedTarget 을 흡수
    assert (_cls("SharedTarget"), RDFS.subClassOf, pg) in g
    # ExampleGroup 은 candidates 가 모두 `assigned` 필터로 제거돼 skipped
    assert (_cls("SharedTarget"), RDFS.subClassOf, eg) not in g
    assert stats["odp_groups_applied"] == 1
    assert stats["odp_groups_skipped"] == 1


def test_idempotent_on_second_run():
    """같은 그래프에 다시 호출해도 스키마가 바뀌지 않아야 한다."""
    group = {
        "abstract_class": "QualityManagement",
        "child_name_patterns": ["Analysis"],
        "child_examples": ["ChemicalAnalysis"],
    }
    g = _graph_with("ChemicalAnalysis", "AcidAnalysis")
    with patch(
        "tools.ontology_quality._load_abstract_group_hints",
        return_value=[group],
    ):
        stats1 = _apply_odp_abstract_groups(g, STEEL_STR)
        triples_after_first = len(g)
        stats2 = _apply_odp_abstract_groups(g, STEEL_STR)
    # 첫 호출: abstract 생성 + 자식 연결
    assert stats1["odp_abstract_classes_created"] == 1
    assert stats1["odp_subclass_links_added"] == 2
    # 두 번째 호출: 자식들이 이미 명시적 부모를 가져 candidates=0 → skipped
    assert stats2["odp_abstract_classes_created"] == 0
    assert stats2["odp_subclass_links_added"] == 0
    assert stats2["odp_groups_skipped"] == 1
    # 그래프 불변
    assert len(g) == triples_after_first
