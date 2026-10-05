"""Mutation 감사가 **결함을 심지도 못했다** — 검출률 0% 의 진짜 원인.

2026-08-24 실측 (배포 T-Box). S9.5 KG 감사 8개 중 **7개가 ``no_effect``** 였고
``caught_at_least_once: 0`` 이 보고됐다. 검출률 0% 가 "검증 체계가 결함을 놓쳤다"
로 읽혔지만, 실제로는 **결함이 T-Box 에 심어지지도 않았다.**

## 원인 1 — 첫 후보만 시도했다

``resolve_targets(..., limit=1)`` 이 정렬 첫 후보 하나만 골랐다. 그 후보가 mutator
의 WHERE 전제를 만족하지 못하면 그대로 ``no_effect`` 다::

    ?TARGET_PROP 첫 후보  airEmissionMonitoringConcentration  (DatatypeProperty)
    delete_inverse 요구   owl:inverseOf 보유 프로퍼티

T-Box 에 ``owl:inverseOf`` 는 **120개** 있었지만 정렬 순서 17번째부터 나타나서
첫 후보로는 절대 도달하지 못했다.

수정: mutator 자신의 WHERE 절을 ``SELECT`` 로 감싸 **전제를 만족하는 대상을 직접
질의** 한다 (:func:`_targets_from_where`). WHERE 에 없는 placeholder (INSERT 전용
``?FOREIGN_CLASS``) 는 후보 목록으로 보충한다.

## 원인 2 — 효과를 ``len()`` 으로 셌다

``DELETE {…} INSERT {…}`` 는 같은 개수를 지우고 넣으므로 **길이가 변하지 않는다.**
``some_to_all`` 이 ``owl:someValuesFrom`` 101개를 ``allValuesFrom`` 으로 전부
바꿨는데 ``len`` 이 4972 → 4972 라 ``no_effect`` 로 기록됐다. 치환 계열 mutator 가
카탈로그의 절반이므로 이 오판이 검출률을 구조적으로 깎았다.

수정: 집합 차분(``set(before) - set(after)``)으로 센다.

## 결과 — 검출률이 **내려간** 것이 정상이다

    수정 전  심어진 결함  6/21 → 검출 3  = 50.0%
    수정 후  심어진 결함 16/21 → 검출 2  = 12.5%

분모가 정직해졌다. 예전에는 결함 10개가 심어지지도 않아 분모에서 빠졌고, 그래서
검출률이 실제보다 4배 높게 보였다. 이번 수정의 산출물은 "검출률 개선" 이 아니라
**14개 진짜 사각지대의 발견** 이다 (M1~M7 전 카테고리에 분포).
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from tools.mutation_runner import (
    Mutator,
    _targets_from_where,
    apply_mutator,
    discover_mutators,
)

NS = Namespace("http://example.org/t#")


def _mut(name: str, sparql: str, category: str = "M1") -> Mutator:
    return Mutator(name=name, category=category, path="<test>", sparql=sparql)


# ──────────────────────────────────────────────────────────────────
# 1. 첫 후보가 전제를 만족하지 못해도 심어야 한다 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

def test_target_beyond_first_candidate_is_found():
    """정렬 첫 후보가 전제를 못 만족하면 뒤 후보를 찾아야 한다.

    실측 재현: ``aaaProp`` 이 정렬 첫 후보인데 ``owl:inverseOf`` 가 없고,
    ``zzzProp`` 만 갖고 있다. 예전 코드는 ``aaaProp`` 에서 멈춰 ``no_effect``.
    """
    g = Graph()
    for name in ("aaaProp", "zzzProp"):
        g.add((NS[name], RDF.type, OWL.ObjectProperty))
    g.add((NS["zzzProp"], OWL.inverseOf, NS["other"]))

    mutator = _mut("M2_inverse/delete_inverse", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv . }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv . }
    """)
    mutated, info = apply_mutator(g, mutator)
    assert info["applied"] is True, f"첫 후보에서 멈췄다: {info}"
    assert info["target"]["?TARGET_PROP"] == str(NS["zzzProp"])
    assert (NS["zzzProp"], OWL.inverseOf, NS["other"]) not in mutated


def test_where_clause_is_the_primary_resolution_path():
    """WHERE 절이 대상을 직접 고른다 (눈먼 스캔보다 우선)."""
    g = Graph()
    for i in range(30):
        g.add((NS[f"p{i:02d}"], RDF.type, OWL.ObjectProperty))
    g.add((NS["p29"], OWL.inverseOf, NS["x"]))

    binding = _targets_from_where(g, """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv }
    """, ["TARGET_PROP"])
    assert binding == {"?TARGET_PROP": NS["p29"]}, binding


def test_insert_only_placeholder_is_filled_from_candidates():
    """WHERE 에 없는 placeholder (INSERT 전용) 도 채워야 한다.

    ``add_spurious_inverse`` 의 ``?FOREIGN_CLASS`` 는 INSERT 에만 있어 SELECT 로
    바인딩할 수 없다. 이 경로가 빠지면 그 mutator 는 영구 ``no_effect``.
    """
    g = Graph()
    g.add((NS["op"], RDF.type, OWL.ObjectProperty))
    g.add((NS["SomeClass"], RDF.type, OWL.Class))

    mutated, info = apply_mutator(g, _mut("M2_inverse/add_spurious_inverse", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        INSERT { ?TARGET_PROP owl:inverseOf ?FOREIGN_CLASS . }
        WHERE  { ?TARGET_PROP a owl:ObjectProperty .
                 FILTER NOT EXISTS { ?TARGET_PROP owl:inverseOf ?any } }
    """, category="M2"))
    assert info["applied"] is True, f"INSERT 전용 placeholder 를 못 채웠다: {info}"
    assert list(mutated.objects(NS["op"], OWL.inverseOf))


def test_placeholder_type_constraint_is_enforced():
    """``?TARGET_CLASS`` 는 **클래스만** 골라야 한다.

    mutator 의 WHERE 는 자기 전제만 적고 대상 종류는 placeholder 이름에 맡긴다 —
    ``delete_label`` 의 WHERE 는 ``?TARGET_CLASS rdfs:label ?l`` 뿐이라 **label 을
    가진 무엇이든** 매칭한다. 실측 (2026-08-24): 그래서 ``owl:Ontology`` (온톨로지
    메타데이터) 가 선택돼 "클래스 label 삭제" 가 아닌 결함이 심어졌고, 클래스
    label 만 보는 ``check_quality_rules`` 가 정당하게 무반응이라 **검증 사각지대로
    오집계** 됐다.

    종류 제약을 넣자 그 두 mutant (delete_label / uri_as_label) 가 WARN→FAIL 로
    검출됐다 — 검증기는 처음부터 정상이었다.
    """
    g = Graph()
    onto = URIRef("http://example.org/t")            # 도메인 NS 자체 = owl:Ontology
    g.add((onto, RDF.type, OWL.Ontology))
    g.add((onto, RDFS.label, Literal("Ontology Title")))
    g.add((NS["RealClass"], RDF.type, OWL.Class))
    g.add((NS["RealClass"], RDFS.label, Literal("Real Class")))

    mutated, info = apply_mutator(g, _mut("M5_annotation/delete_label", """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        DELETE { ?TARGET_CLASS rdfs:label ?l }
        WHERE  { ?TARGET_CLASS rdfs:label ?l }
    """, category="M5"))
    assert info["applied"] is True, info
    assert info["target"]["?TARGET_CLASS"] == str(NS["RealClass"]), (
        f"클래스가 아닌 대상을 골랐다: {info['target']}"
    )
    # 온톨로지 메타데이터는 건드리지 않았다.
    assert list(mutated.objects(onto, RDFS.label))


def test_target_prop_constraint_accepts_both_property_kinds():
    """``?TARGET_PROP`` 은 OP/DP 둘 다 받는다 (과잉 제약 방지).

    UNION 가드가 한쪽만 허용하면 그 종류의 mutator 가 전부 no_effect 가 된다.
    """
    for kind in (OWL.ObjectProperty, OWL.DatatypeProperty):
        g = Graph()
        g.add((NS["p"], RDF.type, kind))
        g.add((NS["p"], RDFS.range, NS["X"]))
        _, info = apply_mutator(g, _mut("M6_reference/rename_to_unknown", """
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            DELETE { ?TARGET_PROP rdfs:range ?orig }
            INSERT { ?TARGET_PROP rdfs:range <http://example.org/DOES_NOT_EXIST> }
            WHERE  { ?TARGET_PROP rdfs:range ?orig . FILTER (!isBlank(?orig)) }
        """, category="M6"))
        assert info["applied"] is True, f"{kind} 를 거부했다: {info}"


def test_union_guard_does_not_break_query_parsing():
    """UNION 가드를 넣어도 SPARQL 파싱이 깨지지 않는다.

    가드를 WHERE 블록 **안에** 문자열로 끼워 넣으면 ``UNION`` 으로 끝나는 패턴 뒤에
    ``.`` 이 붙어 파싱이 실패한다 (실측: ``delete_inverse`` 가 폴백으로 떨어졌다).
    블록을 중괄호로 감싸는 방식이어야 한다.
    """
    g = Graph()
    g.add((NS["p"], RDF.type, OWL.ObjectProperty))
    g.add((NS["p"], OWL.inverseOf, NS["q"]))
    binding = _targets_from_where(g, """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv }
    """, ["TARGET_PROP"])
    assert binding == {"?TARGET_PROP": NS["p"]}, (
        f"UNION 가드가 쿼리를 깨뜨려 폴백으로 떨어졌다: {binding}"
    )


def test_binding_is_deterministic():
    """같은 그래프면 항상 같은 대상 — 재실행 비교가 성립해야 한다."""
    g = Graph()
    for name in ("bProp", "aProp", "cProp"):
        g.add((NS[name], RDF.type, OWL.ObjectProperty))
        g.add((NS[name], OWL.inverseOf, NS["inv" + name]))
    mutator = _mut("m", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv }
    """)
    targets = {apply_mutator(g, mutator)[1]["target"]["?TARGET_PROP"] for _ in range(4)}
    assert len(targets) == 1, f"비결정적 대상 선택: {targets}"


# ──────────────────────────────────────────────────────────────────
# 2. DELETE+INSERT 를 no_effect 로 오판하지 않는다
# ──────────────────────────────────────────────────────────────────

def test_replace_same_count_is_detected_as_applied():
    """THE REGRESSION: 지운 수 == 넣은 수 여도 결함이 심어진 것이다."""
    g = Graph()
    r = NS["restr"]
    g.add((r, RDF.type, OWL.Restriction))
    g.add((r, OWL.someValuesFrom, NS["C"]))

    mutated, info = apply_mutator(g, _mut("M7_restriction/some_to_all", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?r owl:someValuesFrom ?c }
        INSERT { ?r owl:allValuesFrom ?c }
        WHERE  { ?r a owl:Restriction ; owl:someValuesFrom ?c }
    """, category="M7"))
    assert len(g) == len(mutated), "이 픽스처는 길이가 같아야 의미가 있다"
    assert info["applied"] is True, f"치환을 no_effect 로 오판했다: {info}"
    assert info["triples_removed"] == 1
    assert info["triples_added"] == 1
    assert (r, OWL.allValuesFrom, NS["C"]) in mutated
    assert (r, OWL.someValuesFrom, NS["C"]) not in mutated


def test_genuinely_absent_premise_is_still_no_effect():
    """전제가 그래프에 없으면 ``no_effect`` 가 정답이다 (과잉 적용 방지).

    이 방향이 없으면 "무조건 applied 로 보고" 로 지표를 살 수 있다.
    """
    g = Graph()
    g.add((NS["p"], RDF.type, OWL.ObjectProperty))   # inverseOf 없음
    _, info = apply_mutator(g, _mut("m", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv }
    """))
    assert info["applied"] is False
    assert info["reason"] == "no_effect"


def test_no_target_when_placeholder_has_no_candidates():
    """placeholder 후보가 아예 없으면 ``no_target_*`` 로 구분한다."""
    g = Graph()   # 프로퍼티 0개
    _, info = apply_mutator(g, _mut("m", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv }
    """))
    assert info["applied"] is False
    assert info["reason"] == "no_target_TARGET_PROP"


def test_graph_is_not_mutated_in_place():
    """원본 그래프는 변하지 않는다 — baseline 오염 방지."""
    g = Graph()
    g.add((NS["p"], RDF.type, OWL.ObjectProperty))
    g.add((NS["p"], OWL.inverseOf, NS["q"]))
    before = set(g)
    apply_mutator(g, _mut("m", """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        DELETE { ?TARGET_PROP owl:inverseOf ?inv }
        WHERE  { ?TARGET_PROP owl:inverseOf ?inv }
    """))
    assert set(g) == before, "baseline 그래프가 오염됐다"


# ──────────────────────────────────────────────────────────────────
# 3. 실제 카탈로그 회귀 — 감사가 결함을 심는다
# ──────────────────────────────────────────────────────────────────

_TBOX = "data/generated/tbox/t_box.ttl"


def _deployed_tbox() -> Graph:
    import os
    if not os.path.exists(_TBOX):
        pytest.skip("배포 T-Box 없음")
    return Graph().parse(_TBOX, format="turtle")


def test_deployed_catalog_plants_most_defects():
    """THE REGRESSION: 21개 중 8개만 심어졌던 것이 대폭 늘어야 한다.

    이 단정이 깨지면 감사가 다시 "결함을 심지도 못한 채 검출률 0%" 상태로
    돌아간다 — 그 상태는 검증 체계의 실패로 오독된다.
    """
    g = _deployed_tbox()
    mutators = discover_mutators("rules/mutations")
    applied = [m.name for m in mutators if apply_mutator(g, m)[1].get("applied")]
    assert len(applied) >= 14, (
        f"심어진 결함 {len(applied)}/{len(mutators)}개뿐 — 타깃 바인딩이 퇴행했다: "
        f"미적용={sorted(set(m.name for m in mutators) - set(applied))}"
    )


def test_inverse_mutators_reach_their_premise_on_deployed_tbox():
    """``owl:inverseOf`` 계열이 실제로 심어진다 (실측 사각지대였다)."""
    g = _deployed_tbox()
    if not list(g.triples((None, OWL.inverseOf, None))):
        pytest.skip("배포 T-Box 에 owl:inverseOf 없음")
    for m in discover_mutators("rules/mutations"):
        if m.name.endswith("delete_inverse"):
            info = apply_mutator(g, m)[1]
            assert info.get("applied") is True, (
                f"inverseOf 가 T-Box 에 있는데 심지 못했다: {info}"
            )
            return
    pytest.skip("delete_inverse mutator 없음")


def test_replace_style_mutators_are_applied_on_deployed_tbox():
    """치환 계열(someValuesFrom→allValuesFrom)이 심어진다."""
    g = _deployed_tbox()
    if not list(g.triples((None, OWL.someValuesFrom, None))):
        pytest.skip("배포 T-Box 에 someValuesFrom 없음")
    for m in discover_mutators("rules/mutations"):
        if m.name.endswith("some_to_all"):
            info = apply_mutator(g, m)[1]
            assert info.get("applied") is True, (
                f"치환을 len 비교로 no_effect 오판했다: {info}"
            )
            assert info["triples_removed"] > 0 and info["triples_added"] > 0
            return
    pytest.skip("some_to_all mutator 없음")


def test_label_mutator_still_works_with_literal_objects():
    """리터럴 객체를 다루는 mutator 도 정상 동작 (URIRef 전용 회귀 방지)."""
    g = Graph()
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((NS["C"], RDFS.label, Literal("Some Label")))
    _, info = apply_mutator(g, _mut("M5_annotation/delete_label", """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        DELETE { ?TARGET_CLASS rdfs:label ?l }
        WHERE  { ?TARGET_CLASS rdfs:label ?l }
    """, category="M5"))
    assert info["applied"] is True, info
