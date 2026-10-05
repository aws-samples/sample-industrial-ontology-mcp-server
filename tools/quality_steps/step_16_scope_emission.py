"""Step 16 — Scope Emission hasValue Restriction 추가 + 중복/변형 정규화.

본문 ontology_quality.py 의 Step 16 블록을 그대로 모듈로 옮김.

GHGEmission.scopeType 값에 따라 Scope1/2/3Emission 으로 자동 분류. OWL RL
추론기가 hasValue restriction 을 기반으로 인스턴스를 분류한다.

Multi-Agent 가 ``Scope 1`` 같은 공백 포함 변형을 만들면 CSV (``Scope1``) 와
두 값이 공존하여 FunctionalProperty 위반. CSV 표기 (공백 없음) 만 유지
하도록 기존 변형 값은 제거 + 신규 restriction 생성.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


_SCOPE_MAP = {
    "Scope1Emission": "Scope1",
    "Scope2Emission": "Scope2",
    "Scope3Emission": "Scope3",
}


def _resolve_scope_type_dp(g: Graph, domain_ns_obj) -> tuple[object, str]:
    """``scopeType`` 역할을 하는 **실재 DP** 를 T-Box 에서 찾는다.

    ## 왜 하드코딩을 못 쓰는가 (2026-08-27 실측)

    이 스텝은 ``DOMAIN_NS_OBJ["scopeType"]`` 을 썼다. 그런데 Path B(class-specific DP)
    전환 이후 실제 이름은 ``ghgEmissionScopeType`` 이고 ``scopeType`` 은 **T-Box 에
    존재하지 않는다.** 그래서 이 스텝이 만든 Restriction 은 아무 데이터도 가리키지
    못했고, 결과적으로 ``Scope1/2/3Emission`` 은 ``subClassOf GHGEmission`` **하나만**
    가진 빈 클래스로 남았다:

        T-Box 선언       Scope1Emission  (hasValue Restriction **없음**)
        A-Box 인스턴스   0건
        추론 결과        0건
        CSV 실데이터     GHG_Emission.Scope_Type = Scope1 50 / Scope2 40 / Scope3 30
        실제 DP          ghgEmissionScopeType  (A-Box 120 트리플)

    docstring 이 약속한 "OWL RL 추론기가 hasValue restriction 을 기반으로 인스턴스를
    분류한다" 가 **한 번도 실행되지 않았다.** 값은 CSV 에 있고 DP 도 있는데 이름 한 개가
    어긋나 전 경로가 죽었다 — 이 리포가 반복 겪은 "리터럴 이름으로 조회해 게이트가
    0건이 됐다" 와 같은 계열이다 (value_ranges 게이트 7규칙 전량 0건 사고).

    ## 해석 순서

    1. 하드코딩 이름(``scopeType``)이 실재하면 그것 (구 도메인 호환)
    2. ``rdfs:domain`` 이 ``GHGEmission`` 인 DP 중 이름이 ``scopetype`` 으로 끝나는 것
    3. 이름이 ``scopetype`` 으로 끝나는 DP (domain 미선언 대비)

    찾지 못하면 ``(None, 사유)`` — 없는 이름으로 Restriction 을 만들면 그것이 정확히
    이번 결함이므로, **만들지 않고 사유를 보고한다.**
    """
    from rdflib import RDFS as _RDFS

    legacy = domain_ns_obj["scopeType"]
    if (legacy, RDF.type, OWL.DatatypeProperty) in g:
        return legacy, "legacy:scopeType"

    ghg = domain_ns_obj["GHGEmission"]
    tail = "scopetype"
    scoped = [
        p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
        if str(p).split("#")[-1].lower().endswith(tail)
        and (p, _RDFS.domain, ghg) in g
    ]
    if scoped:
        chosen = sorted(scoped, key=lambda p: str(p))[0]
        return chosen, f"domain=GHGEmission:{str(chosen).split('#')[-1]}"

    loose = [
        p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
        if str(p).split("#")[-1].lower().endswith(tail)
    ]
    if loose:
        chosen = sorted(loose, key=lambda p: str(p))[0]
        return chosen, f"name-only:{str(chosen).split('#')[-1]}"
    return None, "not-found"


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import DOMAIN_NS_OBJ

    before = len(g)
    scope_added = 0
    scope_normalized = 0
    scope_promoted = 0
    scope_type_prop, resolution = _resolve_scope_type_dp(g, DOMAIN_NS_OBJ)
    if scope_type_prop is None:
        # 없는 DP 로 Restriction 을 만들면 추론이 아무것도 분류하지 못한다 —
        # 그것이 정확히 이 스텝이 3회 실행 동안 한 일이었다. 만들지 않고 보고한다.
        logger.warning(
            "Step 16: scopeType 역할 DatatypeProperty 를 T-Box 에서 찾지 못했다 — "
            "hasValue Restriction 을 만들지 않는다 (없는 DP 를 가리키면 추론이 "
            "인스턴스를 분류하지 못하고 Scope1/2/3Emission 이 빈 클래스로 남는다)",
        )
        return StepResult(
            name="step_16_scope_emission",
            stats={
                "scope_restrictions_added": 0,
                "scope_hasvalue_normalized": 0,
                "scope_dp_resolution": resolution,
            },
            triples_delta=0,
            step_number=16,
            step_label="scope_emission_restrictions",
        )

    for scope_cls, scope_val in _SCOPE_MAP.items():
        cls_uri = DOMAIN_NS_OBJ[scope_cls]
        if (cls_uri, RDF.type, OWL.Class) not in g:
            continue

        # 기존 Restriction 은 subClassOf / equivalentClass 양쪽에서 찾는다 (구 산출물
        # 호환). 값 정규화는 위치와 무관하게 적용한다.
        existing_restrictions = [
            o for pred in (RDFS.subClassOf, OWL.equivalentClass)
            for o in g.objects(cls_uri, pred)
            if (o, OWL.onProperty, scope_type_prop) in g
            and any(g.objects(o, OWL.hasValue))
        ]
        for rst in existing_restrictions:
            for v in list(g.objects(rst, OWL.hasValue)):
                if isinstance(v, Literal) and str(v) != scope_val:
                    g.remove((rst, OWL.hasValue, v))
                    scope_normalized += 1
            if not any(g.objects(rst, OWL.hasValue)):
                g.add((rst, OWL.hasValue, Literal(scope_val)))

        # ``subClassOf`` 로 붙은 것은 ``equivalentClass`` 로 **옮긴다.**
        #
        # 방향이 결정적이다 (2026-08-27 최소 예제로 실측):
        #
        #   subClassOf Restriction      "Scope1 이면 값이 S1"  → 분류 **안 됨**
        #   equivalentClass Restriction "값이 S1 이면 Scope1"  → 분류 **됨**
        #
        # 이 스텝 docstring 은 "OWL RL 추론기가 hasValue restriction 을 기반으로
        # 인스턴스를 분류한다" 고 약속했지만, ``subClassOf`` 방향으로는 추론기가
        # 역방향 분류를 하지 않는다. `reasonable` (S8 기본 엔진) 과 `owlrl` 양쪽에서
        # 확인했다 — 전자는 equivalentClass 로만 분류하고, 후자는 둘 다 안 한다.
        # 그래서 Scope1/2/3Emission 은 A-Box 0건 / 추론 0건의 빈 클래스로 남았다.
        for rst in list(g.objects(cls_uri, RDFS.subClassOf)):
            if (rst, OWL.onProperty, scope_type_prop) not in g:
                continue
            if not any(g.objects(rst, OWL.hasValue)):
                continue
            g.remove((cls_uri, RDFS.subClassOf, rst))
            g.add((cls_uri, OWL.equivalentClass, rst))
            scope_promoted += 1

        has_restriction = any(
            (o, OWL.onProperty, scope_type_prop) in g
            and any(g.objects(o, OWL.hasValue))
            for o in g.objects(cls_uri, OWL.equivalentClass)
        )
        if not has_restriction:
            restriction = BNode()
            g.add((restriction, RDF.type, OWL.Restriction))
            g.add((restriction, OWL.onProperty, scope_type_prop))
            g.add((restriction, OWL.hasValue, Literal(scope_val)))
            # equivalentClass 로 붙인다 — 추론기가 값으로 인스턴스를 분류할 수 있는
            # 유일한 형태다. 부모 관계(subClassOf GHGEmission)는 별도로 유지된다.
            g.add((cls_uri, OWL.equivalentClass, restriction))
            scope_added += 1

    return StepResult(
        name="step_16_scope_emission",
        stats={
            "scope_restrictions_added": scope_added,
            "scope_hasvalue_normalized": scope_normalized,
            # subClassOf → equivalentClass 로 옮긴 수. 이 값이 >0 이면 구 산출물이
            # 분류 불가 형태였다는 뜻이다.
            "scope_promoted_to_equivalent": scope_promoted,
            # 어느 DP 로 해석됐는지 남긴다. 이 값이 없으면 "Restriction 을 만들었다" 와
            # "쓸모없는 DP 를 가리켰다" 를 구분할 수 없다 — 3회 실행 동안 후자였다.
            "scope_dp_resolution": resolution,
        },
        triples_delta=len(g) - before,
        step_number=16,
        step_label="scope_emission_restrictions",
    )
