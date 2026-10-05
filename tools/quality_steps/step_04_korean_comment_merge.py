"""Step 4 — 중복 한국어 Comment 통합 (가장 긴 것 유지).

본문 ontology_quality.py 의 Step 4 블록을 그대로 모듈로 옮김.

steel: 네임스페이스의 subject 별로 ``rdfs:comment@ko`` 가 2개 이상이면
가장 긴 텍스트만 남기고 나머지를 제거한다. 동일 의미의 다른 표현이 LLM
revision 사이에 누적되어 doc bloat 을 만드는 것을 방지.
"""
from __future__ import annotations

import logging

from rdflib import RDFS, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns
    merged = 0
    for s in set(g.subjects()):
        if not isinstance(s, URIRef) or not str(s).startswith(steel_str):
            continue
        ko_triples = [
            (s, RDFS.comment, o)
            for _, _, o in g.triples((s, RDFS.comment, None))
            if isinstance(o, Literal) and o.language == "ko"
        ]
        if len(ko_triples) <= 1:
            continue
        best = max([str(t[2]) for t in ko_triples], key=len)
        for triple in ko_triples:
            g.remove(triple)
        g.add((s, RDFS.comment, Literal(best, lang="ko")))
        merged += 1
    return StepResult(
        name="step_04_korean_comment_merge",
        stats={"comments_merged": merged},
        triples_delta=len(g) - before,
        step_number=4,
        step_label="duplicate_comment_merge",
    )
