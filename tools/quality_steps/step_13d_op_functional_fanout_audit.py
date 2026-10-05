"""Step 13d — OP 의 ``owl:FunctionalProperty`` 선언을 tacit 실측 팬아웃과 대조.

## 왜 (2026-09-05 실측)

S2(LLM) 가 ObjectProperty **47개**를 ``owl:FunctionalProperty`` 로 선언했다
(베이스라인은 15개). 그런데 S3 에는 **OP functional 선언을 데이터와 대조하는 스텝이
없었다** — ``step_13`` 은 DP 에만 붙이고(PK DP / 분류 DP), 13b·13c 는 cardinality
restriction 축만 본다.

그 결과 ``hasStackEquipment`` 가 Functional 로 선언된 채 배포됐고, tacit 이 같은 OP 에
팬아웃 8 로 11,160 트리플을 쓰므로 **주어 1,560개 전부가 위반**이다. (``validate_kg``
응답의 ``violation_count: 5`` 는 절단값이다 — merge 그래프 실측은 1,560.)

이 tacit 규칙은 실수가 아니라 **의도된 근사**다. ``rules/domain/tacit_rules.json`` 의
``rule_air_emission_colocated_equipment`` 가 스스로 적어 두었다: *"이는 '이 구역의
설비' 이지 '이 굴뚝의 설비' 가 아니다 … 다대다"*. 즉 이 OP 는 설계상 다대다이고
**Functional 선언 쪽이 틀렸다** — tacit 을 고칠 문제가 아니다.

## 왜 tacit 만 보는가 (판정 근거의 시점)

S3 는 S7(A-Box 생성) **앞**에 돈다. 배포 A-Box 를 판정 근거로 쓰면 이전 세대를 보고
결정하게 되므로(이 리포가 겪은 "산출물 세대 불일치") **source 산출물만** 본다 —
tacit TTL 은 소스이고 세대에 무관하다.

단일 FK 컬럼은 행당 값이 하나라 A-Box 만으로는 팬아웃이 생기지 않는다. 실측으로도
merge 그래프(abox+tacit+역방향) 기준 모순 **1개**가 tacit 만으로 잡은 **1개**와
동일했다 (선언 43개 전수 대조). A-Box 쪽에서만 생기는 팬아웃(FK 컬럼 둘이 같은 OP 로
라우팅되는 경우 등)은 A-Box 가 존재하는 시점에 S9 의 ``check_functional_violations``
가 잡는다.

## 무엇을 하지 않는가

* 팬아웃을 재지 못하면 **선언을 남긴다** — 판정 불가는 위반이 아니다. 실측: 선언 43개
  중 tacit 에 나타나는 것은 6개뿐이고 나머지 37개는 이 스텝이 건드리지 않는다.
* OP 선언 자체나 트리플, ``rdfs:domain``/``range`` 는 지우지 않는다.
  ``owl:FunctionalProperty`` 타입 **한 줄만** 제거한다.
* DP 의 functional 선언은 보지 않는다 (``step_13`` 이 A-Box 실측 단일값으로 이미
  판정한다 — ``_mark_classifying_dps_functional``).
"""
from __future__ import annotations

import collections
import logging
import os

from rdflib import OWL, RDF, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: 환경변수 — ``remove``(기본) / ``warn``(측정만) / ``off``(스텝 무효화).
_ENV_MODE = "TBOX_OP_FUNCTIONAL_FANOUT"

#: 이 값을 넘는 팬아웃이 실측되면 Functional 선언과 모순이다.
#: FunctionalProperty 는 "주어당 값이 최대 하나" 이므로 2 이상이면 거짓.
_MAX_FUNCTIONAL_FANOUT = 1


def _tacit_max_fanout(domain_ns: str) -> dict[URIRef, tuple[int, int, int]]:
    """tacit 이 쓰는 도메인 프로퍼티별 ``(주어 수, 트리플 수, 최대 팬아웃)``.

    tacit 을 읽을 수 없으면 빈 dict — 호출자는 아무것도 제거하지 않는다.

    .. note::
       ``domain_ns`` 필터는 **비용 한정이지 가드가 아니다**. 외래 술어를 세어도
       호출자가 도메인 NS OP 만 조회하므로 판정은 달라지지 않는다 (mutation 으로
       확인: 이 필터를 지워도 테스트가 전부 통과한다). 외래 프로퍼티를 판정에서
       빼는 실제 가드는 ``apply`` 의 ``declared`` 열거 쪽이다 — 그것을 지우면
       ``test_foreign_namespace_property_is_ignored`` 가 실패한다. 여기를 "안전
       장치" 로 읽고 호출부 가드를 느슨하게 하면 외래 온톨로지의 공리를 지운다.
    """
    from domain.tbox_utils import load_tacit_graph

    try:
        tacit = load_tacit_graph()
    except Exception as exc:                   # pragma: no cover — 파싱 실패 방어
        logger.info("Step 13d: tacit 로드 실패 — 팬아웃 판정을 건너뛴다 (%s)", exc)
        return {}

    per_prop: dict[URIRef, collections.Counter] = collections.defaultdict(
        collections.Counter,
    )
    for subj, pred, _obj in tacit:
        if isinstance(pred, URIRef) and str(pred).startswith(domain_ns):
            per_prop[pred][subj] += 1
    return {
        prop: (len(counter), sum(counter.values()), max(counter.values()))
        for prop, counter in per_prop.items()
        if counter
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    mode = os.getenv(_ENV_MODE, "remove").strip().lower()
    stats: dict = {"op_functional_mode": mode}
    error: str | None = None

    if mode == "off":
        return StepResult(
            name="step_13d_op_functional_fanout_audit",
            stats={**stats, "op_functional_checked": 0},
            triples_delta=0,
            error=None,
            step_number=13,
            step_label="op_functional_fanout_audit",
        )

    try:
        domain_ns = ctx.domain_ns
        declared = sorted(
            (
                p for p in set(g.subjects(RDF.type, OWL.FunctionalProperty))
                & set(g.subjects(RDF.type, OWL.ObjectProperty))
                if isinstance(p, URIRef) and str(p).startswith(domain_ns)
            ),
            key=str,
        )
        fanout = _tacit_max_fanout(domain_ns)

        contradicted: list[dict] = []
        undecidable = 0
        for op in declared:
            measured = fanout.get(op)
            if measured is None:
                undecidable += 1
                continue                       # 판정 불가 — 선언을 남긴다
            subjects, triples, max_fanout = measured
            if max_fanout <= _MAX_FUNCTIONAL_FANOUT:
                continue                       # 데이터와 정합
            contradicted.append({
                "property": str(op).split("#")[-1],
                "tacit_subjects": subjects,
                "tacit_triples": triples,
                "max_fanout": max_fanout,
            })

        removed = 0
        if mode == "remove":
            for item in contradicted:
                op = URIRef(domain_ns + item["property"])
                g.remove((op, RDF.type, OWL.FunctionalProperty))
                removed += 1

        if contradicted:
            logger.warning(
                "Step 13d: OP %d개의 owl:FunctionalProperty 선언이 tacit 실측 팬아웃과 "
                "모순 (mode=%s, 제거 %d): %s",
                len(contradicted), mode, removed,
                [f'{c["property"]}(팬아웃 {c["max_fanout"]})' for c in contradicted],
            )
        stats.update({
            "op_functional_checked": len(declared),
            "op_functional_measurable": len(declared) - undecidable,
            "op_functional_undecidable": undecidable,
            "op_functional_contradicted": len(contradicted),
            "op_functional_removed": removed,
            "op_functional_contradicted_detail": contradicted[:10],
        })
    except Exception as e:                     # pragma: no cover — 스텝 격리
        logger.warning("Step 13d: OP functional 팬아웃 감사 실패: %s", e)
        error = str(e)

    return StepResult(
        name="step_13d_op_functional_fanout_audit",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=13,
        step_label="op_functional_fanout_audit",
    )
