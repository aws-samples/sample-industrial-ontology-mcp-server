"""Step 24 — Abstract category annotation (R12 C1/H1).

Lazy class 를 제거 대신 skos:Concept 로 이중 타입 + scopeNote 주석,
Management/Category suffix 는 notation='category' 로 시각화/검색 힌트.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _annotate_abstract_categories
    before = len(g)
    stats = _annotate_abstract_categories(g, ctx.domain_ns)
    return StepResult(
        name="step_24_abstract_category",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=24,
        step_label="abstract_category_annotation",
    )
