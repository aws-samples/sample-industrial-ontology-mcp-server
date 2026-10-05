"""시맨틱 딕셔너리 품질 검증 도구

생성된 시맨틱 딕셔너리 JSON이 T-Box와 정합하는지,
필수 섹션이 빠짐없이 채워져 있는지 검증한다.
"""

import json
import logging
import os

from rdflib import OWL, RDF, RDFS, URIRef

from config import (
    ABOX_PATH,
    GENERATED_ABOX_DIR,
    GENERATED_DIR,
    GENERATED_TBOX_DIR,
    SEMANTIC_DICT_PATH,
    TBOX_PATH,
)
from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.common import error_response, resolve_child_path

logger = logging.getLogger(__name__)

_STEEL = DOMAIN_NS


def _axiom_node_names(tbox, class_names) -> set[str]:
    """딕셔너리 클래스 중 **명명된 공리 노드**의 이름 집합.

    ``Union_*`` / ``*_someValuesFrom`` 같은 skolemize 산물은 ``owl:Class`` 로
    선언되지만 분류 노드가 아니다 — 사람이 읽는 라벨이 없는 것이 정상이고 NL→SPARQL
    진입점도 되지 않는다. 판정은 ``graph_utils.is_anonymous_class_expression`` 정본을
    쓴다 (이름 패턴을 여기서 다시 짐작하면 사본이 갈린다).
    """
    from domain.graph_utils import is_anonymous_class_expression

    ns = str(DOMAIN_NS)
    axiom: set[str] = set()
    for name in class_names:
        try:
            if is_anonymous_class_expression(tbox, URIRef(ns + name), ns):
                axiom.add(name)
        except Exception:                     # pragma: no cover — 판정 실패는 미배제
            continue
    return axiom


def _validate(dict_data: dict, tbox_path: str, abox_path: str) -> dict:
    """시맨틱 딕셔너리를 T-Box/A-Box와 대조하여 검증한다."""
    issues = []
    warnings = []

    # ── 1. 필수 섹션 존재 확인 ──────────────────────
    required_sections = [
        "metadata", "classes", "object_properties",
        "sparql_guide", "common_mistakes", "question_templates",
        "class_quick_reference", "process_flow",
    ]
    for section in required_sections:
        if section not in dict_data:
            issues.append({
                "rule": "missing_section",
                "severity": "critical",
                "message": f"필수 섹션 누락: {section}",
            })
        elif not dict_data[section]:
            warnings.append({
                "rule": "empty_section",
                "severity": "warning",
                "message": f"섹션이 비어있음: {section}",
            })

    # sparql_guide 하위 섹션
    sparql_guide = dict_data.get("sparql_guide", {})
    for sub in ["engine_compatibility", "anti_patterns", "common_patterns"]:
        if sub not in sparql_guide:
            issues.append({
                "rule": "missing_sparql_subsection",
                "severity": "high",
                "message": f"sparql_guide.{sub} 누락",
            })
        elif not sparql_guide[sub]:
            warnings.append({
                "rule": "empty_sparql_subsection",
                "severity": "warning",
                "message": f"sparql_guide.{sub}가 비어있음",
            })

    # ── 2. T-Box 정합성 ────────────────────────────
    tbox = _new_graph()
    if os.path.exists(tbox_path):
        tbox.parse(tbox_path, format="turtle")

    # T-Box 클래스 목록
    tbox_classes = set()
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef) and str(cls).startswith(_STEEL):
            tbox_classes.add(_local_name(cls))

    dict_classes = set(dict_data.get("classes", {}).keys())

    # 딕셔너리에 없는 T-Box 클래스
    missing_in_dict = tbox_classes - dict_classes
    for cls in sorted(missing_in_dict):
        issues.append({
            "rule": "class_missing_in_dict",
            "severity": "high",
            "message": f"T-Box 클래스가 딕셔너리에 누락: {cls}",
        })

    # 딕셔너리에는 있지만 T-Box 에 없는 클래스 = **유령**.
    #
    # 이 규칙은 예전에 ``warning`` 이라 ``passed`` 에 영향이 없었고, 그래서 낡은
    # 딕셔너리가 게이트를 그대로 통과했다 (실측 2026-08-11: 삭제된 GHGScope /
    # ghgScopeEnum 을 그대로 선언한 딕셔너리가 ``passed=True``,
    # ``class_match_rate=100.0``). 딕셔너리는 LLM NL→SPARQL 레퍼런스라 존재하지
    # 않는 클래스를 제시하면 질의가 조용히 0건을 반환한다 — 누락(high)과 **대칭
    # 심각도** 여야 한다.
    extra_in_dict = dict_classes - tbox_classes
    for cls in sorted(extra_in_dict):
        issues.append({
            "rule": "class_extra_in_dict",
            "severity": "high",
            "message": (
                f"딕셔너리에 있지만 T-Box 에 없는 클래스(유령): {cls} — "
                "generate_semantic_dictionary 로 재생성 필요"
            ),
        })

    # T-Box ObjectProperty 목록
    tbox_obj_props = set()
    for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if isinstance(prop, URIRef) and str(prop).startswith(_STEEL):
            tbox_obj_props.add(_local_name(prop))

    dict_obj_props = set(dict_data.get("object_properties", {}).keys())

    missing_ops = tbox_obj_props - dict_obj_props
    for prop in sorted(missing_ops):
        issues.append({
            "rule": "objprop_missing_in_dict",
            "severity": "high",
            "message": f"T-Box ObjectProperty가 딕셔너리에 누락: {prop}",
        })

    # 유령 OP — 누락만 보던 단방향 검사를 대칭화한다. 실측: 낡은 딕셔너리에
    # 유령 OP 125개가 있었는데 규칙이 없어 전부 통과했다.
    extra_ops = dict_obj_props - tbox_obj_props
    for prop in sorted(extra_ops):
        issues.append({
            "rule": "objprop_extra_in_dict",
            "severity": "high",
            "message": (
                f"딕셔너리에 있지만 T-Box 에 없는 ObjectProperty(유령): {prop}"
            ),
        })

    # ── DatatypeProperty 양방향 대조 ────────────────
    #
    # 예전에는 DP 를 **전혀 대조하지 않았다** (실측: 낡은 딕셔너리에 유령 DP 33개
    # + 누락 29개, 검출 0건).
    #
    # 스코프 주의: 딕셔너리의 DP 는 ``classes[*]["datatype_properties"]`` 아래에만
    # 있으므로, **딕셔너리에 등재된 클래스를 domain 으로 갖는 T-Box DP** 와만
    # 비교해야 한다. 그러지 않으면 domain 이 없거나 ``owl:Thing`` 인 DP, 혹은
    # 딕셔너리에 없는 클래스의 DP 가 "누락" 으로 오검출된다 (비대칭 비교).
    tbox_dps: set[str] = set()
    for prop in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(prop, URIRef) and str(prop).startswith(_STEEL)):
            continue
        owners = {
            _local_name(d) for d in tbox.objects(prop, RDFS.domain)
            if isinstance(d, URIRef) and str(d).startswith(_STEEL)
        }
        if owners & dict_classes:
            tbox_dps.add(_local_name(prop))

    dict_dps: set[str] = set()
    for cls_info in (dict_data.get("classes") or {}).values():
        if isinstance(cls_info, dict):
            dict_dps.update((cls_info.get("datatype_properties") or {}).keys())

    for prop in sorted(tbox_dps - dict_dps):
        issues.append({
            "rule": "dp_missing_in_dict",
            "severity": "high",
            "message": f"T-Box DatatypeProperty가 딕셔너리에 누락: {prop}",
        })
    for prop in sorted(dict_dps - tbox_dps):
        issues.append({
            "rule": "dp_extra_in_dict",
            "severity": "high",
            "message": (
                f"딕셔너리에 있지만 T-Box 에 없는 DatatypeProperty(유령): {prop}"
            ),
        })

    # ── 3. 레이블 완전성 ───────────────────────────
    #
    # 명명된 공리 노드(``Union_*`` / ``*_someValuesFrom`` 등)는 라벨 검사 대상이
    # 아니다. skolemize 산물이라 사람이 읽는 이름이 없는 것이 정상이고, 분류 노드가
    # 아니므로 NL→SPARQL 진입점도 되지 않는다.
    #
    # 실측 2026-08-29: S2 재실행에서 ``Union_*`` 7개가 생기자 이 검사가 high 7건을
    # 보고해 S11 이 처음으로 FAIL 했다 (이전 실행 high 0). 이 리포는 같은 혼동으로
    # 클래스 수가 2.4배 부풀고 baseline 이 FAIL 로 고정된 이력이 있다 —
    # ``graph_utils.is_anonymous_class_expression`` 이 정본 판정이다.
    classes_data = dict_data.get("classes", {})
    axiom_nodes = _axiom_node_names(tbox, classes_data)
    if axiom_nodes:
        logger.debug(
            "라벨 검사에서 공리 노드 %d개 제외: %s",
            len(axiom_nodes), sorted(axiom_nodes)[:5],
        )
    for cls_name, cls_info in classes_data.items():
        if cls_name in axiom_nodes:
            continue
        if not cls_info.get("label_ko"):
            issues.append({
                "rule": "missing_label_ko",
                "severity": "high",
                "message": f"클래스 {cls_name}에 label_ko 없음",
            })
        if not cls_info.get("label_en"):
            warnings.append({
                "rule": "missing_label_en",
                "severity": "warning",
                "message": f"클래스 {cls_name}에 label_en 없음",
            })
        if not cls_info.get("description_ko"):
            warnings.append({
                "rule": "missing_description_ko",
                "severity": "warning",
                "message": f"클래스 {cls_name}에 description_ko 없음",
            })

    # ObjectProperty 레이블
    for prop_name, prop_info in dict_data.get("object_properties", {}).items():
        if not prop_info.get("label_ko"):
            warnings.append({
                "rule": "missing_op_label_ko",
                "severity": "warning",
                "message": f"ObjectProperty {prop_name}에 label_ko 없음",
            })

    # ── 4. 관계 일관성 ─────────────────────────────
    for prop_name, prop_info in dict_data.get("object_properties", {}).items():
        domains = prop_info.get("domain", [])
        ranges = prop_info.get("range", [])
        for d in domains:
            if d not in dict_classes and d != "Thing":
                issues.append({
                    "rule": "op_domain_not_in_classes",
                    "severity": "high",
                    "message": f"ObjectProperty {prop_name}의 domain '{d}'가 딕셔너리 클래스에 없음",
                })
        for r in ranges:
            if r not in dict_classes and r != "Thing":
                issues.append({
                    "rule": "op_range_not_in_classes",
                    "severity": "high",
                    "message": f"ObjectProperty {prop_name}의 range '{r}'가 딕셔너리 클래스에 없음",
                })

    # ── 5. class_quick_reference 완전성 ─────────────
    quick_ref = dict_data.get("class_quick_reference", {})
    for cls_name in dict_classes:
        if cls_name in axiom_nodes:
            continue                          # 공리 노드는 질의 속성을 갖지 않는다
        if cls_name not in quick_ref:
            warnings.append({
                "rule": "missing_quick_ref",
                "severity": "warning",
                "message": f"class_quick_reference에 {cls_name} 누락",
            })
        elif not quick_ref.get(cls_name):
            warnings.append({
                "rule": "empty_quick_ref",
                "severity": "warning",
                "message": f"class_quick_reference[{cls_name}]가 비어있음",
            })

    # ── 6. A-Box 통계 확인 ─────────────────────────
    has_abox = os.path.exists(abox_path)
    zero_instance_classes = []
    empty_dp_count = 0
    if has_abox:
        for cls_name, cls_info in classes_data.items():
            if cls_info.get("instance_count", 0) == 0:
                zero_instance_classes.append(cls_name)
            # v2 딕셔너리는 DP 에 is_populated 신호를 부여. 빈 DP 수를 집계해
            # NL→SPARQL 소비자가 피해야 할 DP 규모를 노출 (info — passed 무영향).
            for dp_info in cls_info.get("datatype_properties", {}).values():
                if dp_info.get("is_populated") is False:
                    empty_dp_count += 1
        if zero_instance_classes:
            warnings.append({
                "rule": "zero_instances",
                "severity": "info",
                "message": f"A-Box가 있지만 인스턴스 0개인 클래스 {len(zero_instance_classes)}개: {', '.join(sorted(zero_instance_classes)[:10])}",
            })
        if empty_dp_count:
            warnings.append({
                "rule": "empty_datatype_properties",
                "severity": "info",
                "message": f"is_populated=false 인 DatatypeProperty {empty_dp_count}개 (A-Box 값 없음 — SPARQL 작성 시 회피 권장)",
            })

    # ── 7. question_templates 검증 ──────────────────
    templates = dict_data.get("question_templates", {})
    if len(templates) < 5:
        warnings.append({
            "rule": "few_question_templates",
            "severity": "warning",
            "message": f"question_templates가 {len(templates)}개로 적음 (권장: 10개 이상)",
        })

    # ── 결과 집계 ───────────────────────────────────
    all_issues = issues + warnings
    critical = len([i for i in all_issues if i["severity"] == "critical"])
    high = len([i for i in all_issues if i["severity"] == "high"])
    warning_count = len([i for i in all_issues if i["severity"] == "warning"])
    info_count = len([i for i in all_issues if i["severity"] == "info"])

    passed = critical == 0 and high == 0

    return {
        "passed": passed,
        "summary": {
            "critical": critical,
            "high": high,
            "warning": warning_count,
            "info": info_count,
            "total": len(all_issues),
        },
        "coverage": {
            "tbox_classes": len(tbox_classes),
            "dict_classes": len(dict_classes),
            "class_match_rate": round(len(tbox_classes & dict_classes) / max(len(tbox_classes), 1) * 100, 1),
            "tbox_object_properties": len(tbox_obj_props),
            "dict_object_properties": len(dict_obj_props),
            "op_match_rate": round(len(tbox_obj_props & dict_obj_props) / max(len(tbox_obj_props), 1) * 100, 1),
            "tbox_datatype_properties": len(tbox_dps),
            "dict_datatype_properties": len(dict_dps),
            "dp_match_rate": round(
                len(tbox_dps & dict_dps) / max(len(tbox_dps), 1) * 100, 1,
            ),
            "has_abox": has_abox,
            "zero_instance_classes": len(zero_instance_classes) if has_abox else "N/A",
            "empty_datatype_properties": empty_dp_count if has_abox else "N/A",
        },
        "sections": {
            section: section in dict_data and bool(dict_data[section])
            for section in required_sections
        },
        "issues": all_issues,
    }


def validate_semantic_dictionary(dict_path: str = "", tbox_path: str = "", abox_path: str = "") -> str:
    """시맨틱 딕셔너리의 품질을 검증한다.

    T-Box와의 정합성(클래스/프로퍼티 누락), 레이블 완전성,
    관계 일관성, 필수 섹션 존재 여부 등을 체크한다.

    Args:
        dict_path: data/generated 아래 딕셔너리 JSON 파일명.
        tbox_path: data/generated/tbox 아래 T-Box TTL 파일명.
        abox_path: data/generated/abox 아래 A-Box TTL 파일명.
    """
    try:
        dp = (
            resolve_child_path(
                GENERATED_DIR,
                dict_path,
                allowed_suffixes=(".json",),
            )
            if dict_path
            else SEMANTIC_DICT_PATH
        )
        tp = (
            resolve_child_path(
                GENERATED_TBOX_DIR,
                tbox_path,
                allowed_suffixes=(".ttl",),
            )
            if tbox_path
            else TBOX_PATH
        )
        ap = (
            resolve_child_path(
                GENERATED_ABOX_DIR,
                abox_path,
                allowed_suffixes=(".ttl",),
            )
            if abox_path
            else ABOX_PATH
        )
    except Exception as e:
        return error_response(e, logger=logger)

    if not os.path.exists(dp):
        return error_response(f"시맨틱 딕셔너리가 없습니다: {dp}", hint="generate_semantic_dictionary를 먼저 실행하세요.", logger=logger)

    try:
        with open(dp, encoding="utf-8") as f:
            dict_data = json.load(f)
    except json.JSONDecodeError as e:
        return json.dumps({
            "error": f"JSON 파싱 실패: {e}",
        }, ensure_ascii=False)

    result = _validate(dict_data, tp, ap)
    result["success"] = True
    return json.dumps(result, ensure_ascii=False, indent=2)
