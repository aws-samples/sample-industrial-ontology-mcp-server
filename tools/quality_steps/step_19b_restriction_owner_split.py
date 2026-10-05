"""Step 19b — 여러 클래스가 공유하는 named restriction 을 클래스별로 분리.

Architect 가 동일한 ``owl:Restriction`` BNode 를 여러 클래스의 ``rdfs:subClassOf``
에 재사용하면, 스콜렘화(Step 19) 는 **첫 번째로 찾은 클래스 이름** 으로 URI 를
만든다. 그 결과 나머지 클래스는 남의 이름표를 상속받고, 자기 것이 아닌 제약을
강요받는다. 2026-07-25 실측:

    steel:MaterialSpecA_operationStartDatetime_minCardinality
      ← MaterialSpecA (이름 주인)
      ← MaterialB        (남의 제약을 상속)

``MaterialB`` 은 ``operationStartDatetime`` 값이 없는 인스턴스가 많아
``validate_kg`` 의 카디널리티 체크가 FAIL 했다. 정작 이 제약은 MaterialB 을 위해
선언된 것이 아니다.

Step 19 자체도 BNode 공유를 클래스별 복제로 처리하도록 고쳤다(근본). 이 단계는
**이미 스콜렘화가 끝난 T-Box** (named restriction 이 공유된 상태) 를 복구하고,
다른 경로로 같은 공유가 생겼을 때의 안전망이다.

분리 규칙: restriction 이름의 클래스 접두와 실제 소유 클래스가 다르면, 그 소유
클래스 이름으로 restriction 을 복제해 붙이고 원본 참조를 끊는다. 이름 주인
(접두와 일치하는 클래스) 은 그대로 둔다. 접두를 판별할 수 없으면(클래스 이름
패턴이 아니면) 건드리지 않는다.

Step 20(restriction dedup) **직전** 에 실행해야 한다 — dedup 은 의미가 같은
restriction URI 를 하나로 합치므로, 분리를 나중에 하면 상쇄된다.
"""
from __future__ import annotations

import logging

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_RESTRICTION_TYPES = (
    "someValuesFrom", "hasValue", "allValuesFrom",
    "minCardinality", "maxCardinality",
    "minQualifiedCardinality", "maxQualifiedCardinality",
    "qualifiedCardinality",
)


def _restriction_type_of(g: Graph, restriction: URIRef) -> str:
    """restriction 의 종류 문자열 (someValuesFrom / minCardinality …)."""
    for rtype in _RESTRICTION_TYPES:
        if g.value(restriction, OWL[rtype]) is not None:
            return rtype
    return "Restriction"


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """이름 주인이 아닌 클래스의 restriction 참조를 자기 이름 사본으로 교체."""
    before = len(g)
    steel_str = ctx.domain_ns

    # 클래스 local name 집합 — restriction 이름의 접두를 판별하는 데 쓴다.
    class_names = {
        str(cls)[len(steel_str):]
        for cls in g.subjects(RDF.type, OWL.Class)
        if isinstance(cls, URIRef) and str(cls).startswith(steel_str)
    }

    split: list[dict] = []
    for restriction in list(g.subjects(RDF.type, OWL.Restriction)):
        if not (isinstance(restriction, URIRef)
                and str(restriction).startswith(steel_str)):
            continue
        prop = g.value(restriction, OWL.onProperty)
        if prop is None:
            continue

        owners = [
            (owner, pred)
            for pred in (RDFS.subClassOf, OWL.equivalentClass)
            for owner in g.subjects(pred, restriction)
            if isinstance(owner, URIRef) and str(owner).startswith(steel_str)
        ]
        if len(owners) < 2:
            continue

        local = str(restriction)[len(steel_str):]
        # 이름 주인 = local name 이 "{클래스}_" 로 시작하는 클래스. 접두 후보가
        # 여러 개면 가장 긴 것을 택한다 (MaterialASpec vs MaterialSpecA).
        candidates = sorted(
            (c for c in class_names if local.startswith(c + "_")),
            key=len, reverse=True,
        )
        if not candidates:
            continue  # 이름에서 주인을 판별할 수 없으면 건드리지 않는다
        name_owner = candidates[0]

        body = list(g.predicate_objects(restriction))
        prop_local = str(prop)[len(steel_str):] if str(prop).startswith(steel_str) \
            else str(prop).split("#")[-1]
        rtype = _restriction_type_of(g, restriction)

        for owner, pred in owners:
            owner_local = str(owner)[len(steel_str):]
            if owner_local == name_owner:
                continue  # 이름 주인은 그대로
            clone = URIRef(steel_str + f"{owner_local}_{prop_local}_{rtype}")
            for p, o in body:
                g.add((clone, p, o))
            g.remove((owner, pred, restriction))
            g.add((owner, pred, clone))
            split.append({
                "borrower": owner_local,
                "original": local,
                "clone": str(clone)[len(steel_str):],
            })

    if split:
        logger.warning(
            "Step 19b: 남의 restriction 을 상속하던 클래스 %d건을 자기 사본으로 "
            "분리 — %s",
            len(split),
            [f"{s['borrower']} → {s['clone']}" for s in split[:5]],
        )

    return StepResult(
        name="step_19b_restriction_owner_split",
        stats={
            "restrictions_split": len(split),
            "split_detail": split[:20],
        },
        triples_delta=len(g) - before,
        step_number="19b",
        step_label="restriction_owner_split",
    )
