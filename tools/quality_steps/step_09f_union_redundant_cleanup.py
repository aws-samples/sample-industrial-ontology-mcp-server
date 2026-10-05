"""Step 9f — unionOf BNode 와 중복되는 URIRef domain/range 정리.

본문 ontology_quality.py 의 Step 9f 블록을 그대로 모듈로 옮김.

Step 9e 가 union 을 만든 뒤 downstream step (예: Step 13 OP someValuesFrom,
또는 향후 수동 OP 추가) 이 URIRef domain/range 를 재추가하면 SHACL
MaxCount(1) 위반 재발. union BNode 의 멤버 중복 URIRef 만 제거한다.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _cleanup_redundant_domain_after_union

    before = len(g)
    redundant_cleaned = _cleanup_redundant_domain_after_union(g)
    return StepResult(
        name="step_09f_union_redundant_cleanup",
        stats={"redundant_domain_after_union_cleaned": redundant_cleaned},
        triples_delta=len(g) - before,
        step_number="9f",
        step_label="union_redundant_cleanup",
    )
