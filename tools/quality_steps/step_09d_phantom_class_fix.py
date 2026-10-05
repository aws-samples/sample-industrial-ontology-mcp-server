"""Step 9d — 미존재 (phantom) 클래스 URI → 가장 유사한 클래스로 교정.

본문 ontology_quality.py 의 Step 9d 블록을 그대로 모듈로 옮김.

LLM 이 Supplier (존재하지 않음) 대신 SupplierMaster (존재) 를 써야 하는
경우 등을 자동 교정. PascalCase-aware prefix 매칭 → fallback 으로 owl:Thing.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.graph_utils import split_embedded_prefix
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns
    existing_classes = {
        str(c): c for c in g.subjects(RDF.type, OWL.Class)
        if str(c).startswith(steel_str)
    }
    existing_class_names = {
        str(c).split("#")[-1]: c for c in existing_classes.values()
    }
    phantom_fixed = 0

    for prop_type in (OWL.ObjectProperty, OWL.DatatypeProperty):
        for prop in set(g.subjects(RDF.type, prop_type)):
            if not str(prop).startswith(steel_str):
                continue
            for pred in (RDFS.domain, RDFS.range):
                for val in list(g.objects(prop, pred)):
                    val_str = str(val)
                    if not val_str.startswith(steel_str):
                        continue
                    if val_str in existing_classes:
                        continue
                    # 유령 IRI (`<DOMAIN_NS + "iof-core:X">`) 는 **먼저 복구**한다.
                    # 예전엔 '#' 로만 잘라 phantom_name='iof-core:X' 를 얻었고,
                    # 어떤 클래스와도 매칭되지 않아 owl:Thing 으로 덮어썼다 —
                    # step_00 이 유령을 고쳐도 이 스텝이 같은 IOF 매핑을 다시
                    # 파괴하는 **독립된 두 번째 손실 경로** 였다 (2026-08-09 실측).
                    split = split_embedded_prefix(val_str, g)
                    if split is not None:
                        namespace, local = split
                        recovered = URIRef(namespace + local)
                        if recovered != val:
                            g.remove((prop, pred, val))
                            g.add((prop, pred, recovered))
                            logger.info(
                                "유령 IRI 복구: %s %s %s → %s",
                                str(prop).split("#")[-1],
                                "domain" if pred == RDFS.domain else "range",
                                val_str, str(recovered),
                            )
                            phantom_fixed += 1
                            # 복구된 IRI 가 도메인 클래스로 선언돼 있으면 완료.
                            # 외래 IRI 면 이 스텝의 대상이 아니다 (09a2 정책 소관).
                            if (str(recovered) in existing_classes
                                    or not str(recovered).startswith(steel_str)):
                                continue
                            val, val_str = recovered, str(recovered)
                    phantom_name = val_str.split("#")[-1]
                    best_match = None
                    phantom_lower = phantom_name.lower()
                    for cls_name, cls_uri in existing_class_names.items():
                        cls_lower = cls_name.lower()
                        if (cls_lower == phantom_lower
                                or cls_lower.startswith(phantom_lower)
                                or phantom_lower.startswith(cls_lower)):
                            best_match = cls_uri
                            break
                    if best_match:
                        g.remove((prop, pred, val))
                        g.add((prop, pred, best_match))
                        prop_name = str(prop).split("#")[-1]
                        logger.info(
                            "팬텀 클래스 교정: %s %s %s → %s",
                            prop_name,
                            "domain" if pred == RDFS.domain else "range",
                            phantom_name,
                            str(best_match).split("#")[-1],
                        )
                        phantom_fixed += 1
                    else:
                        g.remove((prop, pred, val))
                        g.add((prop, pred, OWL.Thing))
                        prop_name = str(prop).split("#")[-1]
                        logger.info(
                            "팬텀 클래스 → owl:Thing: %s %s %s",
                            prop_name,
                            "domain" if pred == RDFS.domain else "range",
                            phantom_name,
                        )
                        phantom_fixed += 1

    return StepResult(
        name="step_09d_phantom_class_fix",
        stats={"phantom_class_fixed": phantom_fixed},
        triples_delta=len(g) - before,
        step_number="9d",
        step_label="phantom_class_fix",
    )
