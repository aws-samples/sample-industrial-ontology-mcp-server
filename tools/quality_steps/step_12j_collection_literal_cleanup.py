"""Step 12j — RDF 리스트 자리에 새어든 **Turtle 컬렉션 문자열** 정리.

``owl:disjointUnionOf`` / ``owl:members`` / ``owl:unionOf`` 등은 object 가 **RDF
리스트** 여야 한다. 그런데 LLM 이 문법적으로 올바른 Turtle 컬렉션 표기를 보내면
``add_triple`` 경로가 그것을 **평문 리터럴** 로 저장했다.

실측 (2026-08-30 배포 T-Box) — ``owl:disjointUnionOf`` 17건 중 **7건이 리터럴**::

    steel:EquipmentStatus
        owl:disjointUnionOf ( steel:MaintenanceEquipmentStatus … ) ,          ← 정상
        "(steel:MaintenanceEquipmentStatus steel:RunningEquipmentStatus …)"   ← 오염
            ^^xsd:string ;

같은 부모에 정상 리스트와 문자열이 **공존**했고 jury 는 ``applied`` 를 보고했다.

## 왜 치명적인가

OWL 상 리스트가 아니므로 그 공리는 추론에 **아무 영향이 없다** — 즉 "분류 완전성을
선언했다" 고 믿는데 실제로는 선언되지 않은 상태다. 반대로 값이 남아 있으면
``rdf:List`` 를 기대하는 소비자(추론기·SHACL·시각화)가 타입 오류를 만나거나 조용히
건너뛴다.

## 왜 게이트가 못 잡았나

``check_quality_rules`` 는 ``disjointUnionOf`` 를 읽지 않고, ``validate_tbox_shacl``
의 shape 도 이 술어를 다루지 않는다. HermiT 은 리터럴 object 를 **무시**하므로
consistent 를 반환한다. 즉 세 게이트 모두 침묵했다.

## 정리 방식 — 정상 리스트가 있으면 버리고, 없으면 복구한다

같은 (주어, 술어) 에 **정상 RDF 리스트가 이미 있으면** 리터럴은 잉여이므로 제거만
한다 (실측 7건 전부 이 경우다). 리스트가 없으면 문자열을 파싱해 리스트로 **복구**
한다 — 지우기만 하면 LLM 의 의도가 사라진다.

복구 시 멤버 이름이 T-Box 에 선언돼 있는지 확인한다. 선언되지 않은 이름이 섞이면
유령 노드가 리스트에 들어가 :mod:`tools.validation_support.checks.semantic` 의
스키마 참조 무결성이 잡는 상태가 된다 — 복구가 새 결함을 만들지 않게 한다.

생성 지점(``domain.graph_utils.parse_object_term``)은 같은 커밋에서 컬렉션 표기를
해석하도록 고쳤다. 이 스텝은 **이미 오염된 산출물** 과 앞으로 유입될 외부 TTL 을
위한 그물이다 (``step_12d2`` 와 같은 구조).
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: object 가 RDF 리스트여야 하는 술어. 리터럴이 오면 오염이다.
_LIST_VALUED_PREDICATES = (
    OWL.disjointUnionOf,
    OWL.members,
    OWL.unionOf,
    OWL.intersectionOf,
    OWL.oneOf,
    OWL.propertyChainAxiom,
    OWL.hasKey,
    OWL.distinctMembers,
)


def _parse_collection_text(text: str, g: Graph, domain_ns: str) -> list[URIRef] | None:
    """``"(steel:A steel:B)"`` → ``[URIRef, URIRef]``. 해석 불가면 None.

    **선언된 엔티티만** 받는다. 미선언 이름이 섞이면 복구가 유령 노드를 리스트에
    넣어 새 결함을 만든다 — 그때는 복구를 포기하고 리터럴만 지운다 (호출부 판단).
    """
    from domain.graph_utils import resolve_entity_name

    inner = text.strip()
    if not (inner.startswith("(") and inner.endswith(")")):
        return None
    inner = inner[1:-1].strip()
    if not inner:
        return None
    out: list[URIRef] = []
    for token in inner.replace(",", " ").split():
        node = resolve_entity_name(token, g)
        if not isinstance(node, URIRef):
            return None
        # 도메인 엔티티는 선언돼 있어야 한다 (외래 IRI 는 그대로 신뢰).
        if str(node).startswith(domain_ns):
            declared = (
                (node, RDF.type, OWL.Class) in g
                or (node, RDF.type, OWL.ObjectProperty) in g
                or (node, RDF.type, OWL.DatatypeProperty) in g
            )
            if not declared:
                return None
        out.append(node)
    return out or None


def _cleanup(g: Graph, domain_ns: str) -> dict:
    import rdflib.collection

    removed = 0
    repaired = 0
    unrepairable = 0
    samples: list[str] = []

    for predicate in _LIST_VALUED_PREDICATES:
        for subject, obj in list(g.subject_objects(predicate)):
            if not isinstance(obj, Literal):
                continue
            text = str(obj)
            # 같은 (주어, 술어) 에 정상 리스트가 이미 있는가.
            has_list = any(
                not isinstance(other, Literal)
                for other in g.objects(subject, predicate)
            )
            g.remove((subject, predicate, obj))
            removed += 1
            local = str(subject).replace(domain_ns, "")
            pred_local = str(predicate).rsplit("#", 1)[-1]
            if has_list:
                if len(samples) < 10:
                    samples.append(f"{local} {pred_local}: 잉여 리터럴 제거")
                continue
            members = _parse_collection_text(text, g, domain_ns)
            if not members:
                unrepairable += 1
                if len(samples) < 10:
                    samples.append(
                        f"{local} {pred_local}: 복구 불가 (미선언 멤버 또는 "
                        f"파싱 실패) — 리터럴만 제거: {text[:60]}",
                    )
                continue
            head = rdflib.term.BNode()
            rdflib.collection.Collection(g, head, members)
            g.add((subject, predicate, head))
            repaired += 1
            if len(samples) < 10:
                samples.append(
                    f"{local} {pred_local}: 리스트로 복구 ({len(members)} 멤버)",
                )

    if removed:
        logger.warning(
            "Step 12j: RDF 리스트 자리의 문자열 %d건 정리 (복구 %d / 잉여 제거 %d "
            "/ 복구불가 %d) — 리터럴은 OWL 상 리스트가 아니므로 그 공리가 추론에 "
            "아무 영향을 주지 않는다. 어떤 게이트도 이것을 읽지 않았다. 예: %s",
            removed, repaired, removed - repaired - unrepairable,
            unrepairable, samples[:5],
        )
    return {
        "collection_literals_removed": removed,
        "collection_literals_repaired": repaired,
        "collection_literals_unrepairable": unrepairable,
        "collection_literal_samples": samples,
    }


def _prune_uncovered_partitions(g: Graph, domain_ns: str) -> dict:
    """CSV 실값을 덮지 못하는 ``owl:disjointUnionOf`` 파티션을 제거한다.

    ``step_28`` 의 **생성 지점** 가드(``_partition_value_gap``)는 config 가 선언한
    그룹에만 적용된다. 그런데 실측 배포 T-Box 의 파티션 10건 중 **8건이 config 밖**
    이었다 — S2 초안이나 Jury ``add_triple`` 이 직접 주입한 것이다::

        config 허용: ManufacturingProcessStep, GHGEmission
        T-Box 실재: + EquipmentStatus, InventoryTransaction, MaintenanceHistory,
                     NDTResults, NoiseVibrationMonitoring, RealTimeData,
                     TagMaster, WasteManagement

    "생성 지점 가드는 신규 쓰기만 막는다" 가 이 리포에 기록된 교훈이다 — 사후 정리
    스텝이 함께 있어야 한다.

    **출처로 판정하지 않고 근거로 판정한다.** config 허용 목록으로 자르면 SME 가
    config 를 갱신할 때까지 정당한 파티션도 막히고, 반대로 config 에 있으면 값이
    어긋나도 통과한다. 두 경우 모두 틀리므로 ``step_28`` 과 **같은 값 커버리지
    판정**을 쓴다 — 커버하면 남기고 못 하면 지운다.

    실측 결과: 8건 중 ``NoiseVibrationMonitoring`` 은 CSV 값(Noise/Vibration)을
    완전히 덮으므로 **살아남고**, 나머지 7건이 제거된다.
    """
    from tools.ontology_quality import _partition_value_gap

    removed = 0
    kept = 0
    samples: list[str] = []
    for subject, obj in list(g.subject_objects(OWL.disjointUnionOf)):
        if isinstance(obj, Literal):
            continue  # 위 _cleanup 이 처리한다
        members = [m for m in g.items(obj) if isinstance(m, URIRef)]
        if len(members) < 2:
            continue
        parent_local = str(subject).replace(domain_ns, "")
        try:
            gap = _partition_value_gap(g, parent_local, members)
        except Exception as exc:  # noqa: BLE001 — 판정 실패 시 보존 (안전 쪽)
            logger.debug("step_12j: 파티션 판정 실패 (%s): %s", parent_local, exc)
            continue
        if not gap:
            kept += 1
            continue
        g.remove((subject, OWL.disjointUnionOf, obj))
        removed += 1
        if len(samples) < 10:
            samples.append(
                f"{parent_local}: 공리={gap['claimed_values']} vs "
                f"CSV 미포함={gap['uncovered_values']} ({gap['uncovered_rows']}행)",
            )

    if removed:
        logger.warning(
            "Step 12j: CSV 값을 덮지 못하는 disjointUnionOf 파티션 %d건 제거 "
            "(유지 %d) — 파티션은 '부모 인스턴스가 정확히 한 자식에 속한다' 를 "
            "주장하므로, 정의 공리가 놓친 값을 가진 개체는 OWL DL 상 모순이 되고 "
            "inconsistent 온톨로지에서는 어떤 entailment 도 신뢰할 수 없다. "
            "값 어휘는 SME 가 확정해야 한다. 예: %s",
            removed, kept, samples[:5],
        )
    return {
        "uncovered_partitions_removed": removed,
        "partitions_kept": kept,
        "uncovered_partition_samples": samples,
    }


def _prune_uncovered_union_closures(g: Graph, domain_ns: str) -> dict:
    """``parent owl:equivalentClass Union_X (unionOf children)`` — 두 번째 닫힘.

    파티션(``disjointUnionOf``) 을 지워도 부모가 여전히 닫혀 있는 경우가 있다.
    실측: ``EquipmentStatus owl:equivalentClass Union_EquipmentStatus_44b2231a`` 이고
    그 Union 은 같은 자식 3개의 ``owl:unionOf`` 다 — 부모를 자식들의 합집합과
    **동등**으로 선언하므로 ``Standby`` 개체(자식 어디에도 안 맞음)가 여전히 모순이다.

    파티션 제거 후에도 ``EquipmentStatus`` / ``WasteManagement`` 가 INCONSISTENT 였던
    이유가 이것이다 (2026-08-30 실측). 즉 **닫힘이 두 겹**이었고 한 겹만 걷으면
    증상이 그대로 남는다 — "수정이 다음 결함을 만든다" 가 아니라 "수정이 절반만
    닿았다" 는 형태다.

    Union_* 의 ``skos:scopeNote`` 는 "추상 카테고리 — 자체 판별 속성 없이 그룹 부모
    역할만 수행" 이라고 적혀 있는데, ``equivalentClass + unionOf`` 는 그것과 반대로
    **닫힌 정의**다. 주석과 공리가 어긋나 있다.

    ``disjointUnionOf`` 와 **같은 값 커버리지 판정**을 쓴다 (사본 금지). 커버하면
    남기고 못 하면 ``equivalentClass`` 링크만 끊는다 — Union 클래스 자체와 자식
    ``subClassOf`` 는 보존하므로 계층·질의 경로는 유지된다.
    """
    from tools.ontology_quality import _partition_value_gap

    removed = 0
    kept = 0
    samples: list[str] = []
    for parent, union_cls in list(g.subject_objects(OWL.equivalentClass)):
        if not (isinstance(parent, URIRef) and isinstance(union_cls, URIRef)):
            continue
        union_lists = list(g.objects(union_cls, OWL.unionOf))
        if not union_lists:
            continue
        members = [
            m for lst in union_lists for m in g.items(lst) if isinstance(m, URIRef)
        ]
        if len(members) < 2:
            continue
        parent_local = str(parent).replace(domain_ns, "")
        try:
            gap = _partition_value_gap(g, parent_local, members)
        except Exception as exc:  # noqa: BLE001 — 판정 실패 시 보존 (안전 쪽)
            logger.debug("step_12j: union 판정 실패 (%s): %s", parent_local, exc)
            continue
        if not gap:
            kept += 1
            continue
        g.remove((parent, OWL.equivalentClass, union_cls))
        removed += 1
        if len(samples) < 10:
            samples.append(
                f"{parent_local} ≡ {str(union_cls).replace(domain_ns, '')}: "
                f"CSV 미포함={gap['uncovered_values']} ({gap['uncovered_rows']}행)",
            )

    if removed:
        logger.warning(
            "Step 12j: 값을 덮지 못하는 union 닫힘 %d건 해제 (유지 %d) — "
            "parent ≡ unionOf(children) 은 파티션과 같은 닫힘이므로 "
            "disjointUnionOf 만 지우면 모순이 그대로 남는다. Union 클래스와 "
            "자식 subClassOf 는 보존한다. 예: %s",
            removed, kept, samples[:5],
        )
    return {
        "uncovered_union_closures_removed": removed,
        "union_closures_kept": kept,
        "uncovered_union_closure_samples": samples,
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats = _cleanup(g, ctx.domain_ns)
    stats.update(_prune_uncovered_partitions(g, ctx.domain_ns))
    stats.update(_prune_uncovered_union_closures(g, ctx.domain_ns))
    return StepResult(
        name="step_12j_collection_literal_cleanup",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="12j",
        step_label="collection_literal_cleanup",
    )
