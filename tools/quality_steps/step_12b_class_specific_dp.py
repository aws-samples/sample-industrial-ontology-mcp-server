"""Step 12b — Path B class-specific DP enforcement (기능 단계).

본문 ontology_quality.py 의 Step 12b 블록을 그대로 모듈로 옮김.

LLM 이 프롬프트 제약 (04-property-rules.md) 에도 불구하고 generic DP
(``hasStatus``, ``hasIdentifier`` 등) 를 만든 경우 사후 감지/처리.

환경변수 ``TBOX_STRICT_CLASS_SPECIFIC`` 로 3 모드 제어:
  - ``warn`` (default): 경고 로그 + stats 기록, T-Box 유지
  - ``rename``: class prefix 자동 주입 (domain 단일 case)
  - ``remove``: generic DP 삭제 (Multi-Agent 재생성 유도)
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _enforce_class_specific_dps

    before = len(g)
    steel_str = ctx.domain_ns
    cs_stats: dict = {}
    error: str | None = None
    try:
        cs_stats = _enforce_class_specific_dps(g, steel_str)
    except Exception as _cs_exc:
        logger.warning(
            "Step 12b class-specific DP enforcement 실패 (무시): %s", _cs_exc
        )
        error = str(_cs_exc)
    return StepResult(
        name="step_12b_class_specific_dp",
        stats=cs_stats,
        triples_delta=len(g) - before,
        error=error,
        step_number="12b",
        step_label="class_specific_dp_enforcement",
    )
