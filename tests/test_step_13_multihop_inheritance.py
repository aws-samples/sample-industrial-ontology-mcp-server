"""step_13 restriction 호환성 판정이 **전이적 상속** 을 인정하는지 회귀 가드.

배경 (2026-08-08 실측): ``_strip_incompatible_restrictions`` 는 호환성을 한 홉으로만
판정했다 (``(cls, RDFS.subClassOf, d) in g``). OWL 의미론상 두 단계 아래 클래스도
프로퍼티를 상속하는데, 그것을 "호환 불가" 로 보고 **정상 restriction 의 subClassOf
링크를 지웠다**:

    Pump ⊂ RotatingEquipment ⊂ Equipment,  OP domain = Equipment
    1홉(Pump ⊂ Equipment) -> 보존
    2홉                    -> 삭제   ← 잘못

파이프라인이 스스로 이 상황을 만든다: step_09e / step_12 가 중간 추상 클래스를
삽입하면 1홉이 2홉으로 바뀐다. 모듈에 직접 테스트가 0건이었다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, RDFS, BNode, Graph, URIRef
from rdflib.namespace import OWL

from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
    _strip_incompatible_restrictions,
)

STEEL = "http://example.com/steel-ontology#"


def _c(name: str) -> URIRef:
    return URIRef(STEEL + name)


def _build(*, hops: int, op_domain: str) -> tuple[Graph, BNode]:
    """``Pump`` 에 restriction 을 걸고 상속 깊이를 ``hops`` 로 만든다."""
    g = Graph()
    for cls in ("Equipment", "RotatingEquipment", "Pump", "Order"):
        g.add((_c(cls), RDF.type, OWL.Class))
    g.add((_c("Pump"), RDFS.subClassOf, _c("RotatingEquipment")))
    if hops >= 2:
        g.add((_c("RotatingEquipment"), RDFS.subClassOf, _c("Equipment")))
    elif hops == 1:
        g.add((_c("Pump"), RDFS.subClassOf, _c("Equipment")))

    op = _c("hasPart")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, _c(op_domain)))

    restriction = BNode()
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, op))
    g.add((restriction, OWL.someValuesFrom, _c("Equipment")))
    g.add((_c("Pump"), RDFS.subClassOf, restriction))
    return g, restriction


def test_direct_subclass_restriction_is_kept():
    """1홉 상속은 기존에도 보존됐다 (대조군)."""
    g, restriction = _build(hops=1, op_domain="Equipment")
    result = _strip_incompatible_restrictions(g, STEEL)
    assert result["incompatible_restrictions_removed"] == 0
    assert (_c("Pump"), RDFS.subClassOf, restriction) in g


def test_grandchild_restriction_is_kept():
    """THE REGRESSION: 2홉 상속도 보존해야 한다.

    OWL 의미론상 손자 클래스도 조상의 프로퍼티를 상속한다.
    """
    g, restriction = _build(hops=2, op_domain="Equipment")
    result = _strip_incompatible_restrictions(g, STEEL)
    assert result["incompatible_restrictions_removed"] == 0, (
        "다중 홉 상속을 호환 불가로 판정해 정상 restriction 을 지웠다"
    )
    assert (_c("Pump"), RDFS.subClassOf, restriction) in g


@pytest.mark.parametrize("hops", [3, 4])
def test_deeper_hierarchies_are_kept(hops):
    """3~4홉 계층도 보존된다 (파이프라인이 중간 클래스를 더 넣을 수 있다)."""
    g = Graph()
    chain = ["Equipment", "L1", "L2", "L3", "Pump"][: hops + 1]
    for cls in chain:
        g.add((_c(cls), RDF.type, OWL.Class))
    for child, parent in zip(chain[1:], chain[:-1]):
        g.add((_c(child), RDFS.subClassOf, _c(parent)))

    op = _c("hasPart")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, _c("Equipment")))
    restriction = BNode()
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, op))
    g.add((_c(chain[-1]), RDFS.subClassOf, restriction))

    result = _strip_incompatible_restrictions(g, STEEL)
    assert result["incompatible_restrictions_removed"] == 0
    assert (_c(chain[-1]), RDFS.subClassOf, restriction) in g


def test_genuinely_unrelated_domain_is_still_stripped():
    """POSITIVE: 상속 관계가 전혀 없는 domain 의 restriction 은 제거한다 (기능 보존)."""
    g, restriction = _build(hops=2, op_domain="Order")
    result = _strip_incompatible_restrictions(g, STEEL)
    assert result["incompatible_restrictions_removed"] == 1, (
        "무관한 domain 의 restriction 이 남았다 — 가드가 과하게 넓다"
    )
    assert (_c("Pump"), RDFS.subClassOf, restriction) not in g


def test_reverse_direction_ancestor_is_compatible():
    """OP domain 이 클래스의 **자손** 인 경우도 호환으로 본다 (기존 동작 유지)."""
    g = Graph()
    for cls in ("Equipment", "Pump"):
        g.add((_c(cls), RDF.type, OWL.Class))
    g.add((_c("Pump"), RDFS.subClassOf, _c("Equipment")))
    op = _c("hasPart")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, _c("Pump")))          # domain 이 더 좁다
    restriction = BNode()
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, op))
    g.add((_c("Equipment"), RDFS.subClassOf, restriction))

    result = _strip_incompatible_restrictions(g, STEEL)
    assert result["incompatible_restrictions_removed"] == 0
