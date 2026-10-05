"""Step 9b — ObjectProperty range 교정.

본문 ontology_quality.py 의 Step 9b 블록을 그대로 모듈로 옮김.

3 가지 케이스 처리:
- (a) range 누락 + inverseOf 있음 → inverse.domain 을 range 로
- (b) range 누락 + inverseOf 없음 → owl:Thing
- (c) range 2개 이상 → transitive subClassOf 체인 따라 가장 구체적 클래스 1개

stats: ``op_range_fixed`` 와 ``range_fixed`` (본문 호환을 위한 alias) 모두 기록.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    range_fixed = 0
    steel_str = ctx.domain_ns

    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        if not str(prop).startswith(steel_str):
            continue
        ranges = [r for r in g.objects(prop, RDFS.range) if isinstance(r, URIRef)]

        if len(ranges) == 0:
            inverses = list(g.objects(prop, OWL.inverseOf))
            inferred_range = None
            for inv in inverses:
                inv_domains = [d for d in g.objects(inv, RDFS.domain) if isinstance(d, URIRef)]
                if inv_domains:
                    inferred_range = inv_domains[0]
                    break
            g.add((prop, RDFS.range, inferred_range or OWL.Thing))
            range_fixed += 1

        elif len(ranges) > 1:
            keep = ranges[0]
            for r in ranges[1:]:
                is_sub = (r, RDFS.subClassOf, keep) in g
                if not is_sub:
                    visited = set()
                    stack = [r]
                    while stack:
                        cur = stack.pop()
                        if cur in visited:
                            continue
                        visited.add(cur)
                        for parent in g.objects(cur, RDFS.subClassOf):
                            if parent == keep:
                                is_sub = True
                                break
                            if isinstance(parent, URIRef):
                                stack.append(parent)
                        if is_sub:
                            break
                if is_sub:
                    keep = r
            for r in ranges:
                if r != keep:
                    g.remove((prop, RDFS.range, r))
            range_fixed += 1

    return StepResult(
        name="step_09b_op_range_fix",
        stats={"op_range_fixed": range_fixed, "range_fixed": range_fixed},
        triples_delta=len(g) - before,
        step_number="9b",
        step_label="op_range_fix",
    )
