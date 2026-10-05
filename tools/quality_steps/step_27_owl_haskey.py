"""Step 27 — owl:hasKey injection (R13).

CSV-detected primary keys 를 owl:hasKey axiom 으로 승격. Composite PK 는
multi-property key 로. Reasoner-level uniqueness 는 Step 13 의 DP-level
FunctionalProperty 마커를 보완한다.

본 step 은 외부 _inject_owl_haskey 에 위임하며, 실패 시 warn 모드로
처리해 후속 step 진행을 막지 않는다.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _inject_owl_haskey
    before = len(g)
    try:
        stats = _inject_owl_haskey(g)
        error = None
    except Exception as e:
        logger.warning("hasKey injection 실패: %s", e)
        stats = {"haskey_added": 0}
        error = str(e)
    return StepResult(
        name="step_27_owl_haskey",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=27,
        step_label="owl_haskey_injection",
    )
