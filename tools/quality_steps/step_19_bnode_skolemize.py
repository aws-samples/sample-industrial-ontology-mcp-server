"""Step 19 — BNode Skolemization.

Restriction / unionOf / intersectionOf 등에 쓰인 익명 BNode 를 도메인-결정적
URIRef 로 승격. SPARQL 역추적 + Neo4j LPG 적재 시 named-node 강제 요구를
충족한다.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _skolemize_bnodes
    before = len(g)
    error: str | None = None
    try:
        skolem_stats = _skolemize_bnodes(g)
    except Exception as e:
        logger.warning("BNode skolemization 실패: %s", e)
        skolem_stats = {"total_skolemized": 0, "by_type": {}}
        error = str(e)
    return StepResult(
        name="step_19_bnode_skolemize",
        stats={
            "bnode_skolemized": skolem_stats["total_skolemized"],
            "bnode_by_type": skolem_stats["by_type"],
        },
        triples_delta=len(g) - before,
        error=error,
        step_number=19,
        step_label="bnode_skolemization",
    )
