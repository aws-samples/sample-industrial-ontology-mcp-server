"""Step 6 — ManufacturingProcessStep unionOf → equivalentClass 변환.

본문 ontology_quality.py 의 Step 6 블록을 그대로 모듈로 옮김.

- MPS 클래스 자동 생성 (PROCESS_STEP_CLASSES 중 하나라도 등장 시)
- 기존 unionOf 리스트 → equivalentClass 로 재구성
- PROCESS_STEP_CLASSES → MPS 의 subClassOf 보장 (공정 흐름 체인 검증용)
"""
from __future__ import annotations

import logging

import rdflib
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from domain.namespaces import IOF_CORE
    from tools.ontology_quality import DOMAIN_NS_OBJ, PROCESS_STEP_CLASSES

    before = len(g)
    stats: dict = {
        "mps_fixed": False,
        "mps_subclass_links_added": 0,
        "mps_class_created": False,
    }

    mps = DOMAIN_NS_OBJ["ManufacturingProcessStep"]

    def _class_is_present(c_uri):
        if (c_uri, RDF.type, OWL.Class) in g:
            return True
        for _ in g.triples((c_uri, None, None)):
            return True
        for _ in g.triples((None, RDFS.subClassOf, c_uri)):
            return True
        return False

    if (mps, RDF.type, OWL.Class) not in g:
        has_process_child = any(
            _class_is_present(DOMAIN_NS_OBJ[c]) for c in PROCESS_STEP_CLASSES
        )
        if has_process_child:
            g.add((mps, RDF.type, OWL.Class))
            g.add((mps, RDFS.label, Literal("Manufacturing Process Step", lang="en")))
            g.add((mps, RDFS.label, Literal("제조 공정 단계", lang="ko")))
            g.add((mps, RDFS.comment, Literal(
                "제조 공정의 단일 단계 (고로/제강/연주/압연 등의 상위 추상 클래스)",
                lang="ko")))
            iof_mp = URIRef(
                IOF_CORE + "ManufacturingProcess"
            )
            g.add((mps, RDFS.subClassOf, iof_mp))
            stats["mps_class_created"] = True

    if (mps, RDF.type, OWL.Class) in g:
        for _s, _p, o in list(g.triples((mps, OWL.unionOf, None))):
            g.remove((mps, OWL.unionOf, o))
            try:
                rdflib.collection.Collection(g, o).clear()
            except Exception as e:
                logger.debug("unionOf collection clear 실패 (무시 가능): %s", e)
            bnode = BNode()
            g.add((mps, OWL.equivalentClass, bnode))
            union_list = BNode()
            g.add((bnode, OWL.unionOf, union_list))
            col = rdflib.collection.Collection(g, union_list)
            for cls in PROCESS_STEP_CLASSES:
                col.append(DOMAIN_NS_OBJ[cls])
            stats["mps_fixed"] = True

        mps_links_added = 0
        for cls_name in PROCESS_STEP_CLASSES:
            child = DOMAIN_NS_OBJ[cls_name]
            if not _class_is_present(child):
                continue
            if (child, RDF.type, OWL.Class) not in g:
                g.add((child, RDF.type, OWL.Class))
            if (child, RDFS.subClassOf, mps) in g:
                continue
            g.add((child, RDFS.subClassOf, mps))
            mps_links_added += 1
        stats["mps_subclass_links_added"] = mps_links_added

    return StepResult(
        name="step_06_mps_union",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=6,
        step_label="MPS_equivalentClass",
    )
