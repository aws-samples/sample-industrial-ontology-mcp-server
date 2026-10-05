"""R25 regression — subPropertyOf cycle breaker.

Multi-Agent 가 의미 동일한 두 OP 를 duplicate 로 만들면 양방향
subPropertyOf 로 연결돼 critical `sub_property_cycle` 이슈가 생긴다.
`_break_sub_property_cycles` 가 Tarjan SCC 로 순환을 찾아 edge 를
제거해야 한다.
"""
from __future__ import annotations

from rdflib import RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import (
    _break_sub_class_cycles,
    _break_sub_property_cycles,
    _remove_cross_domain_subproperties,
)


def _g(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def test_bidirectional_subproperty_cycle_is_broken():
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:opA a owl:ObjectProperty ;
        rdfs:subPropertyOf steel:opB .
    steel:opB a owl:ObjectProperty ;
        rdfs:subPropertyOf steel:opA .
    """
    g = _g(ttl)
    stats = _break_sub_property_cycles(g, str(DOMAIN_NS))
    assert stats["sub_property_cycles_broken"] >= 1
    # cycle 제거 후 더 이상 양방향이 아니어야 함
    a_parents = set(g.objects(URIRef(f"{DOMAIN_NS}opA"), RDFS.subPropertyOf))
    b_parents = set(g.objects(URIRef(f"{DOMAIN_NS}opB"), RDFS.subPropertyOf))
    # 적어도 한쪽은 비어있어야 cycle 해제
    assert len(a_parents & b_parents) == 0 or not (a_parents and b_parents)


def test_three_way_cycle_is_broken():
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:opA a owl:ObjectProperty ; rdfs:subPropertyOf steel:opB .
    steel:opB a owl:ObjectProperty ; rdfs:subPropertyOf steel:opC .
    steel:opC a owl:ObjectProperty ; rdfs:subPropertyOf steel:opA .
    """
    g = _g(ttl)
    stats = _break_sub_property_cycles(g, str(DOMAIN_NS))
    assert stats["sub_property_cycles_broken"] >= 1


# ── step 22c — subClassOf cycle breaker ──────────────────────────


def test_subclass_bidirectional_cycle_is_broken():
    """SlabDesignSpec ↔ SlabDesignSpecification 처럼 두 클래스가 서로의
    부모로 선언된 subClassOf 순환을 끊어야 한다 (나머지 계층은 보존)."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:SlabDesignSpec a owl:Class ;
        rdfs:subClassOf steel:SlabDesignSpecification .
    steel:SlabDesignSpecification a owl:Class ;
        rdfs:subClassOf steel:SlabDesignSpec ;
        rdfs:subClassOf steel:DesignSpecification .
    steel:DesignSpecification a owl:Class .
    """
    g = _g(ttl)
    stats = _break_sub_class_cycles(g, str(DOMAIN_NS))
    assert stats["sub_class_cycles_broken"] >= 1
    # cycle 제거 후 양방향이 남지 않아야 함
    a = URIRef(f"{DOMAIN_NS}SlabDesignSpec")
    b = URIRef(f"{DOMAIN_NS}SlabDesignSpecification")
    assert not (
        (a, RDFS.subClassOf, b) in g and (b, RDFS.subClassOf, a) in g
    )
    # 정상 계층 (→ DesignSpecification) 은 보존
    assert (b, RDFS.subClassOf, URIRef(f"{DOMAIN_NS}DesignSpecification")) in g


def test_subclass_three_way_cycle_is_broken():
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:A a owl:Class ; rdfs:subClassOf steel:B .
    steel:B a owl:Class ; rdfs:subClassOf steel:C .
    steel:C a owl:Class ; rdfs:subClassOf steel:A .
    """
    g = _g(ttl)
    stats = _break_sub_class_cycles(g, str(DOMAIN_NS))
    assert stats["sub_class_cycles_broken"] >= 1


def test_subclass_linear_hierarchy_preserved():
    """순환이 아닌 정상 subClassOf 계층은 보존되어야 한다."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:Pump a owl:Class ; rdfs:subClassOf steel:Equipment .
    steel:Equipment a owl:Class ; rdfs:subClassOf steel:Asset .
    steel:Asset a owl:Class .
    """
    g = _g(ttl)
    stats = _break_sub_class_cycles(g, str(DOMAIN_NS))
    assert stats["sub_class_cycles_broken"] == 0
    assert (
        URIRef(f"{DOMAIN_NS}Pump"),
        RDFS.subClassOf,
        URIRef(f"{DOMAIN_NS}Equipment"),
    ) in g
    assert (
        URIRef(f"{DOMAIN_NS}Equipment"),
        RDFS.subClassOf,
        URIRef(f"{DOMAIN_NS}Asset"),
    ) in g


def test_subclass_self_loop_is_removed():
    """자기 자신을 부모로 가리키는 self-loop (X subClassOf X) 를 제거해야 한다.

    add_class_hierarchy / someValuesFrom restriction 생성이 네임스페이스를
    혼동해 self-loop 을 만드는 케이스. self-loop 은 adjacency 에서 제외되어
    SCC(size ≥ 2)로는 안 잡히므로 SCC 분석 전에 직접 제거된다."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:MaterialArtifact a owl:Class ;
        rdfs:subClassOf steel:MaterialArtifact ;
        rdfs:subClassOf steel:Artifact .
    steel:Artifact a owl:Class .
    """
    g = _g(ttl)
    stats = _break_sub_class_cycles(g, str(DOMAIN_NS))
    assert stats["sub_class_self_loops_removed"] == 1
    node = URIRef(f"{DOMAIN_NS}MaterialArtifact")
    assert (node, RDFS.subClassOf, node) not in g
    # 정상 계층 (→ Artifact) 은 보존
    assert (node, RDFS.subClassOf, URIRef(f"{DOMAIN_NS}Artifact")) in g


# ── step 23c — cross-domain subPropertyOf cleanup ────────────────


def test_cross_domain_subproperty_is_removed():
    """domain 이 disjoint 한 property 간 subPropertyOf 는 제거되어야 한다."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:reheatingFurnaceOpCreatedObjectId a owl:DatatypeProperty ;
        rdfs:domain steel:ReheatingFurnaceOp ;
        rdfs:subPropertyOf steel:slabCreatedObjectId .
    steel:slabCreatedObjectId a owl:DatatypeProperty ;
        rdfs:domain steel:Slab .
    """
    g = _g(ttl)
    stats = _remove_cross_domain_subproperties(g, str(DOMAIN_NS))
    assert stats["cross_domain_subproperties_removed"] == 1
    assert (
        URIRef(f"{DOMAIN_NS}reheatingFurnaceOpCreatedObjectId"),
        RDFS.subPropertyOf,
        URIRef(f"{DOMAIN_NS}slabCreatedObjectId"),
    ) not in g


def test_same_domain_subproperty_preserved():
    """domain 이 겹치는(같은) property 간 subPropertyOf 는 보존."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:childId a owl:DatatypeProperty ;
        rdfs:domain steel:EquipmentMaster ;
        rdfs:subPropertyOf steel:parentId .
    steel:parentId a owl:DatatypeProperty ;
        rdfs:domain steel:EquipmentMaster .
    """
    g = _g(ttl)
    stats = _remove_cross_domain_subproperties(g, str(DOMAIN_NS))
    assert stats["cross_domain_subproperties_removed"] == 0
    assert (
        URIRef(f"{DOMAIN_NS}childId"),
        RDFS.subPropertyOf,
        URIRef(f"{DOMAIN_NS}parentId"),
    ) in g


def test_polymorphic_subproperty_preserved():
    """한쪽이라도 domain 이 없으면(polymorphic) 보존 — 다른 step 이 채울 수 있음."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:childId a owl:DatatypeProperty ;
        rdfs:domain steel:EquipmentMaster ;
        rdfs:subPropertyOf steel:genericId .
    steel:genericId a owl:DatatypeProperty .
    """
    g = _g(ttl)
    stats = _remove_cross_domain_subproperties(g, str(DOMAIN_NS))
    assert stats["cross_domain_subproperties_removed"] == 0
    assert (
        URIRef(f"{DOMAIN_NS}childId"),
        RDFS.subPropertyOf,
        URIRef(f"{DOMAIN_NS}genericId"),
    ) in g


def test_linear_chain_preserved():
    """순환이 아닌 정상 subPropertyOf 체인은 보존되어야 한다."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:hasChild a owl:ObjectProperty ; rdfs:subPropertyOf steel:hasDescendant .
    steel:hasDescendant a owl:ObjectProperty ; rdfs:subPropertyOf steel:hasRelation .
    steel:hasRelation a owl:ObjectProperty .
    """
    g = _g(ttl)
    stats = _break_sub_property_cycles(g, str(DOMAIN_NS))
    assert stats["sub_property_cycles_broken"] == 0
    # 원본 edge 유지
    assert (
        URIRef(f"{DOMAIN_NS}hasChild"),
        RDFS.subPropertyOf,
        URIRef(f"{DOMAIN_NS}hasDescendant"),
    ) in g
    assert (
        URIRef(f"{DOMAIN_NS}hasDescendant"),
        RDFS.subPropertyOf,
        URIRef(f"{DOMAIN_NS}hasRelation"),
    ) in g


def test_self_loop_is_not_an_scc():
    """self-loop (opA subPropertyOf opA) 는 size=1 SCC 이므로 skip."""
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:opA a owl:ObjectProperty ; rdfs:subPropertyOf steel:opA .
    """
    g = _g(ttl)
    stats = _break_sub_property_cycles(g, str(DOMAIN_NS))
    # self-loop 는 별도 규칙 (_rule_self_referencing...) 로 처리
    assert stats["sub_property_cycles_broken"] == 0


def test_datatype_property_cycle_also_broken():
    ttl = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    steel:dpA a owl:DatatypeProperty ; rdfs:subPropertyOf steel:dpB .
    steel:dpB a owl:DatatypeProperty ; rdfs:subPropertyOf steel:dpA .
    """
    g = _g(ttl)
    stats = _break_sub_property_cycles(g, str(DOMAIN_NS))
    assert stats["sub_property_cycles_broken"] >= 1
