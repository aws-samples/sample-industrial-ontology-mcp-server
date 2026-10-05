"""Step 22 — Duplicate OP consolidation (R11-M5).

같은 (domain, range) 를 가진 OP 그룹 중 의미적으로 중복인 것들을 canonical
하나로 정하고 나머지를 owl:subPropertyOf 로 연결한다. OWL RL 추론 시
kept(canonical) 의 트리플이 나머지 이름으로도 전파되어, CQ/시맨틱 딕셔너리가
non-canonical 이름을 참조해도 JOIN 가능해진다.

의미 구분 휴리스틱 (보수적): 라벨에 Origin/Destination/Source/Target/From/To/
출발/목적/근원/대상 같은 방향성 토큰이 다르면 consolidation 제외.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _consolidate_duplicate_ops
    before = len(g)
    stats = _consolidate_duplicate_ops(g, ctx.domain_ns)
    return StepResult(
        name="step_22_dup_op",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=22,
        step_label="duplicate_op_consolidation",
    )
