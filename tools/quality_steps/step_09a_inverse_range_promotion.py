"""Step 9a — owl:Thing domain OP 의 inverse.range 기반 승격.

본문 ontology_quality.py 의 Step 9a 블록을 그대로 모듈로 옮김.

Step 9 가 fallback 으로 owl:Thing domain 을 부여한 OP 중 inverse 의 range 가
구체 클래스인 경우 domain 을 그 클래스로 승격. A-Box 생성 / OWL RL 타입 추론
을 깨끗하게 유지하기 위해.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns
    op_domain_promoted_from_inverse = 0

    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        if not str(prop).startswith(steel_str):
            continue
        domains = list(g.objects(prop, RDFS.domain))
        if not domains or OWL.Thing not in domains or len(domains) > 1:
            continue
        inverses = list(g.objects(prop, OWL.inverseOf))
        specific_domain = None
        for inv in inverses:
            for rng in g.objects(inv, RDFS.range):
                if isinstance(rng, URIRef) and str(rng).startswith(steel_str):
                    specific_domain = rng
                    break
            if specific_domain is not None:
                break
        if specific_domain is not None:
            g.remove((prop, RDFS.domain, OWL.Thing))
            g.add((prop, RDFS.domain, specific_domain))
            op_domain_promoted_from_inverse += 1

    return StepResult(
        name="step_09a_inverse_range_promotion",
        stats={"op_domain_promoted_from_inverse": op_domain_promoted_from_inverse},
        triples_delta=len(g) - before,
        step_number="9a",
        step_label="inverse_range_promotion",
    )
