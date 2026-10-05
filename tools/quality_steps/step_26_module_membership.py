"""Step 26 — Module membership annotation (R12 M1).

단일 파일 유지하되 dcterms:isPartOf 로 논리적 module 소속을 표시해
downstream 시각화/검색이 module 별 필터링 가능.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _annotate_module_membership
    before = len(g)
    stats = _annotate_module_membership(g, ctx.domain_ns)
    return StepResult(
        name="step_26_module_membership",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=26,
        step_label="module_annotation",
    )
