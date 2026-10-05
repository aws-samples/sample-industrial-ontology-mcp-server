"""Step 23c — cross-domain subPropertyOf cleanup (Path B 강제).

rdfs:domain 이 서로 다른(교집합 공집합) class-specific property 사이의
rdfs:subPropertyOf 를 제거한다. 라벨 유사도로 잘못 묶인 DP(예:
reheatingFurnaceOpCreatedObjectId ⊑ slabCreatedObjectId)는 OWL RL
prp-spo1 + prp-dom 결합으로 타입 폭발을 일으키므로 Path B 정책상 분리한다.
Step 23(dup_dp) 직후 실행해 같은 라운드에서 생성된 cross-domain 링크도 정리.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _remove_cross_domain_subproperties
    before = len(g)
    stats = _remove_cross_domain_subproperties(g, ctx.domain_ns)
    return StepResult(
        name="step_23c_cross_domain_subprop",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="23c",
        step_label="cross_domain_subprop_cleanup",
    )
