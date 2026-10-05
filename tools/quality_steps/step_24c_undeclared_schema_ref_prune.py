"""Step 24c — 선언되지 않은 클래스를 가리키는 **계층 참조** 를 제거.

## 왜 필요한가 — 게이트는 있었지만 고치는 스텝이 없었다

``check_schema_reference_integrity`` (validate_kg 23번) 는 2026-08-24 에 이 결함을
잡도록 도입됐다. 그런데 **S3 에 대응 수정 스텝이 없어서** 매 S2 마다 같은 위반이
재발했다. 2026-09-03 실측 (S2 재실행 직후 배포 T-Box)::

    MaintenanceHistory ⊑ MaintenanceActivity     ← MaintenanceActivity 미선언

같은 이름이 그 게이트의 docstring 예시(2026-08-24)와 **동일**하다. 즉 열흘 넘게
같은 유령 부모가 게이트에만 걸리고 아무도 고치지 않았다. 이번 S2 의 ``save_guard``
는 ``MaintenanceActivity`` 를 **손실 클래스** 로 보고했다 — S2 가 선언을 잃었는데
계층 참조는 남긴 것이 발생 경로다.

유령 부모의 해악은 게이트 메시지가 적은 그대로다: DIT/NOC 지표가 실재하지 않는
노드를 세고, 그 부모를 기대한 질의는 **에러 없이 영구 0건** 이 된다. HermiT 은
RDFS 의미론상 이것을 오류로 보지 않으므로 (미선언 IRI 는 암묵적으로 클래스로
취급된다) S4 는 통과한다.

## 왜 "선언 추가" 가 아니라 "참조 제거" 인가

미선언 부모를 ``owl:Class`` 로 선언하면 **A-Box 0건 빈 클래스** 가 하나 늘어난다.
이 리포는 빈 클래스 34개를 결함으로 추적한 이력이 있고, ``step_31b`` 는 그것을
감사한다. 게이트 메시지도 위험을 "유령 노드가 계층에 섞이는 것" 으로 규정한다 —
즉 문제는 참조 쪽이다. 의미가 실제로 필요한 부모라면 SME 가
``rules/domain/tbox_manual_additions.ttl`` 에 **선언과 함께** 넣어야 하고, 그러면
``step_30`` 이 매 실행 복원하므로 이 스텝은 그것을 건드리지 않는다 (선언이 있으면
위반이 아니다).

## 무엇을 지우지 않는가 (과잉 제거 방지)

- **``rdfs:domain`` / ``rdfs:range``** — 지우면 프로퍼티가 제약 없는 상태가 된다.
  이 리포는 "미선언 ≠ universal" 을 명시적으로 구분하며, domain 을 넓혀 검사를
  눈감긴 사고(step_10) 가 있다. 약화는 탐지되지 않으므로 **보고만** 한다.
- **유일한 부모** — 그 클래스가 계층에서 고립된다. 보고만 하고 남긴다.
- 정상 대상(외래 온톨로지 / XSD·OWL·RDFS / 명명된 Restriction / blank node) 은
  애초에 위반으로 오지 않는다 — 판정은 ``scan_undeclared_schema_references`` 정본
  하나가 한다 (사본 금지: 게이트와 스텝이 어긋나면 이 스텝의 존재 이유가 사라진다).

환경변수:
  - ``TBOX_UNDECLARED_REF_PRUNE``: ``prune`` (default) | ``warn`` | ``off``
"""
from __future__ import annotations

import logging
import os

from rdflib import Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: 계층 축만 제거 대상이다. domain/range 는 제거가 곧 제약 약화라 보고만 한다.
_PRUNABLE_AXES = ("subClassOf", "subPropertyOf")


def _remaining_parents(g: Graph, subj: URIRef, pred: URIRef, dropped: URIRef) -> int:
    """``dropped`` 를 뺀 뒤 남는 부모 수. 0 이면 고립되므로 지우지 않는다."""
    return sum(
        1 for obj in g.objects(subj, pred)
        if isinstance(obj, URIRef) and obj != dropped
    )


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """미선언 클래스를 가리키는 계층 참조를 제거하고, 나머지는 보고한다."""
    from tools.validation_support.checks.semantic import (
        scan_undeclared_schema_references,
    )

    before = len(g)
    mode = (os.getenv("TBOX_UNDECLARED_REF_PRUNE") or "prune").strip().lower()
    if mode == "off":
        return StepResult(
            name="step_24c_undeclared_schema_ref_prune",
            stats={"undeclared_ref_skipped": "disabled by TBOX_UNDECLARED_REF_PRUNE"},
        )

    checked, refs = scan_undeclared_schema_references(g, ctx.domain_ns)

    pruned: list[dict] = []
    kept_sole_parent: list[dict] = []
    reported_not_pruned: list[dict] = []

    for ref in refs:
        record = ref.as_dict()
        if ref.axis not in _PRUNABLE_AXES:
            reported_not_pruned.append(record)
            continue
        if _remaining_parents(g, ref.subject, ref.predicate, ref.target) == 0:
            # 유일한 부모 — 지우면 계층에서 고립된다.
            kept_sole_parent.append(record)
            continue
        if mode == "prune":
            g.remove((ref.subject, ref.predicate, ref.target))
        pruned.append(record)

    stats: dict[str, object] = {
        "undeclared_ref_checked": checked,
        "undeclared_ref_violations": len(refs),
        "undeclared_ref_pruned": len(pruned) if mode == "prune" else 0,
        "undeclared_ref_would_prune": len(pruned) if mode == "warn" else 0,
        "undeclared_ref_kept_sole_parent": len(kept_sole_parent),
        "undeclared_ref_reported_not_pruned": len(reported_not_pruned),
        "undeclared_ref_mode": mode,
    }
    if pruned:
        stats["undeclared_ref_pruned_sample"] = pruned[:10]
        logger.info(
            "Step 24c: 미선언 클래스를 가리키는 계층 참조 %d건 %s — %s",
            len(pruned),
            "제거" if mode == "prune" else "보고 (warn)",
            ", ".join(
                f"{r['subject']} --{r['axis']}--> {r['undeclared_target']}"
                for r in pruned[:5]
            ),
        )
    if kept_sole_parent:
        stats["undeclared_ref_kept_sole_parent_sample"] = kept_sole_parent[:10]
        logger.warning(
            "Step 24c: 유일한 부모라 남긴 미선언 참조 %d건 — 계층 고립을 막기 위해 "
            "제거하지 않았다. rules/domain/tbox_manual_additions.ttl 에 선언을 "
            "추가하거나 SME 가 부모를 교체해야 한다: %s",
            len(kept_sole_parent),
            ", ".join(
                f"{r['subject']} --{r['axis']}--> {r['undeclared_target']}"
                for r in kept_sole_parent[:5]
            ),
        )
    if reported_not_pruned:
        stats["undeclared_ref_reported_sample"] = reported_not_pruned[:10]
        logger.warning(
            "Step 24c: domain/range 가 미선언 IRI 를 가리키는 %d건 — 제거하면 "
            "제약이 사라지므로(미선언 ≠ universal) 보고만 한다: %s",
            len(reported_not_pruned),
            ", ".join(
                f"{r['subject']} --{r['axis']}--> {r['undeclared_target']}"
                for r in reported_not_pruned[:5]
            ),
        )

    return StepResult(
        name="step_24c_undeclared_schema_ref_prune",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="24c",
        step_label="undeclared_schema_ref_prune",
    )
