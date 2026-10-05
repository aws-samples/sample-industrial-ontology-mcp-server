"""Step 9 — 공유 프로퍼티 domain 누락 보완 (owl:Thing).

본문 ontology_quality.py 의 Step 9 블록을 그대로 모듈로 옮김.

Default fallback — Step 9b (OP range from inverse) 보다 먼저 실행되어
``rdfs:domain`` 이 없는 DP/OP 를 채운다. OP 는 inverse 의 range 에서 추론
시도, 실패 시 owl:Thing fallback. DP 는 무조건 owl:Thing.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns
    domain_thing_added = 0

    for prop in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if not str(prop).startswith(steel_str):
            continue
        if not list(g.objects(prop, RDFS.domain)):
            g.add((prop, RDFS.domain, OWL.Thing))
            domain_thing_added += 1

    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        if not str(prop).startswith(steel_str):
            continue
        if list(g.objects(prop, RDFS.domain)):
            continue
        inferred = None
        for inv in g.objects(prop, OWL.inverseOf):
            for rng in g.objects(inv, RDFS.range):
                if isinstance(rng, URIRef) and str(rng) != str(OWL.Thing):
                    inferred = rng
                    break
            if inferred is not None:
                break
        g.add((prop, RDFS.domain, inferred or OWL.Thing))
        domain_thing_added += 1

    return StepResult(
        name="step_09_shared_domain_fill",
        stats={"domain_thing_added": domain_thing_added},
        triples_delta=len(g) - before,
        step_number=9,
        step_label="domain_range_completion",
    )
