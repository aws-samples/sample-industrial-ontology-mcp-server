"""장애 패턴 탐지 도구 — 암묵지 기반 고장 예측 + 인과 분석

암묵지(tacit knowledge)에 정의된 FailurePattern과 OperationalRule을
KG 데이터에 대해 SPARQL 패턴 매칭으로 탐지한다.

OWL 추론이 관계를 완성한 후, 이 도구가 그 위에서 패턴을 탐지하는 2단계 구조:
  1단계: OWL 추론 (자동) — 타입 분류, inverseOf, subClassOf 체인
  2단계: SPARQL 패턴 매칭 (이 도구) — 알람 집계, 임계값 비교, 인과 규칙 조회
"""
from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

from rdflib import Graph, Namespace

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, NS_PREFIX, sanitize_sparql_value
from domain.sparql_templates import execute_local_sparql
from domain.uri_conventions import local_name as _local_name
from tools.common import error_response

logger = logging.getLogger(__name__)

DOMAIN_NS_OBJ = Namespace(DOMAIN_NS)
DOMAIN_INST_NS_OBJ = Namespace(DOMAIN_INST_NS)


def _load_graph(use_inferred: bool = True) -> Graph:
    """검증 대상 그래프를 로드한다."""
    from domain.tbox_utils import load_graph
    g, _ = load_graph(use_inferred=use_inferred)
    return g


def _query(g: Graph, sparql: str) -> list[dict]:
    return execute_local_sparql(g, sparql)


def _local(uri: str | None) -> str | None:
    return _local_name(uri) if uri else uri


def detect_failure_patterns(equipment_id: str = "", use_inferred: bool = True) -> str:
    """암묵지 FailurePattern 기반으로 고장 임박 설비를 탐지한다.

    각 FailurePattern의 alarmThreshold와 A-Box의 실제 알람 건수를 비교하여
    임계값을 초과한 설비를 보고한다.

    equipment_id가 지정되면 해당 설비만, 비어있으면 전체 설비를 검사.

    Args:
        equipment_id: 특정 설비 ID (예: "EQ001"). 비어있으면 전체 검사.
        use_inferred: True면 추론 결과(all_inferred.ttl) 사용.
    """
    try:
        g = _load_graph(use_inferred)
        pfx = NS_PREFIX

        # 1. FailurePattern 정의 추출
        patterns = _query(g, f"""
            SELECT ?pattern ?label ?threshold ?window ?failureType ?description WHERE {{
                ?pattern a {pfx}:FailurePattern .
                OPTIONAL {{ ?pattern rdfs:label ?label }}
                OPTIONAL {{ ?pattern {pfx}:alarmThreshold ?threshold }}
                OPTIONAL {{ ?pattern {pfx}:timeWindowHours ?window }}
                OPTIONAL {{ ?pattern {pfx}:predictedFailureType ?failureType }}
                OPTIONAL {{ ?pattern {pfx}:patternDescription ?description }}
            }}
        """)

        if not patterns:
            return json.dumps({
                "success": True,
                "message": "FailurePattern 암묵지가 없습니다. data/source/tacit/에 고장 패턴 TTL을 추가하세요.",
                "alerts": [],
            }, ensure_ascii=False, indent=2)

        # 2. 설비-패턴 매핑 + 알람 건수 집계
        equip_filter = ""
        if equipment_id:
            safe_id = sanitize_sparql_value(equipment_id)
            equip_filter = f'FILTER(CONTAINS(STR(?equipment), "{safe_id}"))'

        # 패턴별 timeWindowHours로 알람을 필터링해야 하므로,
        # Python에서 패턴별 cutoff를 계산하여 후처리한다.
        # rdflib는 xsd:dayTimeDuration 산술을 지원하지 않으므로 Python-side 접근.
        _DEFAULT_WINDOW_HOURS = 720

        # 패턴별 윈도우 맵 구축 {pattern_uri: window_hours}
        pattern_windows: dict[str, int] = {}
        for p in patterns:
            p_uri = p.get("pattern")
            w = p.get("window")
            try:
                pattern_windows[p_uri] = int(float(w)) if w else _DEFAULT_WINDOW_HOURS
            except (ValueError, TypeError):
                pattern_windows[p_uri] = _DEFAULT_WINDOW_HOURS

        # NOTE: 시간 필터는 alarmTimestamp DP가 있을 때만 효과적.
        # alarmTimestamp가 없는 AlarmEvents는 시간 필터를 통과 (OPTIONAL + !BOUND 폴백).
        # 패턴별 윈도우 적용은 Python 후처리로 수행한다.
        alerts_query = f"""
            SELECT ?equipment ?pattern ?failureType ?threshold ?description
                   (COUNT(?alarm) AS ?alarmCount) ?window WHERE {{
                ?equipment {pfx}:hasFailurePattern ?pattern .
                ?pattern a {pfx}:FailurePattern ;
                    {pfx}:alarmThreshold ?threshold ;
                    {pfx}:predictedFailureType ?failureType .
                OPTIONAL {{ ?pattern {pfx}:timeWindowHours ?window }}
                OPTIONAL {{ ?pattern {pfx}:patternDescription ?description }}
                OPTIONAL {{
                    ?alarm a {pfx}:AlarmEvents .
                    ?equipment {pfx}:hasTag ?tag .
                    ?alarm {pfx}:hasTag ?tag .
                }}
                {equip_filter}
            }} GROUP BY ?equipment ?pattern ?failureType ?threshold ?description ?window
            ORDER BY DESC(?alarmCount)
        """
        raw_alerts = _query(g, alerts_query)

        # Python 후처리: 패턴별 timeWindowHours에 맞게 알람 카운트를 재계산
        # (타임스탬프 기반 필터링이 필요한 경우 개별 알람 조회)
        now_utc = datetime.now(UTC)
        alerts = []
        for row in raw_alerts:
            alarm_count = int(row["alarmCount"]) if row.get("alarmCount") else 0
            threshold = int(row["threshold"]) if row.get("threshold") else 0

            # 패턴별 윈도우로 시간 기반 필터링
            p_uri = row.get("pattern")
            window_hours = _DEFAULT_WINDOW_HOURS
            with contextlib.suppress(ValueError, TypeError):
                window_hours = int(float(row["window"])) if row.get("window") else _DEFAULT_WINDOW_HOURS

            if alarm_count > 0 and window_hours < _DEFAULT_WINDOW_HOURS:
                # 더 짧은 윈도우가 설정된 패턴은 개별 알람 타임스탬프를 확인
                cutoff = (now_utc - timedelta(hours=window_hours)).isoformat()
                equip_uri = row["equipment"]
                ts_query = f"""
                    SELECT (COUNT(?alarm) AS ?cnt) WHERE {{
                        ?alarm a {pfx}:AlarmEvents .
                        <{equip_uri}> {pfx}:hasTag ?tag .
                        ?alarm {pfx}:hasTag ?tag .
                        OPTIONAL {{ ?alarm {pfx}:alarmTimestamp ?ts }}
                        FILTER(!BOUND(?ts) || ?ts >= "{cutoff}"^^xsd:dateTime)
                    }}
                """
                ts_result = _query(g, ts_query)
                alarm_count = int(ts_result[0]["cnt"]) if ts_result else 0

            if alarm_count >= threshold:
                row["alarmCount"] = str(alarm_count)
                alerts.append(row)

        # 2b. 설비에 할당되지 않은 고아 패턴 탐지
        orphaned_query = f"""
            SELECT ?pattern ?label WHERE {{
                ?pattern a {pfx}:FailurePattern .
                OPTIONAL {{ ?pattern rdfs:label ?label }}
                FILTER NOT EXISTS {{ ?equipment {pfx}:hasFailurePattern ?pattern }}
            }}
        """
        orphaned_patterns = _query(g, orphaned_query)
        orphaned_list = [{
            "pattern": _local(op["pattern"]),
            "label": op.get("label"),
        } for op in orphaned_patterns]
        if orphaned_list:
            logger.warning("설비에 할당되지 않은 FailurePattern %d개 발견", len(orphaned_list))

        # 3. 패턴 미초과 설비도 현황 보고
        all_equip_query = f"""
            SELECT ?equipment ?pattern ?failureType ?threshold (COUNT(?alarm) AS ?alarmCount) WHERE {{
                ?equipment {pfx}:hasFailurePattern ?pattern .
                ?pattern a {pfx}:FailurePattern ;
                    {pfx}:alarmThreshold ?threshold ;
                    {pfx}:predictedFailureType ?failureType .
                OPTIONAL {{
                    ?equipment {pfx}:hasTag ?tag .
                    ?alarm a {pfx}:AlarmEvents .
                    ?alarm {pfx}:hasTag ?tag .
                }}
                {equip_filter}
            }} GROUP BY ?equipment ?pattern ?failureType ?threshold
            ORDER BY DESC(?alarmCount)
        """
        all_status = _query(g, all_equip_query)

        alert_list = [{
            "equipment": _local(a["equipment"]),
            "predicted_failure": a.get("failureType"),
            "alarm_count": int(a["alarmCount"]) if a.get("alarmCount") else 0,
            "threshold": int(a["threshold"]) if a.get("threshold") else 0,
            "description": a.get("description"),
            "severity": "CRITICAL",
        } for a in alerts]

        status_list = [{
            "equipment": _local(s["equipment"]),
            "pattern": _local(s.get("pattern")),
            "predicted_failure": s.get("failureType"),
            "alarm_count": int(s["alarmCount"]) if s.get("alarmCount") else 0,
            "threshold": int(s["threshold"]) if s.get("threshold") else 0,
            "status": "ALERT" if (int(s.get("alarmCount") or 0) >= int(s.get("threshold") or 999)) else "NORMAL",
        } for s in all_status]

        return json.dumps({
            "success": True,
            "patterns_defined": len(patterns),
            "equipment_monitored": len({s["equipment"] for s in status_list}),
            "alerts_triggered": len(alert_list),
            "orphaned_patterns": len(orphaned_list),
            "alerts": alert_list,
            "orphaned_pattern_details": orphaned_list if orphaned_list else None,
            "equipment_status": status_list,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def analyze_causal_rules(parameter_name: str = "", direction: str = "") -> str:
    """암묵지 OperationalRule 기반으로 공정 인과 관계를 조회한다.

    특정 파라미터가 변화할 때 영향받는 파라미터와 방향을 보고한다.
    장애 발생 시 원인 파라미터를 역추적하는 데 사용.

    Args:
        parameter_name: 조회할 파라미터명 (예: "Hot_Air_Temp_C"). 비어있으면 전체 규칙 조회.
        direction: "increase" 또는 "decrease". 비어있으면 모든 방향.
    """
    try:
        g = _load_graph(use_inferred=True)
        pfx = NS_PREFIX

        filters = []
        if parameter_name:
            safe_param = sanitize_sparql_value(parameter_name)
            filters.append(f'FILTER(?causeParam = "{safe_param}" || ?effectParam = "{safe_param}")')
        if direction:
            safe_dir = sanitize_sparql_value(direction)
            filters.append(f'FILTER(?causeDir = "{safe_dir}")')
        filter_clause = "\n".join(filters)

        rules = _query(g, f"""
            SELECT ?rule ?label ?causeParam ?effectParam ?causeDir ?effectDir ?description ?process WHERE {{
                ?rule a {pfx}:OperationalRule ;
                    {pfx}:causeParameter ?causeParam ;
                    {pfx}:effectParameter ?effectParam ;
                    {pfx}:causeDirection ?causeDir ;
                    {pfx}:effectDirection ?effectDir .
                OPTIONAL {{ ?rule rdfs:label ?label }}
                OPTIONAL {{ ?rule {pfx}:ruleDescription ?description }}
                OPTIONAL {{ ?process {pfx}:hasOperationalRule ?rule }}
                {filter_clause}
            }}
            ORDER BY ?causeParam
        """)

        rule_list = [{
            "rule": _local(r["rule"]),
            "label": r.get("label"),
            "cause": f"{r['causeParam']} ({r['causeDir']})",
            "effect": f"{r['effectParam']} ({r['effectDir']})",
            "process": _local(r.get("process")),
            "description": r.get("description"),
        } for r in rules]

        # 인과 체인 구축 (A→B→C 연쇄 영향)
        cause_map: dict[str, list] = {}
        for r in rules:
            key = r["causeParam"]
            cause_map.setdefault(key, []).append({
                "effect": r["effectParam"],
                "cause_dir": r["causeDir"],
                "effect_dir": r["effectDir"],
            })

        # 특정 파라미터의 연쇄 영향 추적 (순환 감지 포함)
        chains = []
        circular_chains = []
        if parameter_name and parameter_name in cause_map:
            visited = set()
            stack = [(parameter_name, direction or "change", [])]
            while stack:
                param, dir_, path = stack.pop()
                # 현재 경로에 이미 등장한 파라미터면 순환
                path_params = {seg.split("(")[0] for seg in path}
                if param in path_params:
                    cycle_desc = " → ".join(path + [f"{param}({dir_}) [CYCLE]"])
                    circular_chains.append(cycle_desc)
                    logger.warning("인과 관계 순환 감지: %s", cycle_desc)
                    continue
                if param in visited:
                    continue
                visited.add(param)
                for effect in cause_map.get(param, []):
                    new_path = path + [f"{param}({dir_}) → {effect['effect']}({effect['effect_dir']})"]
                    chains.append(" → ".join(new_path))
                    stack.append((effect["effect"], effect["effect_dir"], new_path))

        return json.dumps({
            "success": True,
            "rules_total": len(rule_list),
            "rules": rule_list,
            "causal_chains": chains if chains else None,
            "circular_chains": circular_chains if circular_chains else None,
            "hint": "parameter_name을 지정하면 해당 파라미터의 연쇄 영향을 추적합니다." if not parameter_name else None,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
