"""Step 22b — subPropertyOf cycle breaker (R25).

Multi-Agent 가 의미 동일한 OP 를 duplicate 생성 후 양방향 subPropertyOf 로
연결하면 sub_property_cycle critical 이슈 발생. 각 SCC 에서 가장 "하위"
(가장 적은 outgoing subPropertyOf) 한 OP 의 루프 edge 만 제거.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _break_sub_property_cycles
    before = len(g)
    stats = _break_sub_property_cycles(g, ctx.domain_ns)
    return StepResult(
        name="step_22b_subprop_cycle",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="22b",
        step_label="sub_property_cycle_breaker",
    )
