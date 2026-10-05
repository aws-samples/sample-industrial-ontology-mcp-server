"""Step 23 — Duplicate DP consolidation (R12 C2).

중복 라벨 DatatypeProperty 를 canonical 하나로 묶어 subPropertyOf chain 형성.
CSV 출처별 tenant property (equipmentId 계열) 를 재사용 가능한 의미 단위로
정규화한다.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _consolidate_duplicate_dps
    before = len(g)
    stats = _consolidate_duplicate_dps(g, ctx.domain_ns)
    return StepResult(
        name="step_23_dup_dp",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=23,
        step_label="duplicate_dp_consolidation",
    )
