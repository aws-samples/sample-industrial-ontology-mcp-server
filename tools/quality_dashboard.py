"""quality_dashboard.py — Dashboard, Tracing, Impact.

품질 대시보드 통합, 인과 추적, 변경 영향 사전 예측.
"""
from __future__ import annotations

import json
import logging
import os

from rdflib import OWL, RDF, Graph, URIRef

from config import (
    ABOX_PATH,
    GENERATED_TBOX_DIR,
    SEMANTIC_DICT_PATH,
    TBOX_BASELINE_PATH,
    TBOX_PATH,
)
from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.common import error_response, resolve_child_path
from tools.validation_core import _check_quality_rules_internal, _load_ttl

logger = logging.getLogger(__name__)


# ── Q2: Cross-tool 메트릭 인과 추적 ──────────────────


# 규칙 위반 → 영향받는 메트릭 매핑.
#
# **값은 ``measure_tbox_metrics`` 의 실제 메트릭 키여야 한다.** 아래 조인이
# ``wm in t["affects_metrics"]`` 로 문자열 일치를 보기 때문에, 이름이 하나라도
# 어긋나면 그 계열의 근본원인이 **조용히 전부 사라진다**. 2026-08-08 실측:
# ``annotation`` 이라고 적혀 있었지만 실제 키는 ``annotation_completeness`` 여서
# 라벨·주석·명명 규칙 위반 4종의 근본원인이 100% 유실됐다 (조인 영구 실패).
# 계약은 ``tests/test_quality_dashboard.py`` 가 고정한다.
_RULE_METRIC_IMPACT: dict[str, list[str]] = {
    "missing_domain": ["rr", "ar"],
    "missing_range": ["rr"],
    "missing_label": ["annotation_completeness"],
    "missing_comment": ["annotation_completeness"],
    "isolated_class": ["ar", "cc"],
    "multiple_domains": ["rr"],
    "multiple_ranges": ["rr"],
    "naming_class": ["annotation_completeness"],
    "naming_property": ["annotation_completeness"],
}


def trace_quality_issues(ttl_path: str = "") -> str:
    """T-Box 품질 규칙 위반과 메트릭 저하를 인과 관계로 연결하여 보고한다.

    check_quality_rules의 위반이 measure_tbox_metrics의 어떤 메트릭에
    영향을 미치는지 추적하여, 수정 우선순위를 판단할 수 있게 한다.

    Args:
        ttl_path: data/generated/tbox 아래 TTL 파일명. 비어있으면 기본 T-Box 사용.
    """
    from tools.tbox_metrics import measure_tbox_metrics

    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl("", ttl_path)
        g = _new_graph()
        g.parse(data=ttl, format="turtle")
        quality_result = _check_quality_rules_internal(g)
        metrics_result = json.loads(measure_tbox_metrics())

        # 규칙 위반 → 메트릭 영향 추적
        traced = []
        metric_impact_count: dict[str, int] = {}
        for issue in quality_result.get("issues", []):
            rule = issue.get("rule", "")
            affected = _RULE_METRIC_IMPACT.get(rule, [])
            if affected:
                traced.append({
                    "rule": rule,
                    "severity": issue.get("severity"),
                    "message": issue.get("message", "")[:120],
                    "affects_metrics": affected,
                })
                for m in affected:
                    metric_impact_count[m] = metric_impact_count.get(m, 0) + 1

        # WARNING 상태인 메트릭의 원인 분석
        warning_metrics = [
            name for name, data in metrics_result.get("metrics", {}).items()
            if isinstance(data, dict) and data.get("status") == "WARNING"
        ]
        root_causes = {}
        for wm in warning_metrics:
            causes = [t for t in traced if wm in t["affects_metrics"]]
            if causes:
                root_causes[wm] = {
                    "violation_count": len(causes),
                    "top_violations": [c["rule"] for c in causes[:5]],
                }

        return json.dumps({
            "success": True,
            "traced_violations": len(traced),
            "metric_impact_summary": metric_impact_count,
            "warning_metrics_with_causes": root_causes,
            "quality_gates": metrics_result.get("quality_gates"),
            "grade": metrics_result.get("summary", {}).get("grade"),
            "recommendation": "high severity 위반을 먼저 수정하면 WARNING 메트릭이 개선됩니다."
                if root_causes else "규칙 위반이 메트릭에 미치는 영향이 없습니다.",
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


# ── Q3: 통합 품질 대시보드 ────────────────────────────


def generate_quality_dashboard() -> str:
    """모든 품질 검증 결과를 통합하여 단일 대시보드로 반환한다.

    measure_tbox_metrics, check_quality_rules, detect_tbox_antipatterns의
    결과를 종합하여 overall score + pass/fail 판정 + 개선 권장사항을 제공.
    """
    from tools.tbox_metrics import detect_tbox_antipatterns, measure_tbox_metrics

    try:
        # T-Box를 한 번만 읽고, 내부 함수 또는 ttl_content 파라미터로 전달하여 중복 파싱 방지
        ttl = _load_ttl("", "")
        g = _new_graph()
        g.parse(data=ttl, format="turtle")

        metrics_result = json.loads(measure_tbox_metrics())
        quality_rules = _check_quality_rules_internal(g)
        antipatterns = json.loads(detect_tbox_antipatterns(ttl_content=ttl))

        # 종합 점수 (0~100)
        metrics_score = metrics_result.get("summary", {}).get("total_score", 0)
        critical_issues = quality_rules.get("critical", 0)
        high_issues = quality_rules.get("high", 0)
        antipattern_total = antipatterns.get("total", 0)

        # 가중 종합: 메트릭 60% + 규칙 25% + 안티패턴 15%
        rule_score = max(0, 100 - critical_issues * 20 - high_issues * 5)
        ap_score = max(0, 100 - antipattern_total * 10)
        overall = round(metrics_score * 0.6 + rule_score * 0.25 + ap_score * 0.15, 1)
        overall = min(100, max(0, overall))

        gates = metrics_result.get("quality_gates", {})
        gates_passed = gates.get("all_passed", False) if gates else False

        ready = gates_passed and critical_issues == 0 and antipattern_total == 0

        # 개선 권장사항
        recommendations = []
        if critical_issues > 0:
            recommendations.append(f"critical 규칙 위반 {critical_issues}건 즉시 수정 필요")
        if not gates_passed and gates:
            failed_gates = [k for k, v in gates.items() if isinstance(v, dict) and not v.get("passed", True)]
            if failed_gates:
                recommendations.append(f"품질 게이트 미통과: {', '.join(failed_gates)}")
        if antipattern_total > 0:
            recommendations.append(f"안티패턴 {antipattern_total}건 정리 권장")
        if high_issues > 5:
            recommendations.append(f"high severity 위반 {high_issues}건 — improve_tbox_quality 재실행 권장")

        return json.dumps({
            "success": True,
            "overall_score": overall,
            "grade": "A" if overall >= 85 else "B" if overall >= 70 else "C" if overall >= 55 else "D",
            "ready_for_production": ready,
            "sections": {
                "metrics": {
                    "score": metrics_score,
                    "grade": metrics_result.get("summary", {}).get("grade"),
                    "gates_passed": gates_passed,
                },
                "quality_rules": {
                    "total_issues": quality_rules.get("issues_count", 0),
                    "critical": critical_issues,
                    "high": high_issues,
                    "warning": quality_rules.get("warning", 0),
                },
                "antipatterns": antipatterns.get("counts", {}),
            },
            "recommendations": recommendations if recommendations else ["품질 기준 충족 — 다음 단계 진행 가능"],
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


# ── T-Box 변경 영향 사전 예측 (Zablith et al. 2015) ────────


def estimate_change_impact(proposed_changes: str = "") -> str:
    """T-Box 변경의 영향을 사전에 예측한다. (Zablith et al. 2015)

    제안된 변경(클래스/프로퍼티 추가/삭제/수정)이 A-Box 인스턴스,
    시맨틱 딕셔너리, CQ 테스트에 미치는 영향을 추정한다.

    Args:
        proposed_changes: 변경 내용 JSON. 예: {"remove_classes": ["OldClass"], "rename_properties": [{"from": "oldProp", "to": "newProp"}]}
                         비어있으면 현재 T-Box와 baseline 비교로 변경사항 자동 감지.
    """
    try:
        # ── 1. 변경사항 결정: 명시적 JSON 또는 baseline 비교 ──
        changes: dict = {}
        if proposed_changes.strip():
            changes = json.loads(proposed_changes)
        else:
            if not os.path.exists(TBOX_PATH):
                return error_response(
                    "현재 T-Box가 없습니다.",
                    hint="generate_tbox로 먼저 생성하세요.",
                    logger=logger,
                )
            if not os.path.exists(TBOX_BASELINE_PATH):
                return error_response(
                    "베이스라인이 없습니다.",
                    hint="첫 파이프라인 실행 후 자동 생성됩니다. proposed_changes를 직접 지정하세요.",
                    logger=logger,
                )
            changes = _detect_changes_from_baseline()

        remove_classes = changes.get("remove_classes", [])
        rename_classes = changes.get("rename_classes", [])  # [{"from": "A", "to": "B"}]
        remove_properties = changes.get("remove_properties", [])
        rename_properties = changes.get("rename_properties", [])  # [{"from": "p", "to": "q"}]

        all_changed_classes = set(remove_classes)
        for rc in rename_classes:
            all_changed_classes.add(rc.get("from", ""))
        all_changed_props = set(remove_properties)
        for rp in rename_properties:
            all_changed_props.add(rp.get("from", ""))

        # 변경사항 없으면 early return
        if not all_changed_classes and not all_changed_props:
            return json.dumps({
                "success": True,
                "message": "변경사항이 감지되지 않았습니다.",
                "risk_level": "LOW",
                "affected_instances": 0,
                "affected_triples": 0,
                "affected_dict_entries": 0,
                "affected_cqs": 0,
            }, ensure_ascii=False, indent=2)

        steel_ns = DOMAIN_NS

        # ── 2. A-Box 영향 분석 ──
        affected_instances = 0
        affected_triples = 0
        instance_details: list[dict] = []
        triple_details: list[dict] = []

        if os.path.exists(ABOX_PATH):
            abox_g = _new_graph()
            abox_g.parse(ABOX_PATH, format="turtle")

            for cls_local in all_changed_classes:
                if not cls_local:
                    continue
                cls_uri = URIRef(steel_ns + cls_local)
                instances = list(abox_g.subjects(RDF.type, cls_uri))
                if instances:
                    affected_instances += len(instances)
                    instance_details.append({
                        "class": cls_local,
                        "instance_count": len(instances),
                    })

            for prop_local in all_changed_props:
                if not prop_local:
                    continue
                prop_uri = URIRef(steel_ns + prop_local)
                triples = list(abox_g.triples((None, prop_uri, None)))
                if triples:
                    affected_triples += len(triples)
                    triple_details.append({
                        "property": prop_local,
                        "triple_count": len(triples),
                    })

        # ── 3. 시맨틱 딕셔너리 영향 분석 ──
        affected_dict_entries = 0
        dict_details: list[dict] = []

        if os.path.exists(SEMANTIC_DICT_PATH):
            try:
                with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
                    sem_dict = json.load(f)
                dict_text = json.dumps(sem_dict, ensure_ascii=False)
                for entity in all_changed_classes | all_changed_props:
                    if not entity:
                        continue
                    count = dict_text.count(entity)
                    if count > 0:
                        affected_dict_entries += count
                        dict_details.append({
                            "entity": entity,
                            "reference_count": count,
                        })
            except (json.JSONDecodeError, OSError):
                pass

        # ── 4. CQ 영향 분석 ──
        affected_cqs = 0
        cq_details: list[dict] = []

        from config import COMPETENCY_QUESTIONS_PATH
        cq_path = COMPETENCY_QUESTIONS_PATH

        if os.path.exists(cq_path):
            try:
                with open(cq_path, encoding="utf-8") as f:
                    cqs = json.load(f)
                for cq in cqs:
                    cq_id = cq.get("id", "unknown")
                    cq_text = json.dumps(cq, ensure_ascii=False).lower()
                    cq_domains = [d.lower().replace("_", "") for d in cq.get("domains", [])]
                    matched_entities = []
                    for entity in all_changed_classes | all_changed_props:
                        if not entity:
                            continue
                        entity_lower = entity.lower()
                        if entity_lower in cq_text or entity_lower in cq_domains:
                            matched_entities.append(entity)
                    if matched_entities:
                        affected_cqs += 1
                        cq_details.append({
                            "cq_id": cq_id,
                            "matched_entities": matched_entities,
                        })
            except (json.JSONDecodeError, OSError):
                pass

        # ── 5. 위험도 산정 ──
        risk_level = "LOW"
        if affected_instances > 0 or affected_triples > 10:
            risk_level = "MEDIUM"
        if affected_instances > 50 or affected_triples > 100 or affected_cqs > 3:
            risk_level = "HIGH"

        return json.dumps({
            "success": True,
            "proposed_changes": {
                "remove_classes": remove_classes,
                "rename_classes": rename_classes,
                "remove_properties": remove_properties,
                "rename_properties": rename_properties,
            },
            "affected_instances": affected_instances,
            "affected_triples": affected_triples,
            "affected_dict_entries": affected_dict_entries,
            "affected_cqs": affected_cqs,
            "risk_level": risk_level,
            "details": {
                "instances": instance_details if instance_details else None,
                "triples": triple_details if triple_details else None,
                "dictionary": dict_details if dict_details else None,
                "competency_questions": cq_details if cq_details else None,
            },
        }, ensure_ascii=False, indent=2)

    except json.JSONDecodeError as e:
        return error_response(
            f"proposed_changes JSON 파싱 실패: {e}",
            hint='올바른 JSON 형식: {"remove_classes": ["OldClass"]}',
            logger=logger,
        )
    except Exception as e:
        return error_response(e, logger=logger)


def _detect_changes_from_baseline() -> dict:
    """현재 T-Box와 baseline을 비교하여 변경사항을 dict로 반환한다."""
    baseline_g = _new_graph()
    baseline_g.parse(TBOX_BASELINE_PATH, format="turtle")
    current_g = _new_graph()
    current_g.parse(TBOX_PATH, format="turtle")

    steel_ns = DOMAIN_NS

    def _local_names(g: Graph, rdf_type: URIRef) -> set[str]:
        return {
            str(s).split("#")[-1].split("/")[-1]
            for s in g.subjects(RDF.type, rdf_type)
            if str(s).startswith(steel_ns)
        }

    b_classes = _local_names(baseline_g, OWL.Class)
    c_classes = _local_names(current_g, OWL.Class)
    b_ops = _local_names(baseline_g, OWL.ObjectProperty)
    c_ops = _local_names(current_g, OWL.ObjectProperty)
    b_dps = _local_names(baseline_g, OWL.DatatypeProperty)
    c_dps = _local_names(current_g, OWL.DatatypeProperty)

    return {
        "remove_classes": sorted(b_classes - c_classes),
        "rename_classes": [],  # 자동 감지로는 rename 구별 불가
        "remove_properties": sorted((b_ops | b_dps) - (c_ops | c_dps)),
        "rename_properties": [],
        "added_classes": sorted(c_classes - b_classes),
        "added_properties": sorted((c_ops | c_dps) - (b_ops | b_dps)),
    }
