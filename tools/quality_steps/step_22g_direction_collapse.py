"""Step 22g — 방향 소멸 subPropertyOf 정리.

`_consolidate_duplicate_ops`(step_22) 의 방향 가드는 **신규 추가만** 막는다.
이미 T-Box 에 기록된 링크는 S3 재실행으로 사라지지 않으므로 사후 정리가 필요하다
(step_22b 가 subPropertyOf 순환에 대해 하는 일과 같은 역할).

제거 대상 — 둘 다 ``owl:inverseOf`` 공리로 판정한다:
  1. ``P ⊑ inv(P)`` — P 가 대칭이 된다.
  2. ``P ⊑ Q``, ``P ⊑ R``, ``Q = inv(R)`` — P·Q·R 전부 대칭. 한쪽만 남긴다.

실측 손상 (2026-08-17): ``followedBy`` 가 ``directlyFollows`` /
``directlyPrecedes`` / ``precededBy`` 셋 모두의 하위였고 그 안에 역관계 쌍이
있었다. tacit 정방향 3건(고로→제강→연주→압연)이 추론 후 양방향으로 붕괴해
``all_inferred.ttl`` 에 ``ProcessSteelmakingFurnace directlyPrecedes
ProcessBlastFurnace`` 가 실재했다. HermiT consistent=true / SHACL 위반 0 /
check_quality_rules 101 이슈 중 언급 0건 — 어떤 게이트도 잡지 못했다.
"""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _repair_direction_collapse
    before = len(g)
    stats = _repair_direction_collapse(g, ctx.domain_ns)
    return StepResult(
        name="step_22g_direction_collapse",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="22g",
        step_label="direction_collapse_repair",
    )
