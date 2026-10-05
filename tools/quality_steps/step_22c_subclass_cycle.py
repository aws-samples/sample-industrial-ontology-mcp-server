"""Step 22c — rdfs:subClassOf cycle breaker (2026-06-23).

Multi-Agent 가 의미가 거의 같은 두 클래스(SlabDesignSpec ↔
SlabDesignSpecification)를 서로의 부모로 선언하면 subClassOf 순환이 생긴다.
OWL 상 두 클래스가 equivalent 가 되어 추론을 오염시키고, 클래스 계층을 순회하는
코드(CQ runtime check 의 _descendants)를 무한 재귀(RecursionError)시킨다.
각 SCC 에서 가장 하위(outgoing subClassOf 가 가장 많은) 클래스의 루프 edge 만
제거해 순환을 끊고 나머지 계층은 보존한다.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _break_sub_class_cycles
    before = len(g)
    stats = _break_sub_class_cycles(g, ctx.domain_ns)
    return StepResult(
        name="step_22c_subclass_cycle",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="22c",
        step_label="sub_class_cycle_breaker",
    )
