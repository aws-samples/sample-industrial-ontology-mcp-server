"""Step 25 — OntoClean meta-annotation injection (R12 C3).

rules/domain/ontoclean_labels.json 의 rigidity/unity/identity/dependence 를 T-Box 에
주입하여 validate_ontoclean 검증기가 C1/C2/C3 규칙을 실제 적용할 수 있게 한다.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _inject_ontoclean_annotations
    before = len(g)
    stats = _inject_ontoclean_annotations(g, ctx.domain_ns)
    return StepResult(
        name="step_25_ontoclean",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=25,
        step_label="ontoclean_annotation",
    )
