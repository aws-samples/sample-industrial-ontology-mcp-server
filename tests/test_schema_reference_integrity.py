"""T-Box 스키마 축이 **선언되지 않은 IRI** 를 가리켜도 아무도 잡지 않았다.

2026-08-24 실측 (배포 T-Box): ``MaintenanceHistory ⊑ MaintenanceActivity`` 인데
``MaintenanceActivity`` 는 **어디에도 선언되지 않았다** (``owl:Class`` 아님, A-Box
인스턴스 0건). 그런데 기존 게이트 전부가 통과했다:

  - ``check_quality_rules`` → critical 0 / high 0, ``MaintenanceActivity`` 언급 0건
  - ``validate_owl_consistency`` (HermiT) → 통과. RDFS 의미론상 미선언 IRI 참조는
    **논리적 오류가 아니다** (암묵적으로 클래스로 취급된다).
  - ``댕글링 참조 탐지`` → A-Box 인스턴스 대상만 훑는다. **스키마 축은 범위 밖.**

## 어떻게 발견했나 — mutation 이 지목했다

``rename_to_unknown`` mutation (``rdfs:range`` 를 존재하지 않는 IRI 로 교체) 이
**22개 체크 중 어느 것에도 잡히지 않았다.** 그 프로퍼티는 A-Box 에서 1,560 트리플
쓰이는데도 그랬다. 그 미검출을 추적해 이 갭에 도달했다.

## 무엇이 문제인가

유령 노드가 계층에 조용히 섞인다. DIT/NOC 지표가 실재하지 않는 클래스를 세고, 그
부모를 기대한 질의는 **에러 없이 0건** 을 반환한다. 원인도 함께 드러났다 —
프롬프트는 ``iof-maint:MaintenanceActivity`` (외래 IOF 클래스) 를 지시하는데 T-Box
에는 ``steel:MaintenanceActivity`` 로 적혀 있다. 외래 prefix 뭉갬 계열이고, S2 LLM
산출물에 이미 4건 있으므로 **재실행해도 재발한다** — 그래서 산출물 손수정이 아니라
게이트가 정답이다.

## 오탐 방지가 이 검사의 절반이다

XSD/OWL/RDFS/RDF, 외래 온톨로지(``owl:imports`` 대상), 명명된 ``owl:Restriction``
(skolemize 산물 = 공리 노드), blank node 는 **선언 대상이 아니다.** 특히 명명된
Restriction 을 빼지 않으면 배포 T-Box 에서 103건이 오탐으로 터진다 (실측) — 이
리포는 그 혼동으로 클래스 수가 2.4배 부풀고 baseline 이 FAIL 로 고정된 이력이 있다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.validation_support.checks.semantic import check_schema_reference_integrity

NS = Namespace(str(DOMAIN_NS))
_TBOX = "data/generated/tbox/t_box.ttl"


def _g() -> Graph:
    return Graph()


# ──────────────────────────────────────────────────────────────────
# 1. 미선언 참조를 잡는다 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("pred", "axis"), [
    (RDFS.subClassOf, "subClassOf"),
    (RDFS.domain, "domain"),
    (RDFS.range, "range"),
    (RDFS.subPropertyOf, "subPropertyOf"),
])
def test_undeclared_target_is_reported(pred, axis):
    """네 축 모두 미선언 대상을 보고한다."""
    g = _g()
    subj = NS["Declared"]
    g.add((subj, RDF.type, OWL.Class if "Class" in axis else OWL.ObjectProperty))
    g.add((subj, pred, NS["GhostTarget"]))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is False, f"{axis} 미선언 대상을 놓쳤다"
    assert r["by_axis"] == {axis: 1}
    assert r["violations"][0]["undeclared_target"] == "GhostTarget"


def test_declared_target_passes():
    """대상이 선언돼 있으면 통과 (과잉 차단 방지 — 주 방향)."""
    g = _g()
    g.add((NS["Child"], RDF.type, OWL.Class))
    g.add((NS["Parent"], RDF.type, OWL.Class))
    g.add((NS["Child"], RDFS.subClassOf, NS["Parent"]))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True, r
    assert r["undeclared_references"] == 0
    assert r["checked_references"] == 1


def test_any_meta_type_counts_as_declared():
    """어느 메타클래스로든 선언되면 정의로 본다 — 목적은 존재 확인이다."""
    for meta in (OWL.Class, RDFS.Class, OWL.ObjectProperty, OWL.DatatypeProperty,
                 OWL.AnnotationProperty, RDFS.Datatype, OWL.NamedIndividual):
        g = _g()
        g.add((NS["P"], RDF.type, OWL.ObjectProperty))
        g.add((NS["Target"], RDF.type, meta))
        g.add((NS["P"], RDFS.range, NS["Target"]))
        r = check_schema_reference_integrity(g)
        assert r["passed"] is True, f"{meta} 선언을 인정하지 않았다"


# ──────────────────────────────────────────────────────────────────
# 2. 오탐 방지 (이 검사의 절반)
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    XSD.decimal, XSD.string, XSD.dateTime,
    OWL.Thing, RDFS.Literal, RDF.Property,
])
def test_standard_vocabulary_is_not_a_violation(target):
    """XSD/OWL/RDFS/RDF 는 도메인이 선언할 대상이 아니다."""
    g = _g()
    g.add((NS["dp"], RDF.type, OWL.DatatypeProperty))
    g.add((NS["dp"], RDFS.range, target))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True, f"{target} 를 위반으로 봤다"


def test_foreign_ontology_reference_is_not_a_violation():
    """IOF/BFO 등 외래 온톨로지는 owl:imports 대상 — 이 그래프에 선언이 없다."""
    from domain.namespaces import FOREIGN_PREFIXES
    if not FOREIGN_PREFIXES:
        pytest.skip("외래 prefix 미설정")
    foreign_ns = next(iter(FOREIGN_PREFIXES.values()))
    g = _g()
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((NS["C"], RDFS.subClassOf, URIRef(f"{foreign_ns}SomeForeignClass")))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True, f"외래 IRI 를 위반으로 봤다: {r['violations']}"


def test_named_restriction_is_not_a_violation():
    """명명된 ``owl:Restriction`` 은 공리 노드다 — 분류 계층이 아니다.

    이걸 빼지 않으면 배포 T-Box 에서 103건이 오탐으로 터진다 (실측).
    """
    g = _g()
    restr = NS["Child_someProp_someValuesFrom"]
    g.add((NS["Child"], RDF.type, OWL.Class))
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((NS["Child"], RDFS.subClassOf, restr))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True, r


def test_class_expression_body_is_excluded_without_rdf_type():
    """``owl:onProperty`` 등 표현식 술어를 가진 노드는 타입 없이도 배제된다.

    두 방어선이 있다: ``defined`` 집합의 ``OWL.Restriction`` (rdf:type 기반) 과
    ``is_anonymous_class_expression`` (술어·이름 기반). 배포 T-Box 의 명명된
    Restriction 102개는 **전부 rdf:type 을 가져서** 첫 방어선만으로 통과한다 —
    그래서 두 번째 방어선을 지우는 mutant 가 살아남았다. 이 테스트가 그 경로를
    직접 찍는다: 직렬화 왕복이나 부분 병합에서 타입 트리플이 빠져도 표현식 본체는
    선언 대상이 아니다.

    이름 폴백(``Union_`` 접두)만으로는 Restriction 이름을 덮지 못한다는 것도 함께
    확인한다 — ``_ANON_EXPR_PREFIXES`` 는 ``("Union_",)`` 하나뿐이다.
    """
    expr = NS["Child_someProp_someValuesFrom"]
    g = _g()
    g.add((NS["Child"], RDF.type, OWL.Class))
    g.add((expr, OWL.onProperty, NS["someProp"]))   # 표현식 본체 (타입 없음)
    g.add((NS["Child"], RDFS.subClassOf, expr))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True, (
        f"표현식 본체를 미선언 참조로 봤다: {r['violations']}"
    )

    # Union_ 접두는 술어 없이도 배제된다 (이름 폴백 경로).
    g2 = _g()
    g2.add((NS["C"], RDF.type, OWL.Class))
    g2.add((NS["C"], RDFS.subClassOf, NS["Union_A_B"]))
    assert check_schema_reference_integrity(g2)["passed"] is True


def test_blank_node_target_is_skipped():
    """익명 표현식(BNode)은 선언 대상이 아니다."""
    from rdflib import BNode
    g = _g()
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((NS["C"], RDFS.subClassOf, BNode()))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True
    assert r["checked_references"] == 0, "BNode 를 검사 대상으로 셌다"


def test_foreign_subject_is_not_our_responsibility():
    """외래 주어의 축은 이 T-Box 의 책임이 아니다."""
    g = _g()
    g.add((URIRef("http://other.org/X"), RDFS.subClassOf, NS["Ghost"]))
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True
    assert r["checked_references"] == 0


# ──────────────────────────────────────────────────────────────────
# 3. 보고 형태
# ──────────────────────────────────────────────────────────────────

def test_violation_carries_axis_and_full_iri():
    """어느 축인지, 어떤 IRI 인지 남는다 (진단 가능성)."""
    g = _g()
    g.add((NS["P"], RDF.type, OWL.ObjectProperty))
    g.add((NS["P"], RDFS.range, NS["Ghost"]))
    v = check_schema_reference_integrity(g)["violations"][0]
    assert v["axis"] == "range"
    assert v["subject"] == "P"
    assert v["target_iri"] == str(NS["Ghost"])


def test_message_explains_the_consequence():
    """메시지가 "왜 위험한지" 를 담는다 — WARN 을 무시하지 않게."""
    g = _g()
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((NS["C"], RDFS.subClassOf, NS["Ghost"]))
    msg = str(check_schema_reference_integrity(g).get("message", ""))
    assert "0건" in msg or "유령" in msg, msg


def test_empty_graph_passes():
    r = check_schema_reference_integrity(_g())
    assert r["passed"] is True
    assert r["checked_references"] == 0


# ──────────────────────────────────────────────────────────────────
# 4. 배포 산출물 + mutation 회귀
# ──────────────────────────────────────────────────────────────────

def _deployed_tbox() -> Graph:
    import os
    if not os.path.exists(_TBOX):
        pytest.skip("배포 T-Box 없음")
    return Graph().parse(_TBOX, format="turtle")


def test_deployed_tbox_scan_is_not_vacuous():
    """배포 T-Box 에서 실제로 무언가를 검사한다 (0건 검사 방지)."""
    r = check_schema_reference_integrity(_deployed_tbox())
    assert r["checked_references"] > 100, (
        f"스키마 참조를 {r['checked_references']}건만 검사했다 — 필터가 과하다"
    )


def test_deployed_tbox_false_alarm_count_is_bounded():
    """오탐이 폭발하지 않는다.

    명명된 Restriction 을 안 빼면 103건이 터진다 (실측). 실재 결함은 1건
    (``MaintenanceHistory ⊑ MaintenanceActivity``) 이므로 상한을 낮게 잡는다.
    """
    r = check_schema_reference_integrity(_deployed_tbox())
    assert r["undeclared_references"] <= 5, (
        f"위반 {r['undeclared_references']}건 — 오탐 필터가 퇴행했다: "
        f"{r['violations'][:5]}"
    )


def test_mutation_that_no_other_check_caught_is_now_detected():
    """THE REGRESSION: ``rename_to_unknown`` 을 잡는다.

    이 mutation 은 ``rdfs:range`` 를 존재하지 않는 IRI 로 바꾸는데, 22개 체크 중
    **어느 것도 잡지 못했다** (실측). 이 검사가 그 사각지대를 메운다.
    """
    from tools.mutation_runner import apply_mutator, discover_mutators
    tbox = _deployed_tbox()
    baseline = check_schema_reference_integrity(tbox)["undeclared_references"]
    mutator = next(
        (m for m in discover_mutators("rules/mutations")
         if m.name.endswith("rename_to_unknown")), None,
    )
    if mutator is None:
        pytest.skip("rename_to_unknown mutator 없음")
    mutated, info = apply_mutator(tbox, mutator)
    if not info.get("applied"):
        pytest.skip(f"mutant 미적용: {info.get('reason')}")
    after = check_schema_reference_integrity(mutated)["undeclared_references"]
    assert after > baseline, (
        f"미선언 IRI 로 바꾼 range 를 놓쳤다 (baseline={baseline}, mutant={after})"
    )


# ──────────────────────────────────────────────────────────────────
# 5. 공리 노드가 분류 계층의 주어가 된 경우
# ──────────────────────────────────────────────────────────────────

def test_axiom_node_as_subclass_subject_is_reported():
    """명명된 Restriction 이 ``rdfs:subClassOf`` 의 **주어** 면 구조 오류다.

    공리 노드가 분류 계층에 참여하면 DIT/NOC 가 공리를 클래스로 센다. 실측:
    ``flip_parent`` mutation 이 이 상태를 만드는데 **KG 단계(S9.5) 23개 체크는
    전부 무반응** 이다 — 순환이 생기지 않아 ``subclass_cycle`` 조차 발화하지 않는다.

    T-Box 단계(S4.5)에서는 HermiT/classify 가 이 mutant 를 잡는다. 즉 이 검사는
    그 축의 **이중 방어** 이고, 추론기를 돌리지 않는 KG 검증 경로에서는 유일한
    방어선이다.
    """
    g = _g()
    restr = NS["C_someProp_someValuesFrom"]
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((NS["Parent"], RDF.type, OWL.Class))
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, RDFS.subClassOf, NS["Parent"]))     # 공리가 주어
    r = check_schema_reference_integrity(g)
    assert r["passed"] is False, "공리 노드가 계층 주어인 것을 놓쳤다"
    assert r["axiom_as_subject_count"] == 1
    assert r["axiom_nodes_in_hierarchy"][0]["axiom_node"] == "C_someProp_someValuesFrom"


def test_axiom_node_as_object_is_normal():
    """반대로 공리 노드가 **목적어** 인 것은 정상 구조다 (과잉 차단 방지).

    ``C ⊑ (someProp some D)`` 는 표준 OWL 표현이다. 이 방향을 막으면 배포
    T-Box 에서 102건이 오탐으로 터진다.
    """
    g = _g()
    restr = NS["C_someProp_someValuesFrom"]
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((NS["C"], RDFS.subClassOf, restr))          # 공리가 목적어
    r = check_schema_reference_integrity(g)
    assert r["passed"] is True, r


def test_deployed_tbox_has_no_axiom_in_hierarchy():
    """배포 T-Box baseline 은 0건 — 이 축이 오탐 없이 민감하다."""
    r = check_schema_reference_integrity(_deployed_tbox())
    assert r.get("axiom_as_subject_count", 0) == 0, (
        f"공리 노드가 계층 주어로 섞였다: {r.get('axiom_nodes_in_hierarchy')}"
    )


def test_axiom_as_subject_is_detected():
    """계층 역전으로 **공리가 주어가 된 것**을 잡는다.

    S4.5(T-Box)에서는 HermiT/classify 가 이미 잡는다. 이 테스트가 고정하는 것은
    **추론기 없이 도는 KG 검증 경로** 에서도 잡힌다는 것이다.

    ## mutation 대상의 우연에 의존하지 않는다 (2026-08-31 정정)

    예전에는 ``flip_parent`` mutator 를 배포 T-Box 에 돌려 그 결과를 주장했다. 그런데
    그 mutator 는 클래스를 하나 골라 부모 방향을 뒤집을 뿐이고, **고른 클래스가 명명
    Restriction 을 가져야** 이 축이 발화한다. 세대별 실측:

        이전 T-Box: 대상 AirEmissionMonitoring (Restriction 보유) → count=2 ✓
        이번 T-Box: 대상 AdjustmentInventoryTransaction (없음)    → count=None ✗

    즉 테스트가 통과한 것은 mutator 가 우연히 맞는 클래스를 뽑았기 때문이었다
    (이 리포의 "mutation 대상 종류가 중요하다" 와 같은 축). 그래서 **결함을 직접
    심어** 검출을 주장한다 — mutator 는 별도 테스트가 다룬다.
    """
    tbox = _deployed_tbox()
    base = check_schema_reference_integrity(tbox)
    assert not base.get("axiom_as_subject_count"), (
        "배포 T-Box 에 이미 공리-주어가 있다 — baseline 이 오염됐다"
    )

    # 명명 Restriction 을 하나 찾아 분류 계층의 주어로 만든다 (= flip_parent 가
    # 운 좋게 맞췄을 때 생기는 상태를 결정적으로 재현).
    axiom_node = next(
        (s for s in tbox.subjects(RDF.type, OWL.Restriction)
         if isinstance(s, URIRef) and str(s).startswith(str(DOMAIN_NS))), None,
    )
    if axiom_node is None:
        pytest.skip("배포 T-Box 에 명명 Restriction 이 없다")
    some_class = next(
        (s for s in tbox.subjects(RDF.type, OWL.Class)
         if isinstance(s, URIRef) and str(s).startswith(str(DOMAIN_NS))), None,
    )
    assert some_class is not None

    mutated = Graph()
    for triple in tbox:
        mutated.add(triple)
    mutated.add((axiom_node, RDFS.subClassOf, some_class))

    after = check_schema_reference_integrity(mutated)
    assert after.get("axiom_as_subject_count", 0) >= 1, (
        f"공리 노드({axiom_node.split('#')[-1]})를 계층 주어로 만들었는데 놓쳤다: "
        f"{ {k: v for k, v in after.items() if k != 'violations'} }"
    )
    assert after["passed"] is False, "공리-주어가 있는데 PASS 로 냈다"
    assert "message" in after, "사람이 읽을 설명이 없다"


def test_registered_in_validate_kg():
    """23번째 check 로 등록돼 있다 — 구현만 있고 안 불리면 의미가 없다."""
    import inspect

    import tools.kg_validation as kgv
    source = inspect.getsource(kgv)
    assert "_check_schema_reference_integrity" in source
    assert 'reg.register("schema_ref"' in source, (
        "validate_kg 에 등록되지 않았다 — 체크가 실행되지 않는다"
    )
