"""Step 11b — CSV-derived 클래스 자동 생성.

본문 ontology_quality.py 의 Step 11b 블록을 그대로 모듈로 옮김.

Multi-Agent 가 일부 master 테이블 (ItemMaster, ProductMaster,
MaintenanceHistory, PurchaseOrder, MonitoringPointMaster 등) 에 대해 클래스
선언을 생략하는 경우, 하류의 step 15b FK-OP 자동 생성 + S9 CW master 고립
검증이 모두 target 클래스 존재를 전제로 하므로 CSV 테이블명에 매칭되는
클래스를 자동 선언.

CSV 누락 컬럼 → class-specific DP 주입은 Step 12d (csv_dp_inject) 가 전담한다.
이 step 은 클래스 합성만 담당해 책임을 분리한다.

monkeypatch 호환: ``oq`` 모듈 globals 우선 + ``config`` fallback.
"""
from __future__ import annotations

import logging
import os
import re

from rdflib import OWL, RDF, RDFS, Graph, Literal

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    import config
    from tools import ontology_quality as _oq
    from tools.ontology_quality import DOMAIN_NS_OBJ

    SOURCE_RAWDATA_DIR = (
        getattr(_oq, "SOURCE_RAWDATA_DIR", None)
        or config.SOURCE_RAWDATA_DIR
    )
    before = len(g)
    csv_classes_added = 0
    try:
        if SOURCE_RAWDATA_DIR and os.path.isdir(SOURCE_RAWDATA_DIR):
            # 기존 클래스의 local name 을 소문자 키로 인덱싱 — 대소문자만 다른
            # 중복 클래스 (예: TemperatureHistory vs temperaturehistory) 방지.
            existing_lower = {
                str(s).rsplit("#", 1)[-1].rsplit("/", 1)[-1].lower()
                for s in g.subjects(RDF.type, OWL.Class)
            }
            for fname in sorted(os.listdir(SOURCE_RAWDATA_DIR)):
                if not fname.lower().endswith(".csv"):
                    continue
                base = fname.rsplit(".", 1)[0]
                # CSV 파일명 → PascalCase 클래스명 (temperature_history → TemperatureHistory).
                # uri_conventions / abox_generation._table_to_class 와 동일한 규칙.
                class_name = "".join(
                    p.capitalize() for p in base.replace("-", "_").split("_")
                )
                # 대소문자 무관 중복 체크 — Multi-Agent 가 이미 PascalCase 로
                # 선언한 클래스를 소문자/다른 케이스로 재생성하지 않는다.
                if class_name.lower() in existing_lower:
                    continue
                cls_uri = DOMAIN_NS_OBJ[class_name]
                if (cls_uri, RDF.type, OWL.Class) in g:
                    continue
                label_en = re.sub(r"(?<!^)(?=[A-Z])", " ", class_name)
                g.add((cls_uri, RDF.type, OWL.Class))
                g.add((cls_uri, RDFS.label, Literal(label_en, lang="en")))
                g.add((cls_uri, RDFS.label, Literal(label_en, lang="ko")))
                g.add((cls_uri, RDFS.comment, Literal(
                    f"CSV 소스 {fname} 에서 자동 생성된 클래스", lang="ko")))
                existing_lower.add(class_name.lower())
                csv_classes_added += 1
                logger.info("CSV-derived 클래스 자동 생성: %s", class_name)
    except Exception as e:
        logger.warning("CSV 클래스 자동 생성 실패: %s", e)

    return StepResult(
        name="step_11b_csv_class_synthesis",
        stats={"csv_classes_added": csv_classes_added},
        triples_delta=len(g) - before,
        step_number=11.5,
        step_label="csv_class_synthesis",
    )
