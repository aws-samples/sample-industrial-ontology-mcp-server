"""Step 11 — 암묵지 클래스 T-Box 보완.

본문 ontology_quality.py 의 Step 11 블록을 그대로 모듈로 옮김.

``data/source/tacit/*.ttl`` 에서 사용된 ``rdf:type`` 중 T-Box 에 없는
클래스를 자동 추가. tacit 룰이 ``steel:QualitySpecification`` 같은 클래스를
참조하지만 T-Box 에는 선언이 없는 경우 orphan triple 이 되므로 미리 채움.
"""
from __future__ import annotations

import glob
import logging
import os
import re

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    import config  # 테스트 monkeypatch 동적 반영
    from tools.ontology_quality import _new_graph

    before = len(g)
    steel_str = ctx.domain_ns
    tacit_classes_added = 0
    tacit_classes: set[str] = set()
    try:
        SOURCE_TACIT_DIR = config.SOURCE_TACIT_DIR
        existing_classes = {
            str(c) for c in g.subjects(RDF.type, OWL.Class)
            if str(c).startswith(steel_str)
        }
        if os.path.isdir(SOURCE_TACIT_DIR):
            for ttl_file in sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl"))):
                tg = _new_graph()
                tg.parse(ttl_file, format="turtle")
                for obj in tg.objects(None, RDF.type):
                    obj_str = str(obj)
                    if obj_str.startswith(steel_str):
                        tacit_classes.add(obj_str)

        for cls_uri_str in sorted(tacit_classes - existing_classes):
            cls_uri = URIRef(cls_uri_str)
            local_name = cls_uri_str.split("#")[-1]
            label_en = re.sub(r"(?<!^)(?=[A-Z])", " ", local_name)
            label_ko = label_en
            g.add((cls_uri, RDF.type, OWL.Class))
            g.add((cls_uri, RDFS.label, Literal(label_en, lang="en")))
            g.add((cls_uri, RDFS.label, Literal(label_ko, lang="ko")))
            g.add((cls_uri, RDFS.comment, Literal(
                "암묵지에서 사용되는 클래스 (자동 추가)", lang="ko")))
            tacit_classes_added += 1
            logger.info("암묵지 클래스 T-Box 추가: %s", local_name)
    except Exception as e:
        logger.warning("암묵지 클래스 보완 실패: %s", e)
    if tacit_classes_added == 0 and tacit_classes:
        logger.info(
            "암묵지 클래스 이미 존재, 추가 불필요 (%d개 확인)", len(tacit_classes)
        )

    return StepResult(
        name="step_11_tacit_class",
        stats={"tacit_classes_added": tacit_classes_added},
        triples_delta=len(g) - before,
        step_number=11,
        step_label="tacit_class_synthesis",
    )
