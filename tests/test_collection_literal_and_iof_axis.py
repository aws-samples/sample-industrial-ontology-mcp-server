"""Turtle 컬렉션 문자열 오염 + IOF 상위 프로퍼티 범주 충돌 (rank 4 + rank 2 원인).

## rank 4 — 컬렉션 표기가 문자열이 됐다

``add_triple`` 이 object 를 해석할 때 ``parse_object_term`` 이 Turtle 컬렉션
``( a b c )`` 를 몰라서 **평문 리터럴**로 저장했다. 실측 (2026-08-30 배포 T-Box):
``owl:disjointUnionOf`` 7건이 ``"(steel:A steel:B)"^^xsd:string`` 형태였고, 같은
부모에 정상 리스트와 문자열이 **공존**했다. jury 는 ``applied`` 를 보고했다.

OWL 상 리스트가 아니므로 그 공리는 추론에 아무 영향이 없다 — "분류 완전성을
선언했다" 고 믿는데 실제로는 선언되지 않은 상태다. ``check_quality_rules`` 도
``validate_tbox_shacl`` 도 이 술어를 읽지 않고, HermiT 은 리터럴 object 를
무시하므로 세 게이트 모두 침묵했다.

## rank 2 원인 — IOF 상위 프로퍼티가 domain 범주를 강요한다

``steel:hasFuelEnergySource rdfs:subPropertyOf iof:hasInput`` 이고
``iof:hasInput rdfs:domain obo:BFO_0000015`` (process) 다. 그런데 그 OP 의 domain
``FuelConsumption`` 은 ``iof:MeasurementInformationContentEntity`` (정보 개체) 이고
BFO 는 occurrent 와 continuant 를 배타적으로 둔다 ⇒ **unsatisfiable**.

배포 T-Box 의 IOF subPropertyOf 12건 중 **이것 하나**가 문제였고 나머지 11건
(``hasParticipantAtSomeTime`` 8 / ``isLocatedIn`` 3) 은 안전하다. 즉 "IOF 매핑을
쓰지 말라" 가 아니라 **범주가 맞는지 보라** 가 정답이다.

이 unsat 은 명명 Restriction 을 익명화한 뒤에만 드러난다 — 그것이 rank 2 의 눈멂이
숨기던 실체 결함이다.
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_12h_iof_category_conflict as step_12h
from tools.quality_steps import step_12j_collection_literal_cleanup as step_12j
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)
IOF = "https://spec.industrialontologies.org/ontology/construct/"
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")


def _ctx() -> StepContext:
    return StepContext(domain_ns=NS)


def _tbox() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    g = Graph()
    g.parse(str(TBOX), format="turtle")
    return g


# ── rank 4: 생성 지점 — parse_object_term 이 컬렉션을 이해한다 ────────────


def test_parse_object_term_builds_rdf_list():
    """THE REGRESSION: Turtle 컬렉션이 리터럴이 아니라 RDF 리스트가 된다."""
    from domain.graph_utils import parse_object_term

    g = Graph()
    g.bind("steel", NS)

    node = parse_object_term("( steel:A steel:B steel:C )", g)

    assert not isinstance(node, Literal), "컬렉션이 리터럴로 저장됐다"
    assert [str(x).replace(NS, "") for x in g.items(node)] == ["A", "B", "C"]


def test_empty_collection_is_rdf_nil():
    from domain.graph_utils import parse_object_term

    g = Graph()

    assert parse_object_term("()", g) == RDF.nil


def test_collection_without_graph_returns_none():
    """리스트 노드를 만들 곳이 없으면 리터럴 폴백보다 None 이 정직하다."""
    from domain.graph_utils import parse_object_term

    assert parse_object_term("(steel:A steel:B)", None) is None


@pytest.mark.parametrize(("raw", "expect_literal"), [
    ('"라벨"@ko', True),
    ('"true"^^xsd:boolean', True),
    ("steel:Equipment", False),
    ("http://example.org/x", False),
    ("평문 값", True),
])
def test_other_object_forms_unchanged(raw, expect_literal):
    """NEGATIVE 방향 — 컬렉션 지원이 다른 표기를 깨지 않는다."""
    from domain.graph_utils import parse_object_term

    g = Graph()
    g.bind("steel", NS)
    g.bind("xsd", "http://www.w3.org/2001/XMLSchema#")

    node = parse_object_term(raw, g)

    assert isinstance(node, Literal) is expect_literal, f"{raw!r} → {node!r}"


def test_jury_add_triple_emits_real_list():
    """엔진 경로 확인 — 카운터가 아니라 그래프로 본다."""
    from tools.jury_fixes import apply_jury_fixes

    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        f"@prefix steel: <{NS}> .\n"
        "steel:P a owl:Class . steel:A a owl:Class . steel:B a owl:Class .\n"
    )

    result = apply_jury_fixes(ttl, [{
        "action": "add_triple", "subject": "steel:P",
        "predicate": "owl:disjointUnionOf", "object": "(steel:A steel:B)",
    }])

    assert len(result["applied"]) == 1, result
    g = Graph()
    g.parse(data=result["ttl"], format="turtle")
    objs = list(g.objects(URIRef(NS + "P"), OWL.disjointUnionOf))
    assert objs and not isinstance(objs[0], Literal)
    assert [str(x).replace(NS, "") for x in g.items(objs[0])] == ["A", "B"]


# ── rank 4: 사후 정리 — 이미 오염된 산출물 ───────────────────────────────


def test_cleanup_removes_redundant_literal_beside_valid_list():
    """정상 리스트가 있으면 리터럴은 잉여이므로 제거만 한다."""
    g = Graph()
    g.bind("steel", NS)
    parent = URIRef(NS + "P")
    for local in ("P", "A", "B"):
        g.add((URIRef(NS + local), RDF.type, OWL.Class))
    import rdflib.collection
    from rdflib import BNode
    head = BNode()
    rdflib.collection.Collection(g, head, [URIRef(NS + "A"), URIRef(NS + "B")])
    g.add((parent, OWL.disjointUnionOf, head))
    g.add((parent, OWL.disjointUnionOf, Literal("(steel:A steel:B)")))

    stats = step_12j._cleanup(g, NS)

    assert stats["collection_literals_removed"] == 1
    assert stats["collection_literals_repaired"] == 0
    remaining = list(g.objects(parent, OWL.disjointUnionOf))
    assert len(remaining) == 1 and not isinstance(remaining[0], Literal)


def test_cleanup_repairs_literal_when_no_valid_list():
    """리스트가 없으면 문자열을 파싱해 복구한다 — 지우기만 하면 의도가 사라진다."""
    g = Graph()
    g.bind("steel", NS)
    parent = URIRef(NS + "P")
    for local in ("P", "A", "B"):
        g.add((URIRef(NS + local), RDF.type, OWL.Class))
    g.add((parent, OWL.disjointUnionOf, Literal("(steel:A steel:B)")))

    stats = step_12j._cleanup(g, NS)

    assert stats["collection_literals_repaired"] == 1
    objs = list(g.objects(parent, OWL.disjointUnionOf))
    assert len(objs) == 1 and not isinstance(objs[0], Literal)
    assert [str(x).replace(NS, "") for x in g.items(objs[0])] == ["A", "B"]


def test_cleanup_does_not_repair_with_undeclared_member():
    """미선언 멤버가 섞이면 복구가 유령 노드를 리스트에 넣는다 — 포기한다."""
    g = Graph()
    g.bind("steel", NS)
    parent = URIRef(NS + "P")
    g.add((parent, RDF.type, OWL.Class))
    g.add((URIRef(NS + "A"), RDF.type, OWL.Class))
    # steel:Ghost 는 선언하지 않는다.
    g.add((parent, OWL.disjointUnionOf, Literal("(steel:A steel:Ghost)")))

    stats = step_12j._cleanup(g, NS)

    assert stats["collection_literals_unrepairable"] == 1
    assert not list(g.objects(parent, OWL.disjointUnionOf))


def test_deployed_tbox_has_no_collection_literal():
    """배포 산출물 실측 — 리스트 자리에 문자열이 없다."""
    g = _tbox()

    offenders = [
        (str(s).replace(NS, ""), str(p).rsplit("#", 1)[-1])
        for p in step_12j._LIST_VALUED_PREDICATES
        for s, o in g.subject_objects(p)
        if isinstance(o, Literal)
    ]

    assert not offenders, f"리스트 자리 문자열이 남아 있다: {offenders}"


# ── rank 2 원인: IOF 상위 프로퍼티 범주 충돌 ─────────────────────────────


def test_iof_property_axis_conflict_is_removed():
    """THE REGRESSION: 상위 프로퍼티 domain 이 하위 domain 범주와 어긋나면 끊는다."""
    g = _tbox()
    prop = URIRef(NS + "hasFuelEnergySource")
    if (prop, RDF.type, OWL.ObjectProperty) not in g:
        pytest.skip("대상 OP 없음 (T-Box 세대 차이)")
    # 이미 정리된 상태면 다시 심어 검출을 확인한다.
    g.add((prop, RDFS.subPropertyOf, URIRef(IOF + "hasInput")))

    stats = step_12h._remove_property_axis_conflicts(g, NS)

    assert stats["iof_property_axis_conflicts_removed"] >= 1, stats
    assert (prop, RDFS.subPropertyOf, URIRef(IOF + "hasInput")) not in g


def test_safe_iof_property_mappings_are_kept():
    """NEGATIVE 방향 — 범주가 맞는 IOF 매핑은 보존된다.

    ``hasParticipantAtSomeTime`` / ``isLocatedIn`` 계열 11건이 그것이다. 전부
    지우면 표준 정렬을 잃는다.
    """
    g = _tbox()
    before = {
        (s, o) for s, o in g.subject_objects(RDFS.subPropertyOf)
        if isinstance(o, URIRef) and str(o).startswith(IOF)
    }
    if not before:
        pytest.skip("IOF subPropertyOf 선언 없음")

    step_12h._remove_property_axis_conflicts(g, NS)

    after = {
        (s, o) for s, o in g.subject_objects(RDFS.subPropertyOf)
        if isinstance(o, URIRef) and str(o).startswith(IOF)
    }
    assert after, "IOF 매핑이 전부 제거됐다 — 과잉 정리"
    removed = before - after
    assert all("hasInput" in str(o) for _s, o in removed), (
        f"안전한 매핑까지 제거됐다: {[(str(s), str(o)) for s, o in removed]}"
    )


def test_property_axis_check_skips_when_domain_ambiguous():
    """domain 이 없거나 여럿이면 판정 근거가 약하다 — 건드리지 않는다."""
    g = _tbox()
    prop = URIRef(NS + "hasFuelEnergySource")
    if (prop, RDF.type, OWL.ObjectProperty) not in g:
        pytest.skip("대상 OP 없음")
    g.add((prop, RDFS.subPropertyOf, URIRef(IOF + "hasInput")))
    # domain 을 하나 더 붙여 모호하게 만든다.
    g.add((prop, RDFS.domain, URIRef(NS + "GasEnergy")))

    stats = step_12h._remove_property_axis_conflicts(g, NS)

    assert stats["iof_property_axis_conflicts_removed"] == 0
    assert (prop, RDFS.subPropertyOf, URIRef(IOF + "hasInput")) in g
