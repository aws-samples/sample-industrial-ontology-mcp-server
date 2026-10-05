"""Step 24b — ODP auto-apply (T4).

abstract_group_hints.json 의 그룹 정의를 결정적으로 주입한다. Architect 가
프롬프트 힌트를 무시했을 때 평면 계층이 자동 그룹화되어 OntoQA DIT 목표
(2~5) 및 최상위 클래스 비율 ≤20% 달성이 결정적이 된다. Step 24 의
skos:Concept 태그 다음에 와야 — 새로 생성된 abstract class 는 Step 25
OntoClean annotation 에 의해 휴리스틱 라벨도 받게 된다.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _apply_odp_abstract_groups
    before = len(g)
    stats = _apply_odp_abstract_groups(g, ctx.domain_ns)
    return StepResult(
        name="step_24b_odp_auto_apply",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="24b",
        step_label="odp_abstract_groups_applied",
    )
