"""Step 14 — 2차 계층 서브그룹 생성 (NOC/DIT 추가 향상).

본문 ontology_quality.py 의 Step 14 블록을 그대로 모듈로 옮김.

자식 4개 이상인 도메인 중간 클래스를 서브그룹으로 분할하여 계층 깊이
(DIT) 를 높이고 부모-자식 분포 (NOC) 를 개선. 끝에 transitive reduction
한 번 더 호출 (Step 12 / 14 가 모두 끝난 시점 안전망).
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, Literal

from domain.graph_utils import would_violate_disjoint
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import (
        DOMAIN_NS_OBJ,
        _load_sub_group_config,
        _remove_redundant_subclass,
    )

    before = len(g)
    steel_str = ctx.domain_ns
    subgroup_classes_added = 0
    subgroup_moves = 0
    subgroup_disjoint_skipped = 0
    ap_redundant_removed_step14 = 0
    try:
        existing_classes = {
            str(c) for c in g.subjects(RDF.type, OWL.Class)
            if str(c).startswith(steel_str)
        }

        _SUB_GROUP_CONFIG = _load_sub_group_config()

        for parent_name, sub_groups in _SUB_GROUP_CONFIG.items():
            parent_uri = DOMAIN_NS_OBJ[parent_name]
            if str(parent_uri) not in existing_classes:
                continue

            for sg in sub_groups:
                sg_uri = DOMAIN_NS_OBJ[sg["class"]]
                if str(sg_uri) not in existing_classes:
                    g.add((sg_uri, RDF.type, OWL.Class))
                    g.add((sg_uri, RDFS.label, Literal(sg["label_en"], lang="en")))
                    g.add((sg_uri, RDFS.label, Literal(sg["label_ko"], lang="ko")))
                    g.add((sg_uri, RDFS.comment, Literal(sg["comment_ko"], lang="ko")))
                    g.add((sg_uri, RDFS.subClassOf, parent_uri))
                    subgroup_classes_added += 1
                    existing_classes.add(str(sg_uri))

                for child_name in sg["children"]:
                    child_uri = DOMAIN_NS_OBJ[child_name]
                    if str(child_uri) not in existing_classes:
                        continue
                    # 이미 다른 disjoint 그룹에 속한 클래스에 두 번째 그룹을
                    # 붙이면 unsatisfiable 이 되어 A-Box 인스턴스가 생성되지 않는다.
                    # 실측 (2026-08-11): 이 경로가 TagMaster(MonitoringManagement)
                    # 에 EquipmentAsset(→EquipmentManagement) 을 붙였다.
                    conflict = would_violate_disjoint(g, child_uri, sg_uri)
                    if conflict:
                        logger.warning(
                            "서브그룹 부모 추가 건너뜀: %s ⊑ %s — disjoint 충돌 "
                            "(%s ⊥ %s). 기존 부모를 그대로 유지한다",
                            child_name, sg["class"], conflict[0], conflict[1],
                        )
                        subgroup_disjoint_skipped += 1
                        continue
                    if (child_uri, RDFS.subClassOf, parent_uri) in g:
                        g.remove((child_uri, RDFS.subClassOf, parent_uri))
                        subgroup_moves += 1
                    if (child_uri, RDFS.subClassOf, sg_uri) not in g:
                        g.add((child_uri, RDFS.subClassOf, sg_uri))

    except Exception as e:
        logger.warning("서브그룹 생성 실패: %s", e)

    ap_redundant_removed_step14 = _remove_redundant_subclass(g, steel_str)

    return StepResult(
        name="step_14_subgroup_creation",
        stats={
            "subgroup_classes_added": subgroup_classes_added,
            "subgroup_disjoint_conflicts_skipped": subgroup_disjoint_skipped,
            "subgroup_moves": subgroup_moves,
            "subgroup_redundant_reduced": ap_redundant_removed_step14,
            "_step14_redundant_delta": ap_redundant_removed_step14,
        },
        triples_delta=len(g) - before,
        step_number=14,
        step_label="subgroup_creation",
    )
