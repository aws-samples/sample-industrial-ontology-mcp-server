"""Step 17 — Label Synthesis.

Jury add_class / add_object_property 경로에서 label 을 깜빡한 신규 엔티티가
check_quality_rules HIGH / SHACL 위반으로 잡히는 문제 대응. 누락된 라벨을
한국어/영어로 자동 생성한다.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _inject_missing_labels
    before = len(g)
    label_stats = _inject_missing_labels(g, ctx.domain_ns)
    return StepResult(
        name="step_17_label_synthesis",
        stats={
            "labels_added_en": label_stats["added_en"],
            "labels_added_ko": label_stats["added_ko"],
            "labels_subjects_fixed": label_stats["subjects_fixed"],
        },
        triples_delta=len(g) - before,
        step_number=17,
        step_label="label_synthesis",
    )
