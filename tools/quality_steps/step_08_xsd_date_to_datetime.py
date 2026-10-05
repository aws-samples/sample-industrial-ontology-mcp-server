"""Step 8 — xsd:date → xsd:dateTime 변환 (HermiT OWL 2 호환).

본문 ontology_quality.py 의 Step 8 블록을 그대로 모듈로 옮김.

HermiT 가 ``rdfs:range xsd:date`` 를 OWL 2 datatype 으로 거부하므로
``xsd:dateTime`` 으로 정규화.
"""
from __future__ import annotations

import logging

from rdflib import RDFS, XSD, Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    date_fixed = 0
    for s, p, o in list(g.triples((None, RDFS.range, XSD.date))):
        g.remove((s, p, o))
        g.add((s, RDFS.range, XSD.dateTime))
        date_fixed += 1
    return StepResult(
        name="step_08_xsd_date_to_datetime",
        stats={"xsd_date_converted": date_fixed},
        triples_delta=len(g) - before,
        step_number=8,
        step_label="xsd_date_conversion",
    )
