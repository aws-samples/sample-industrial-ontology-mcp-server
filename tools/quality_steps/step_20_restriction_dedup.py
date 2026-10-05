"""Step 20 — Restriction dedup.

과거 buggy skolemize 가 hash suffix 로 분기해 만든 의미론적으로 동일한
restriction 들을 하나로 병합. subClassOf 에서 참조되는 의미가 같은 URI 들이
모두 별개 클래스로 추론 대상이 되면서 OWL RL closure 가 수분~수시간 빠지는
폭발을 막는다.

본 step 은 본문에서 'Step 19' 주석으로 표시되어 있었으나, change_log 의
``step: 20`` 라벨과 일관되도록 모듈명도 step_20 으로.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _dedup_named_restrictions
    before = len(g)
    error: str | None = None
    try:
        dedup_stats = _dedup_named_restrictions(g)
    except Exception as e:
        logger.warning("Restriction dedup 실패: %s", e)
        dedup_stats = {"merged": 0, "removed": 0}
        error = str(e)
    return StepResult(
        name="step_20_restriction_dedup",
        stats={"restriction_dedup": dedup_stats},
        triples_delta=len(g) - before,
        error=error,
        step_number=20,
        step_label="restriction_dedup",
    )
