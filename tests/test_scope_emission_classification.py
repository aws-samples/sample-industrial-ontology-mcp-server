"""값 기반 서브클래스가 추론으로 분류된다 — 빈 클래스 회귀 가드.

2026-08-27 실측. `Scope1/2/3Emission` 이 **A-Box 0건 / 추론 0건**의 빈 클래스로 3회
실행 내내 남아 있었고, S2 리뷰어가 그 상태를 critical/high 로 지적해(disjoint 불완전
분할) 합의를 막았다. 원인이 **두 겹**이었다.

## ① DP 이름이 어긋났다 (Path B 전환 누락)

    step_16 이 찾은 DP    scopeType              → T-Box 에 **존재하지 않음**
    실제 DP               ghgEmissionScopeType   → A-Box 120 트리플
    CSV 실데이터          GHG_Emission.Scope_Type = Scope1 50 / Scope2 40 / Scope3 30

class-specific DP(Path B)로 전환할 때 이 스텝이 갱신되지 않았다. 없는 이름으로
Restriction 을 만들면 추론이 가리킬 데이터가 없다. 이 리포가 반복 겪은 "리터럴 이름으로
조회해 게이트가 0건이 됐다" 와 같은 계열이다 (value_ranges 7규칙 전량 0건 사고).

S3 스텝 전수 조사 결과 이 결함은 **step_16 하나뿐**이다 (`DOMAIN_NS_OBJ["..."]` 로
참조하는 DP 이름 중 T-Box 에 없는 것은 `scopeType` 만).

## ② 방향이 틀렸다 (subClassOf 로는 분류가 안 된다)

이름을 고쳐도 부족했다. 최소 예제로 실측:

    subClassOf Restriction      "Scope1 이면 값이 S1"  → 분류 **안 됨**
    equivalentClass Restriction "값이 S1 이면 Scope1"  → 분류 **됨**

`reasonable` (S8 기본 엔진) 은 `equivalentClass` 로만 분류하고, `owlrl` 은 둘 다 하지
않는다. 즉 스텝 docstring 이 약속한 *"OWL RL 추론기가 hasValue restriction 을 기반으로
인스턴스를 분류한다"* 가 **한 번도 실행되지 않았다.** 이 리포는 같은 유형을 이미 겪었다 —
OWL RL 이 propertyChainAxiom 을 무시해 주입해도 파생 0건이었다.

end-to-end 실측 (실제 A-Box 20건 주입 후 reasonable):

    Scope1Emission 8건 / Scope2Emission 3건 / Scope3Emission 9건

## 이 테스트의 방향

"Restriction 이 생겼다" 만 주장하면 방향이 틀려도 통과한다 — 그것이 정확히 3회 실행
동안의 상태였다. 세 축을 함께 고정한다:

* DP 해석 — 하드코딩 이름이 없어도 실재 DP 를 찾고, **못 찾으면 만들지 않는다**
* 방향 — `equivalentClass` 여야 한다 (`subClassOf` 는 승격된다)
* 효과 — **추론기가 실제로 분류하는가** (형태만 맞고 분류 안 되면 무의미)
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.quality_steps import step_16_scope_emission as s16
from tools.quality_steps._base import StepContext

NS = Namespace(str(DOMAIN_NS))


def _ctx() -> StepContext:
    return StepContext(domain_ns=str(DOMAIN_NS))


def _base_graph(dp_name: str = "ghgEmissionScopeType") -> Graph:
    """Scope 클래스 3개 + class-specific DP 를 가진 최소 T-Box."""
    g = Graph()
    bind_namespaces(g)
    g.add((NS["GHGEmission"], RDF.type, OWL.Class))
    for cls in ("Scope1Emission", "Scope2Emission", "Scope3Emission"):
        g.add((NS[cls], RDF.type, OWL.Class))
        g.add((NS[cls], RDFS.subClassOf, NS["GHGEmission"]))
    g.add((NS[dp_name], RDF.type, OWL.DatatypeProperty))
    g.add((NS[dp_name], RDFS.domain, NS["GHGEmission"]))
    return g


def _restrictions(g: Graph, cls: str, pred) -> list[tuple[list[str], list[str]]]:
    out = []
    for node in g.objects(NS[cls], pred):
        if (node, RDF.type, OWL.Restriction) not in g:
            continue
        props = [str(p).split("#")[-1] for p in g.objects(node, OWL.onProperty)]
        vals = [str(v) for v in g.objects(node, OWL.hasValue)]
        out.append((props, vals))
    return out


# ── DP 해석 ─────────────────────────────────────────────────────────────


def test_class_specific_dp_is_resolved():
    """THE REGRESSION: 하드코딩 ``scopeType`` 이 없어도 실재 DP 를 찾는다."""
    g = _base_graph()
    result = s16.apply(g, _ctx())
    assert result.stats["scope_dp_resolution"] == (
        "domain=GHGEmission:ghgEmissionScopeType"
    )
    assert result.stats["scope_restrictions_added"] == 3


def test_legacy_dp_name_still_works():
    """구 도메인 호환 — ``scopeType`` 이 실재하면 그것을 쓴다."""
    g = _base_graph(dp_name="scopeType")
    result = s16.apply(g, _ctx())
    assert result.stats["scope_dp_resolution"] == "legacy:scopeType"
    assert _restrictions(g, "Scope1Emission", OWL.equivalentClass) == [
        (["scopeType"], ["Scope1"]),
    ]


def test_dp_without_domain_is_found_by_name():
    """``rdfs:domain`` 이 없어도 이름으로 찾는다 (domain 미선언 T-Box 대비)."""
    g = _base_graph()
    g.remove((NS["ghgEmissionScopeType"], RDFS.domain, NS["GHGEmission"]))
    result = s16.apply(g, _ctx())
    assert result.stats["scope_dp_resolution"] == "name-only:ghgEmissionScopeType"


def test_missing_dp_creates_nothing():
    """DP 를 못 찾으면 **Restriction 을 만들지 않는다.**

    없는 DP 를 가리키는 Restriction 이 정확히 이번 결함이었다 — 만들어 놓고 추론이
    아무것도 분류하지 못하면 "적용됨" 으로 보고되면서 클래스는 비어 있다.
    """
    g = _base_graph(dp_name="somethingElse")
    result = s16.apply(g, _ctx())
    assert result.stats["scope_dp_resolution"] == "not-found"
    assert result.stats["scope_restrictions_added"] == 0
    assert result.triples_delta == 0
    assert _restrictions(g, "Scope1Emission", OWL.equivalentClass) == []


# ── 방향: equivalentClass 여야 한다 ─────────────────────────────────────


def test_restriction_is_attached_as_equivalent_class():
    """``equivalentClass`` 로 붙는다 — ``subClassOf`` 로는 추론 분류가 안 된다."""
    g = _base_graph()
    s16.apply(g, _ctx())
    assert _restrictions(g, "Scope2Emission", OWL.equivalentClass) == [
        (["ghgEmissionScopeType"], ["Scope2"]),
    ]
    assert _restrictions(g, "Scope2Emission", RDFS.subClassOf) == [], (
        "subClassOf 에 Restriction 이 남아 있다 (분류 불가 형태)"
    )


def test_parent_subclass_link_is_preserved():
    """부모 관계는 유지된다 — 승격이 계층을 끊으면 안 된다."""
    g = _base_graph()
    s16.apply(g, _ctx())
    parents = {str(o).split("#")[-1] for o in g.objects(NS["Scope1Emission"], RDFS.subClassOf)
               if isinstance(o, URIRef)}
    assert "GHGEmission" in parents


def test_existing_subclass_restriction_is_promoted():
    """이미 ``subClassOf`` 로 붙은 구 산출물을 ``equivalentClass`` 로 옮긴다."""
    g = _base_graph()
    for cls, val in (("Scope1Emission", "Scope1"), ("Scope2Emission", "Scope2"),
                     ("Scope3Emission", "Scope3")):
        node = BNode()
        g.add((node, RDF.type, OWL.Restriction))
        g.add((node, OWL.onProperty, NS["ghgEmissionScopeType"]))
        g.add((node, OWL.hasValue, Literal(val)))
        g.add((NS[cls], RDFS.subClassOf, node))
    result = s16.apply(g, _ctx())
    assert result.stats["scope_promoted_to_equivalent"] == 3
    assert result.stats["scope_restrictions_added"] == 0, "승격 대신 중복 생성했다"
    assert _restrictions(g, "Scope3Emission", RDFS.subClassOf) == []


def test_idempotent():
    """두 번 돌려도 중복이 생기지 않는다 (S3 는 재실행된다)."""
    g = _base_graph()
    s16.apply(g, _ctx())
    n1 = len(g)
    second = s16.apply(g, _ctx())
    assert len(g) == n1
    assert second.stats["scope_restrictions_added"] == 0
    assert second.stats["scope_promoted_to_equivalent"] == 0


def test_variant_value_is_normalised():
    """``Scope 1`` 같은 공백 변형을 CSV 표기로 정규화한다 (기존 기능 보존)."""
    g = _base_graph()
    node = BNode()
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, NS["ghgEmissionScopeType"]))
    g.add((node, OWL.hasValue, Literal("Scope 1")))
    g.add((NS["Scope1Emission"], OWL.equivalentClass, node))
    result = s16.apply(g, _ctx())
    assert result.stats["scope_hasvalue_normalized"] == 1
    assert _restrictions(g, "Scope1Emission", OWL.equivalentClass) == [
        (["ghgEmissionScopeType"], ["Scope1"]),
    ]


def test_absent_scope_class_is_skipped():
    """Scope 클래스가 없는 도메인에서는 아무것도 만들지 않는다 (도메인 중립)."""
    g = Graph()
    bind_namespaces(g)
    g.add((NS["GHGEmission"], RDF.type, OWL.Class))
    g.add((NS["ghgEmissionScopeType"], RDF.type, OWL.DatatypeProperty))
    result = s16.apply(g, _ctx())
    assert result.stats["scope_restrictions_added"] == 0


# ── 효과: 추론기가 실제로 분류하는가 ────────────────────────────────────


def test_reasoner_actually_classifies_instances():
    """**형태가 아니라 효과** 를 확인한다 — 추론기가 값으로 개체를 분류하는가.

    이 축이 없으면 "Restriction 이 생겼다" 만 보고 3회 실행처럼 0건인 상태를 통과시킨다.
    이 리포는 OWL RL 이 propertyChainAxiom 을 무시해 파생 0건이었던 이력이 있고,
    그때 픽스처가 **공리 존재만 ASK** 해서 통과했다.
    """
    reasonable = pytest.importorskip("reasonable")
    g = _base_graph()
    s16.apply(g, _ctx())
    # 실제 개체 3개 — 각 scope 값 하나씩
    for i, val in enumerate(("Scope1", "Scope2", "Scope3"), start=1):
        inst = NS[f"emission{i}"]
        g.add((inst, RDF.type, NS["GHGEmission"]))
        g.add((inst, NS["ghgEmissionScopeType"], Literal(val)))

    engine = reasonable.PyReasoner()
    engine.from_graph(g)
    inferred = Graph()
    for triple in engine.reason():
        inferred.add(triple)

    for i, cls in enumerate(("Scope1Emission", "Scope2Emission", "Scope3Emission"), start=1):
        assert (NS[f"emission{i}"], RDF.type, NS[cls]) in inferred, (
            f"{cls} 로 분류되지 않았다 — Restriction 형태는 맞지만 추론이 안 된다"
        )


def test_subclass_form_does_not_classify():
    """``subClassOf`` 형태로는 분류되지 않음을 **명시적으로 고정**한다.

    이 사실이 승격(promotion)의 존재 이유다. 누가 다시 subClassOf 로 바꾸면 이
    테스트가 그 선택의 결과를 보여준다.
    """
    reasonable = pytest.importorskip("reasonable")
    g = _base_graph()
    node = BNode()
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, NS["ghgEmissionScopeType"]))
    g.add((node, OWL.hasValue, Literal("Scope1")))
    g.add((NS["Scope1Emission"], RDFS.subClassOf, node))     # 승격 없이 그대로
    inst = NS["emissionX"]
    g.add((inst, RDF.type, NS["GHGEmission"]))
    g.add((inst, NS["ghgEmissionScopeType"], Literal("Scope1")))

    engine = reasonable.PyReasoner()
    engine.from_graph(g)
    inferred = Graph()
    for triple in engine.reason():
        inferred.add(triple)
    assert (inst, RDF.type, NS["Scope1Emission"]) not in inferred, (
        "subClassOf 로도 분류된다면 승격이 불필요하다 — 이 테스트의 전제를 재확인하라"
    )
