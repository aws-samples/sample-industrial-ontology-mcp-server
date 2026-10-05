"""Step 9a-2 — 외부 네임스페이스 domain 교정 (R28 post-S5).

본문 ontology_quality.py 의 Step 9a-2 블록을 그대로 모듈로 옮김.

Architect / jury_fixes / manual edit 등에서 ObjectProperty 의 domain 으로
IOF, BFO, Core 같은 상위 온톨로지 클래스를 잘못 선언하는 경우 처리:
- domain 이 외부 네임스페이스 단독 인 경우만 처리 (혼합은 9e unionOf 에 위임)
- inverseOf 의 range 로 대체 시도, 실패 시 owl:Thing fallback
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns
    op_domain_foreign_namespace_fixed = 0
    _owl_thing = OWL.Thing

    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        if not str(prop).startswith(steel_str):
            continue
        domains = list(g.objects(prop, RDFS.domain))
        if not domains:
            continue
        has_steel_or_thing = any(
            isinstance(d, URIRef)
            and (str(d).startswith(steel_str) or d == _owl_thing)
            for d in domains
        )
        if has_steel_or_thing:
            continue
        foreign = [d for d in domains if isinstance(d, URIRef)]
        if not foreign:
            continue
        replacement = None
        for inv in g.objects(prop, OWL.inverseOf):
            for rng in g.objects(inv, RDFS.range):
                if isinstance(rng, URIRef) and str(rng).startswith(steel_str):
                    replacement = rng
                    break
            if replacement is not None:
                break
        for d in foreign:
            g.remove((prop, RDFS.domain, d))
        remaining = list(g.objects(prop, RDFS.domain))
        if not remaining:
            g.add((prop, RDFS.domain, replacement or _owl_thing))
        op_domain_foreign_namespace_fixed += 1

    return StepResult(
        name="step_09a2_foreign_ns_domain_fix",
        stats={
            "op_domain_foreign_namespace_fixed":
                op_domain_foreign_namespace_fixed,
        },
        triples_delta=len(g) - before,
        step_number="9a-2",
        step_label="foreign_ns_domain_fix",
    )
