"""추상 상위 클래스를 품질 지표 분모에서 제외하는 규칙의 회귀 가드.

배경 (2026-07-25 실측): 파이프라인은 스스로 추상 계층을 만들라고 요구한다
(``rules/domain/design_patterns.json`` 의 domain_hierarchy, ``step_12`` 중간 추상 클래스
생성, ``step_24`` abstract_category 주석). 그런데 두 품질 체크가 그 클래스에
CSV 테이블·인스턴스가 없다고 감점했다:

  - **T-Box Fitness** 2.9% FAIL — 34개 중 14개가 추상인데 분모에 포함
  - **클래스별 인스턴스 수** 52.9% FAIL — ``truly_orphan`` 12개가 전부 추상
    클래스였다 (실제 갭 0)

즉 지표가 구조적으로 달성 불가능한 상태였다. 두 체크가 같은 판정
(``find_abstract_parent_classes``) 을 공유하도록 통일했다.

판정 규칙: **자식 클래스가 있고 자기 DatatypeProperty 가 없으면 추상.**
자기 DP 가 있으면 자식이 있어도 실체다 (``MaterialA`` 은 ``MaterialB`` 을 자식으로
두지만 자기 컬럼 184개를 갖는다).
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, URIRef

from domain.tbox_utils import _new_graph
from tools.validation_support.checks.structural import check_class_instance_count
from tools.validation_support.common import find_abstract_parent_classes

STEEL = "http://example.com/steel-ontology#"


def _cls(g, name: str) -> URIRef:
    uri = URIRef(STEEL + name)
    g.add((uri, RDF.type, OWL.Class))
    return uri


def _dp(g, name: str, domain: URIRef) -> None:
    uri = URIRef(STEEL + name)
    g.add((uri, RDF.type, OWL.DatatypeProperty))
    g.add((uri, RDFS.domain, domain))


# ── 판정 헬퍼 ─────────────────────────────────────────────────────────


def test_parent_without_own_dp_is_abstract():
    """자식만 있고 자기 DP 가 없으면 추상으로 판정한다."""
    g = _new_graph()
    parent = _cls(g, "MasterData")
    child = _cls(g, "Order")
    g.add((child, RDFS.subClassOf, parent))
    _dp(g, "idCol1", child)      # 자식만 DP 보유

    assert find_abstract_parent_classes(g, ns=STEEL) == {"MasterData"}


def test_parent_with_own_dp_is_not_abstract():
    """자식이 있어도 자기 DP 를 가지면 실체 클래스다 (MaterialA ⊃ MaterialB 케이스)."""
    g = _new_graph()
    parent = _cls(g, "MaterialA")
    child = _cls(g, "MaterialB")
    g.add((child, RDFS.subClassOf, parent))
    _dp(g, "materialAWeight", parent)   # 부모도 자기 컬럼 보유

    assert find_abstract_parent_classes(g, ns=STEEL) == set()


def test_leaf_class_is_never_abstract():
    """자식이 없으면 추상이 아니다 (차원 클래스 등은 실제 갭으로 남아야 함)."""
    g = _new_graph()
    _cls(g, "SteelGradeGroup")

    assert find_abstract_parent_classes(g, ns=STEEL) == set()


def test_candidates_narrows_scope():
    """candidates 로 검사 범위를 좁힐 수 있다."""
    g = _new_graph()
    for parent_name, child_name in (("AbsOne", "KidOne"), ("AbsTwo", "KidTwo")):
        parent = _cls(g, parent_name)
        child = _cls(g, child_name)
        g.add((child, RDFS.subClassOf, parent))

    assert find_abstract_parent_classes(g, ns=STEEL) == {"AbsOne", "AbsTwo"}
    assert find_abstract_parent_classes(
        g, candidates={"AbsOne"}, ns=STEEL,
    ) == {"AbsOne"}


# ── 클래스별 인스턴스 수 체크 ─────────────────────────────────────────


def test_instance_count_excludes_abstract_parents():
    """추상 클래스가 인스턴스 없다고 FAIL 되지 않는다."""
    tbox = _new_graph()
    abstract = _cls(tbox, "MasterData")
    concrete = _cls(tbox, "Order")
    tbox.add((concrete, RDFS.subClassOf, abstract))
    _dp(tbox, "idCol1", concrete)

    instances = _new_graph()
    instances.add((URIRef(STEEL + "order1"), RDF.type, concrete))

    result = check_class_instance_count(instances, tbox=tbox)
    assert "MasterData" in result["abstract_parents_excluded"]
    assert result["no_instance_truly_orphan"] == []
    assert result["no_instance_ratio"] == 0.0
    assert result["passed"] is True


def test_instance_count_still_flags_real_gap():
    """실체 클래스가 비면 여전히 갭으로 잡는다 (오탐 수정이 과탐지 억제로 가지 않음)."""
    tbox = _new_graph()
    filled = _cls(tbox, "Order")
    empty = _cls(tbox, "Shipment")
    _dp(tbox, "idCol1", filled)
    _dp(tbox, "shipmentId", empty)     # 둘 다 실체(자식 없음, DP 보유)

    instances = _new_graph()
    instances.add((URIRef(STEEL + "order1"), RDF.type, filled))

    result = check_class_instance_count(instances, tbox=tbox)
    assert result["abstract_parents_excluded"] == []
    assert result["no_instance_truly_orphan"] == ["Shipment"]
    assert result["no_instance_ratio"] == 50.0
    assert result["passed"] is False


def test_instance_count_excludes_restriction_derivable():
    """restriction 보유 클래스(추론으로 분류 가능)는 추론 전 비어도 정상."""
    tbox = _new_graph()
    filled = _cls(tbox, "Order")
    derivable = _cls(tbox, "PriorityOrder")
    _dp(tbox, "idCol1", filled)
    restriction = URIRef(STEEL + "PriorityOrder_restriction")
    tbox.add((restriction, RDF.type, OWL.Restriction))
    tbox.add((restriction, OWL.someValuesFrom, filled))
    tbox.add((derivable, RDFS.subClassOf, restriction))

    instances = _new_graph()
    instances.add((URIRef(STEEL + "order1"), RDF.type, filled))

    result = check_class_instance_count(instances, tbox=tbox)
    assert "PriorityOrder" in result["no_instance_derivable"]
    assert result["no_instance_truly_orphan"] == []
    assert result["passed"] is True
