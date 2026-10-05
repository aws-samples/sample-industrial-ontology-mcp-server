"""추론 노이즈 정리(6b/6c)가 이중 선언 프로퍼티를 전삭제하지 않는지 회귀 가드.

배경 (2026-08-08 실측): pattern 6b 는 ObjectProperty 의 literal 객체를 지우고,
6c 는 DatatypeProperty 의 non-literal 객체를 지운다. 각각은 타당하지만, 한
프로퍼티가 **두 타입 모두** 로 선언돼 있으면 두 패턴이 합작해 그 프로퍼티의
**모든 트리플** 이 사라진다:

    before: literal 1건 + IRI 1건
    after : 0건   (6b 가 literal, 6c 가 IRI 를 각각 제거)

이중 선언은 Architect/Jury 의 T-Box 오류이거나 ``owl:equivalentProperty`` 를 거친
OWL RL 폐쇄의 산물이지 **A-Box 데이터 결함이 아니다**. 그런데 손실이
``noise_pruning`` 항목으로 집계돼 품질 개선으로 기록됐고, ``dp_iri_object_removed``
에는 단언이 하나도 없었다 (기존 테스트는 pattern 1·2·3·4·6a·6b 만 덮었다).

수정: 두 패턴 모두 이중 선언 프로퍼티를 제외하고, 그 수를
``dual_typed_properties_preserved`` 로 보고하며 WARNING 을 남긴다 — 데이터를 지우는
대신 T-Box 를 고치게 한다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import OWL

from tools.inference import _prune_inference_noise

STEEL = "http://example.com/steel-ontology#"


def _p(name: str) -> URIRef:
    return URIRef(STEEL + name)


def _value_triples(g: Graph, prop: URIRef) -> int:
    return len(list(g.triples((None, prop, None))))


def test_dual_typed_property_keeps_all_its_triples():
    """THE REGRESSION: 이중 선언 프로퍼티의 트리플은 하나도 지우지 않는다."""
    g = Graph()
    prop = _p("ambiguousProp")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((URIRef(STEEL + "X1"), prop, Literal("value")))
    g.add((URIRef(STEEL + "X2"), prop, URIRef(STEEL + "Target")))

    stats = _prune_inference_noise(g)

    assert _value_triples(g, prop) == 2, (
        "6b+6c 합작으로 프로퍼티의 모든 값이 사라졌다 — T-Box 선언 오류가 "
        "데이터 손실로 번역됐다"
    )
    assert stats["dual_typed_properties_preserved"] == 1, (
        "보존 사실이 보고되지 않으면 T-Box 를 고칠 신호가 없다"
    )


def test_dual_typed_count_excluded_from_total_pruned():
    """보존 카운터가 제거 합계(total_pruned)에 섞이지 않는다."""
    g = Graph()
    prop = _p("ambiguousProp")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((URIRef(STEEL + "X1"), prop, Literal("v")))

    stats = _prune_inference_noise(g)
    assert stats["dual_typed_properties_preserved"] == 1
    assert stats["total_pruned"] == 0, (
        f"보존 항목이 제거 합계에 계산됐다: total_pruned={stats['total_pruned']}"
    )


def test_pure_datatype_property_still_loses_iri_object():
    """POSITIVE: 순수 DP 의 IRI 객체는 여전히 제거된다 (6c 기능 보존).

    실측 사례: 추론기가 provenance IRI 를 xsd:string range DP 에 접붙였다.
    """
    g = Graph()
    prop = _p("pureDp")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((URIRef(STEEL + "X1"), prop, Literal("ok")))
    g.add((URIRef(STEEL + "X2"), prop, URIRef("prov://row/1")))

    stats = _prune_inference_noise(g)

    assert _value_triples(g, prop) == 1
    assert stats["dp_iri_object_removed"] == 1
    assert stats["dual_typed_properties_preserved"] == 0


def test_pure_object_property_still_loses_literal_object():
    """POSITIVE: 순수 OP 의 literal 객체는 여전히 제거된다 (6b 기능 보존)."""
    g = Graph()
    prop = _p("pureOp")
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((URIRef(STEEL + "X1"), prop, URIRef(STEEL + "T")))
    g.add((URIRef(STEEL + "X2"), prop, Literal("1210.48")))

    stats = _prune_inference_noise(g)

    assert _value_triples(g, prop) == 1
    assert stats["op_literal_object_removed"] == 1


@pytest.mark.parametrize("foreign_ns", ["http://elsewhere.org/x#"])
def test_foreign_namespace_property_untouched(foreign_ns):
    """남의 네임스페이스 프로퍼티는 정리 대상이 아니다."""
    g = Graph()
    prop = URIRef(foreign_ns + "someProp")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((URIRef(foreign_ns + "X"), prop, URIRef(foreign_ns + "Y")))

    _prune_inference_noise(g)
    assert _value_triples(g, prop) == 1
