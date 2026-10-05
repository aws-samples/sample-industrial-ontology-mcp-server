"""Step 28 — owl:disjointUnionOf promotion (R13).

Disjoint group 의 union_parent 가 선언되어 있고 모든 member 가 그 parent 의
직접 subClassOf 일 때, 단순 AllDisjointClasses 를 disjointUnionOf axiom 으로
승격. parent 인스턴스가 정확히 한 child 에 들어간다는 분류 완전성을 명시한다.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _DISJOINT_CFG, _inject_disjoint_union_of
    before = len(g)
    try:
        stats = _inject_disjoint_union_of(g, _DISJOINT_CFG.get("groups", []))
        error = None
    except Exception as e:
        logger.warning("disjointUnionOf injection 실패: %s", e)
        stats = {"disjoint_unions_added": 0}
        error = str(e)
    return StepResult(
        name="step_28_disjoint_union",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=28,
        step_label="disjoint_union_of",
    )
