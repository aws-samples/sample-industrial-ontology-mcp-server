"""OWL/RDF 메타 상수 — 모듈 간 공유 (P1-4 중앙화).

여러 모듈 (tools/remote/neo4j.py, tools/kg_validation.py, tools/ontology_quality.py
등) 에서 비슷한 OWL axiom predicate 집합 / 메타 타입 set 을 각자 정의하던
패턴을 단일 모듈로 모아 '도메인 추가/제거 시 한 곳만 수정' 으로 정리.

- ``OWL_AXIOM_PREDICATES``: LPG 변환·SPARQL 분포 비교 등에서 인스턴스/관계
  데이터로 취급하지 **않을** OWL/RDF axiom predicate. inverseOf, unionOf,
  cardinality 류, RDF list 구조 (first/rest) 포함.
- ``OWL_META_TYPES``: ``rdf:type`` 의 object 가 OWL 메타 클래스 일 때
  인스턴스 카운트에서 제외해야 하는 타입 (owl:Class, owl:ObjectProperty 등).
  ``owl:Thing``, ``rdfs:Class``, ``rdfs:Resource`` 도 포함.
- ``UPPER_ONTOLOGY_NS_PREFIXES``: BFO 등 상위 온톨로지 네임스페이스. 도메인
  분포 측정에서 prefix 기반으로 제외할 때 사용.
- ``is_upper_ontology_type(uri)``: prefix 매칭 헬퍼.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS

# ── LPG 변환 / SPARQL 분포 비교에서 제외할 axiom predicate ──
# 추가 시: 새 OWL/RDF 구조 predicate (예: owl:onDataRange) 도 여기에.
OWL_AXIOM_PREDICATES: frozenset[str] = frozenset({
    str(OWL.inverseOf), str(OWL.equivalentClass), str(OWL.equivalentProperty),
    str(OWL.disjointWith), str(OWL.complementOf),
    str(OWL.unionOf), str(OWL.intersectionOf),
    str(OWL.someValuesFrom), str(OWL.allValuesFrom), str(OWL.hasValue),
    str(OWL.onProperty), str(OWL.onClass),
    str(OWL.maxCardinality), str(OWL.minCardinality), str(OWL.cardinality),
    str(OWL.maxQualifiedCardinality), str(OWL.minQualifiedCardinality),
    str(OWL.imports), str(OWL.versionIRI), str(OWL.distinctMembers),
    str(OWL.members), str(OWL.propertyChainAxiom),
    str(RDF.type), str(RDF.first), str(RDF.rest),
    str(RDFS.domain), str(RDFS.range),
    str(RDFS.subClassOf),  # downstream 별도 처리
    str(OWL.sameAs), str(OWL.differentFrom),
    # OWL annotation reification
    str(OWL.annotatedSource), str(OWL.annotatedProperty), str(OWL.annotatedTarget),
})


# ── rdf:type object 가 OWL 메타 클래스 일 때 인스턴스 분포에서 제외 ──
OWL_META_TYPES: frozenset[str] = frozenset({
    str(OWL.Class), str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
    str(OWL.FunctionalProperty), str(OWL.TransitiveProperty),
    str(OWL.SymmetricProperty), str(OWL.InverseFunctionalProperty),
    str(OWL.Restriction), str(OWL.Ontology), str(OWL.AnnotationProperty),
    str(OWL.AllDisjointClasses), str(OWL.NamedIndividual),
    str(OWL.Thing),
    str(RDFS.Class),
    str(RDFS.Resource),
})


# ── 상위 온톨로지 네임스페이스 prefix ──
# 추가 시: 새 upper ontology (DOLCE 등) 도 여기에.
UPPER_ONTOLOGY_NS_PREFIXES: tuple[str, ...] = (
    "http://purl.obolibrary.org/obo/BFO_",
)


def is_upper_ontology_type(type_uri: str) -> bool:
    """주어진 type URI 가 BFO 등 상위 온톨로지에 속하는지 prefix 매칭."""
    return any(type_uri.startswith(pfx) for pfx in UPPER_ONTOLOGY_NS_PREFIXES)


__all__ = [
    "OWL_AXIOM_PREDICATES",
    "OWL_META_TYPES",
    "UPPER_ONTOLOGY_NS_PREFIXES",
    "is_upper_ontology_type",
]
