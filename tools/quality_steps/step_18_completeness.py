"""Step 18 — Completeness Annotation (CWA/OWA 브릿지).

CSV 뒷받침 클래스는 "closed" (CWA), 암묵지 전용은 "open" (OWA), 추론 전용은
"inferred" (검증 스킵) 로 마킹. 카디널리티 위반 심각도 분기에 사용.

본 step 은 CSV / tacit / 추론 클래스 set 을 모아 _annotate_completeness 에
넘긴다. 입력 디렉토리 스캔 실패는 inferred class 추정 정확도만 영향 — warn 처리.
"""
from __future__ import annotations

import glob
import logging
import os

from rdflib import OWL, RDF, Graph

from config import SOURCE_RAWDATA_DIR
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _annotate_completeness
    before = len(g)
    completeness_count = 0
    error: str | None = None
    try:
        steel_str = ctx.domain_ns

        csv_classes: set[str] = set()
        try:
            if os.path.isdir(SOURCE_RAWDATA_DIR):
                for fn in os.listdir(SOURCE_RAWDATA_DIR):
                    if fn.lower().endswith(".csv"):
                        csv_classes.add(fn.rsplit(".", 1)[0].replace("_", ""))
        except Exception as e:
            logger.debug("CSV 클래스 스캔 실패 (추론 클래스 추정에만 영향): %s", e)

        tacit_classes: set[str] = set()
        try:
            from config import SOURCE_TACIT_DIR
            if os.path.isdir(SOURCE_TACIT_DIR):
                for ttl_file in sorted(
                    glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")),
                ):
                    tg = _new_graph()
                    tg.parse(ttl_file, format="turtle")
                    for obj in tg.objects(None, RDF.type):
                        obj_str = str(obj)
                        if obj_str.startswith(steel_str):
                            tacit_classes.add(obj_str.split("#")[-1])
        except Exception as e:
            logger.debug("tacit 클래스 스캔 실패 (추론 클래스 추정에만 영향): %s", e)

        all_class_names = {
            _local_name(str(c)) for c in g.subjects(RDF.type, OWL.Class)
            if str(c).startswith(steel_str) and _local_name(str(c))
        }
        inferred_classes = all_class_names - csv_classes - tacit_classes

        completeness_count = _annotate_completeness(
            g, csv_classes, tacit_classes, inferred_classes,
        )
    except Exception as e:
        logger.warning("Completeness annotation 실패: %s", e)
        error = str(e)
    return StepResult(
        name="step_18_completeness",
        stats={"completeness_annotated": completeness_count},
        triples_delta=len(g) - before,
        error=error,
        step_number=18,
        step_label="completeness_annotation",
    )
