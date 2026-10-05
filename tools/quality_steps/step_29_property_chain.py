"""Step 29 — owl:propertyChainAxiom injection (R13).

rules/domain/property_chains.json 의 declarative chain 을 promote — multi-hop 도메인
지식을 추론 한 단계로. 체인 segment 가 하나라도 부재하면 해당 axiom 만 skip
하므로 규칙 변경에 graceful 하게 대응.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import (
        _inject_property_chain_axioms,
        _load_property_chains,
    )
    before = len(g)
    try:
        chains = _load_property_chains()
        stats = _inject_property_chain_axioms(g, chains)
        error = None
    except Exception as e:
        logger.warning("propertyChainAxiom injection 실패: %s", e)
        stats = {"chains_added": 0}
        error = str(e)
    return StepResult(
        name="step_29_property_chain",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=29,
        step_label="property_chain_axioms",
    )
