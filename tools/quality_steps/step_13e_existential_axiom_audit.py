"""Step 13e — 기존 ``someValuesFrom`` 필수참여 공리를 근거와 대조 (사후 감사).

## 왜 (2026-09-05 실측)

``step_13`` 의 근거 판정(``_fk_evidence_for_op``)은 **자기가 만드는 공리만** 지킨다.
S2(LLM) 가 이미 넣어 온 공리는 그 뒤 아무도 다시 보지 않는다. 실측: 배포 T-Box 의
필수참여 위반 3쌍 / 개체 607건 중 근거 없는 2건이 **S2 출력 스냅샷에 이미 존재**했다
(``t_box_s2out_*.ttl`` 로 확인 — 즉 S3 가 만든 것이 아니다)::

    ProductionResult ⊑ ∃isResultOfProductionPlan.ProductionPlan          위반 521/521
      → Production_Result.csv 에 Plan_ID 컬럼이 없다
    MonitoringPointMaster ⊑ ∃monitoringPointRefersToEquipment.EquipmentMaster  위반 50/50
      → Monitoring_Point_Master.csv 에 Equipment_ID 컬럼이 없다

이 리포에 같은 형태가 기록돼 있다 — "가드는 신규 쓰기만 막는다: **생성 지점 가드 +
사후 정리 스텝 두 벌**이 필요하다". 생성 게이트와 이 감사가 **같은 판정기**를 쓰는
것이 요점이다 (사본을 만들면 두 방향이 갈린다).

왜 게이트가 못 잡았나: HermiT 은 개방세계라 원리적으로 침묵하고, SHACL 동적 shape 은
``sh:minCount`` 를 만들지 않는다. 추론은 위반 개체를 자기가 위반하는 클래스로
타이핑하므로 그 클래스로 필터하는 질의가 **조용히 틀린 결과**를 낸다.

## tacit 축이 없으면 산 공리를 죽인다

CSV 판정기만 그대로 적용하면 제거 후보가 4개가 되는데 그중 ``hasStackEquipment`` 는
**오탐**이다 — CSV 에는 근거가 없지만 tacit 이 1,560/1,560 을 채워 S9 에서 위반이
아니다. ``_fk_evidence_for_op`` 는 domain 에 CSV 가 **있으면** tacit 을 보지 않는다
(그쪽 tacit 축은 ``no_csv_for_domain`` 분기 전용이다). 그래서 이 스텝은 두 축의
**최대값**으로 판정한다. 이 리포는 가드가 A-Box 36,000건 보유 OP 를 억제해 미선언
술어를 만든 사고를 겪었다.

실측 (배포 T-Box, 공리 51개):

===================================== ========= ========== ======
공리                                    CSV 채움   tacit 채움  판정
===================================== ========= ========== ======
``hasStackEquipment``                   0.000     1.000      보존
``monitoringPointRefersToEquipment``    0.000     0.000      제거
``isResultOfProductionPlan``            0.000     0.000      제거
``hasFuelEnergySource``                 0.800     0.000      제거
나머지 47개                              ≥0.999    —          보존
===================================== ========= ========== ======

``hasFuelEnergySource`` 는 모델링 오류가 아니라 **소스 결측**이다 (Energy_Source_ID 가
180행 중 36행 비어 있다). ``someValuesFrom`` 은 "모든 개체가 이 관계를 갖는다" 는
주장이라 한 행이라도 비면 거짓이고 생성 게이트도 같은 임계로 거부하므로, 양방향을
일치시키기 위해 제거한다. 다만 사유를 ``partial_fill`` 로 따로 집계한다 — SME 가
"공리가 맞고 데이터가 틀렸다" 고 판단하면 CSV 를 채운 뒤 다시 돌리면 되살아난다.

## 무엇을 하지 않는가

* 판정 불가는 보존한다. ``_fk_evidence_for_op`` 가 ``no_csv_for_range`` /
  ``empty_csv`` / ``no_pk_candidate_in_range`` 로 ``(True, 1.0)`` 을 돌려주는 경로가
  그것이고, 이 스텝은 그 값을 그대로 통과시킨다.
* OP 선언은 지우지 않는다. 공리를 잃은 OP 가 0행 phantom 으로 남는 것은 별 축이고
  ``step_22e``/``step_22f`` 가 판정한다. 여기서 함께 지우면 두 판정이 섞인다.
* restriction 노드의 트리플은 **다른 참조가 없을 때만** 지운다 (여러 클래스가 공유하는
  restriction 을 반쪽만 지우면 고아 노드가 남는다 — ``step_19b`` 가 소유자를 분리하기
  전 상태에서 실제로 공유가 일어난다).
"""
from __future__ import annotations

import logging
import os

from rdflib import OWL, RDFS, BNode, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: 환경변수 — ``prune``(기본) / ``warn``(측정만) / ``off``(스텝 무효화).
_ENV_MODE = "TBOX_EXISTENTIAL_AUDIT"


def _tacit_row_coverage(
    g: Graph, op: URIRef, owner_cls: URIRef,
) -> tuple[float, str]:
    """tacit 이 이 OP 를 owner 개체 수 대비 얼마나 채우는가.

    분모는 **owner 테이블의 행 수**다 — tacit 주어 수를 분모로 쓰면 "tacit 이 건드린
    것만" 세어 언제나 1.0 이 된다 (``step_13c`` 에서 같은 함정을 고쳤다).
    owner 에 CSV 가 없으면 tacit 이 타입한 개체를 분모로 쓴다 (``step_13`` 의
    ``_tacit_coverage_for_op`` 와 같은 축).

    Returns:
        ``(커버리지, 사유)``. 판정 근거가 없으면 ``(0.0, 사유)`` — 호출자는 CSV 축과
        최대값을 취하므로 이것만으로 제거가 결정되지는 않는다.
    """
    from domain.tbox_utils import load_tacit_graph
    from domain.uri_conventions import local_name as _local_name
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _csv_rows_for_class,
        _tacit_coverage_for_op,
    )

    try:
        tacit = load_tacit_graph()
    except Exception as exc:                   # pragma: no cover — 파싱 실패 방어
        return 0.0, f"tacit-unreadable: {exc}"

    subjects = set(tacit.subjects(op, None))
    if not subjects:
        return 0.0, "tacit_absent"

    owner_local = _local_name(str(owner_cls))
    csv = _csv_rows_for_class(owner_local)
    if csv is None:
        covered, total = _tacit_coverage_for_op(g, op, owner_cls)
        if total == 0:
            return 0.0, "tacit_no_denominator"
        return covered / total, f"tacit {covered}/{total} (typed)"
    rows = len(csv[1])
    if rows == 0:
        return 0.0, "owner_csv_empty"
    return len(subjects) / rows, f"tacit {len(subjects)}/{rows}"


def effective_existential_coverage(
    g: Graph, op: URIRef, owner_cls: URIRef, filler_cls: URIRef, domain_ns: str,
) -> tuple[float, dict]:
    """필수참여 공리의 **정본** 근거 판정 — CSV 근거와 tacit 실측의 최대값.

    ``_fk_evidence_for_op`` 단독은 domain 에 CSV 가 있으면 tacit 을 보지 않으므로
    tacit 이 채우는 공리를 "근거 없음" 으로 낸다 (2026-09-05 실측:
    ``AirEmissionMonitoring ⊑ ∃hasStackEquipment`` 를 CSV 축은 0.0 으로 판정하는데
    tacit 이 1,560/1,560 을 채워 S9 의 ``existential_participation`` 은 위반 0 이다).

    **테스트와 게이트가 이 함수를 쓸 것.** CSV 축만 보는 판정을 기준으로 삼으면 산
    공리의 삭제를 요구하게 된다 — 이 리포는 가드가 A-Box 36,000건 보유 OP 를 억제한
    사고를 겪었다.

    Returns:
        ``(유효 커버리지, 근거 상세)``. 커버리지가 ``_REQUIRED_FILL_RATIO`` 이상이면
        그 공리는 근거가 있다.
    """
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _fk_evidence_for_op,
    )

    has_basis, csv_ratio, csv_reason = _fk_evidence_for_op(
        g, op, owner_cls, filler_cls, domain_ns,
    )
    csv_effective = csv_ratio if has_basis else 0.0
    tacit_ratio, tacit_reason = _tacit_row_coverage(g, op, owner_cls)
    return max(csv_effective, tacit_ratio), {
        "csv_coverage": round(csv_effective, 3),
        "csv_reason": csv_reason,
        "tacit_coverage": round(tacit_ratio, 3),
        "tacit_reason": tacit_reason,
    }


def _collect_existential_axioms(
    g: Graph, domain_ns: str,
) -> list[tuple[URIRef | BNode, URIRef, URIRef, URIRef]]:
    """``(restriction, owner, op, filler)`` 목록.

    BNode 든 스콜렘화된 URIRef 든 같이 잡는다 — 노드 종류로 공리를 식별하면
    ``step_19_bnode_skolemize`` 전후로 결과가 달라진다 (이 리포의 반복 실패 형태).
    """
    out = []
    for restriction in set(g.subjects(OWL.someValuesFrom, None)):
        op = next(g.objects(restriction, OWL.onProperty), None)
        filler = next(g.objects(restriction, OWL.someValuesFrom), None)
        if not isinstance(op, URIRef) or not isinstance(filler, URIRef):
            continue
        if not str(op).startswith(domain_ns):
            continue
        for owner in g.subjects(RDFS.subClassOf, restriction):
            if isinstance(owner, URIRef) and str(owner).startswith(domain_ns):
                out.append((restriction, owner, op, filler))
    return out


def _drop_axiom(g: Graph, restriction: URIRef | BNode, owner: URIRef) -> None:
    """owner 의 subClassOf 링크를 끊고, 고아가 된 restriction 노드를 정리한다."""
    g.remove((owner, RDFS.subClassOf, restriction))
    # 다른 참조가 남아 있으면 노드를 지우지 않는다 (공유 restriction 보호).
    if any(True for _ in g.subjects(None, restriction)):
        return
    for pred, obj in list(g.predicate_objects(restriction)):
        g.remove((restriction, pred, obj))


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    mode = os.getenv(_ENV_MODE, "prune").strip().lower()
    stats: dict = {"existential_audit_mode": mode}
    error: str | None = None

    if mode == "off":
        return StepResult(
            name="step_13e_existential_axiom_audit",
            stats={**stats, "existential_axioms_checked": 0},
            triples_delta=0,
            error=None,
            step_number=13,
            step_label="existential_axiom_audit",
        )

    try:
        from domain.uri_conventions import local_name as _local_name
        from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
            _REQUIRED_FILL_RATIO,
        )

        domain_ns = ctx.domain_ns
        axioms = _collect_existential_axioms(g, domain_ns)

        unsupported: list[dict] = []
        for restriction, owner, op, filler in axioms:
            effective, evidence = effective_existential_coverage(
                g, op, owner, filler, domain_ns,
            )
            if effective >= _REQUIRED_FILL_RATIO:
                continue
            unsupported.append({
                "owner": _local_name(str(owner)),
                "property": _local_name(str(op)),
                "filler": _local_name(str(filler)),
                # 근거가 아예 없는 것과 부분 충진을 구분한다 — 조치가 다르다
                # (모델링 수정 vs 소스 데이터 보완).
                "cause": "no_evidence" if effective == 0.0 else "partial_fill",
                **evidence,
                "_restriction": restriction,
                "_owner": owner,
            })

        pruned = 0
        if mode == "prune":
            for item in unsupported:
                _drop_axiom(g, item.pop("_restriction"), item.pop("_owner"))
                pruned += 1
        else:
            for item in unsupported:
                item.pop("_restriction", None)
                item.pop("_owner", None)

        if unsupported:
            logger.warning(
                "Step 13e: 근거 없는 필수참여 공리 %d개 (mode=%s, 제거 %d) — %s",
                len(unsupported), mode, pruned,
                [f'{i["owner"]}.{i["property"]}({i["cause"]})' for i in unsupported],
            )
        stats.update({
            "existential_axioms_checked": len(axioms),
            "existential_axioms_unsupported": len(unsupported),
            "existential_axioms_pruned": pruned,
            "existential_axioms_preserved": len(axioms) - pruned,
            "existential_unsupported_no_evidence": sum(
                1 for i in unsupported if i["cause"] == "no_evidence"
            ),
            "existential_unsupported_partial_fill": sum(
                1 for i in unsupported if i["cause"] == "partial_fill"
            ),
            "existential_unsupported_detail": unsupported[:10],
        })
    except Exception as e:                     # pragma: no cover — 스텝 격리
        logger.warning("Step 13e: 필수참여 공리 감사 실패: %s", e)
        error = str(e)

    return StepResult(
        name="step_13e_existential_axiom_audit",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=13,
        step_label="existential_axiom_audit",
    )
