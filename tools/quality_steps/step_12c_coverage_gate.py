"""Step 12c — L2 Coverage Gate (R2, 2026-05-12).

본문 ontology_quality.py 의 Step 12c 블록을 그대로 모듈로 옮김.

CSV non-PK/FK 컬럼 대비 class-specific DP 커버리지 측정 (read-only).
Step 12b rename/remove 이후 최종 상태에서 측정되므로 rename 으로 복구된
DP 도 coverage 에 포함.

환경변수:
  - TBOX_COVERAGE_GATE: ``warn`` (default) | ``fail``
  - TBOX_COVERAGE_THRESHOLD: 0.0~1.0 (default 0.85)

본 단계는 T-Box 를 수정하지 않는다. ``fail`` 모드에서 임계치 미달 시
``RuntimeError`` 를 호출자에게 전파한다.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _check_csv_column_coverage

    before = len(g)
    steel_str = ctx.domain_ns
    cov_stats: dict = {}
    error: str | None = None
    try:
        cov_stats = _check_csv_column_coverage(g, steel_str)
    except RuntimeError as _cov_err:
        logger.error("Step 12c coverage gate FAIL: %s", _cov_err)
        raise
    except Exception as _cov_exc:
        logger.warning("Step 12c coverage 측정 실패 (무시): %s", _cov_exc)
        error = str(_cov_exc)
    return StepResult(
        name="step_12c_coverage_gate",
        stats=cov_stats,
        triples_delta=len(g) - before,
        error=error,
        step_number="12c",
        step_label="csv_column_coverage_gate",
    )
