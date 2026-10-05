"""Statistical check 그룹.

통계/분포 기반 이상치 및 값 범위 검증:
- value_ranges: DP 값이 rules/contracts/value_ranges.json 범위 내
- numeric_outliers: DP 수치값 3σ/IQR 초과 (Paulheim 2017)
- string_patterns: 무효 문자열 / 단일값 집중
- relationship_outliers: OP 인스턴스당 사용 빈도 이상치

공유 자원: 없음 (각 check 독립).
설정 주입: rules_dir (value_ranges.json 경로).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from collections import defaultdict

from rdflib import XSD, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from domain.rules_paths import rules_path
from tools.validation_support.common import (
    local,
    validate_prop_name,
)
from tools.validation_support.common import (
    query as _query,
)
from tools.validation_support.thresholds import get_tier_thresholds

logger = logging.getLogger(__name__)


_NUMERIC_XSD = {
    str(XSD.decimal), str(XSD.integer), str(XSD.float), str(XSD.double),
    str(XSD.int), str(XSD.long), str(XSD.short),
}


def _domain_dp_locals(g: Graph) -> set[str]:
    """그래프가 실제로 쓰는 도메인 프로퍼티 local name 집합."""
    return {
        local(str(p)) for p in set(g.predicates())
        if isinstance(p, URIRef) and str(p).startswith(str(DOMAIN_NS))
    }


def _near_miss_candidates(
    missing: list[str], dps: set[str], limit: int = 3,
) -> dict[str, list[str]]:
    """낡은 ``applies_to`` 이름에 대한 근접 후보 — SME 가 갱신할 수 있게.

    ``rules/`` 는 git 추적이고 ``data/generated`` 는 gitignore 라 S2 재생성마다 DP
    이름이 갈린다 (실측 2026-09-05: ``energyEfficiencyPercent`` →
    ``energyEfficiencyEfficiencyPercent``). 후보를 함께 내지 않으면 "미매칭" 만 보고
    사람이 다시 T-Box 를 뒤져야 한다. 진단용이므로 판정에는 쓰지 않는다.
    """
    import difflib

    ordered = sorted(dps)
    return {
        name: difflib.get_close_matches(name, ordered, n=limit, cutoff=0.6)
        for name in missing
    }


def _resolve_range_targets(
    g: Graph, key: str, declared: list[str] | None,
) -> tuple[list[str], str, list[str]]:
    """범위 규칙 키 → 실제 DP local name 목록과 해상 방식.

    Path B 정책 아래 모든 DP 는 class-prefixed 이므로 (``gasEnergyTemperatureC``)
    규칙 키를 **리터럴 DP 이름으로 조회하면 영구 0건** 이다. 실측 (2026-08-24,
    배포 A-Box 746,373 트리플): 선언된 7개 키 전부가 0 트리플에 매칭돼
    ``checked_properties: 0`` / ``passed: True`` 였다 — 값이 범위를 크게 벗어나도
    통과하는 상태다.

    해상 순서:
      1. ``applies_to`` **명시 선언** — SME 가 적었으면 그것이 정본이다. 단위
         모호성(``carbonContent`` 가 비율인지 퍼센트인지)을 이름으로 추론할 수
         없으므로 명시가 유일하게 안전한 방법이다.
      2. **접미 매칭** (case-insensitive) — ``…TemperatureC`` 가 ``temperatureC``
         규칙을 받는다. class prefix 만 앞에 붙는 R1A 규칙과 정확히 대응한다.
      3. 정확 일치 — Path B 이전 도메인·generic DP 하위 호환.

    substring 매칭은 **하지 않는다.** ``carbonContent`` 규칙 ``[0,1]`` 이
    ``steelmakingCarbonContentPercent`` (퍼센트 단위) 에 걸리는 식으로 단위가 다른
    DP 를 끌어와 오탐을 만든다. 그런 케이스는 ``applies_to`` 로 명시하게 한다.

    ``applies_to`` 는 **대소문자 무시**로 맞춘다. 대소문자만 다른 개명은 같은 DP 이고
    (실측 2026-09-05: ``blastFurnaceCo2Percent`` → ``blastFurnaceCO2Percent``) S2 재생성
    마다 casing 이 갈리므로, 이 축을 case-sensitive 로 두면 규칙이 조용히 죽는다. 한
    T-Box 안에 대소문자만 다른 DP 두 개가 있는 것은 그 자체로 T-Box 결함이라 모호성
    위험이 없다.

    ``applies_to`` 가 낡았을 때 **접미 매칭으로 폴백하지 않는다.** ``applies_to`` 는
    단위 모호성(``carbonContent`` 가 비율인지 퍼센트인지)을 막기 위해 존재하므로,
    폴백은 그 목적을 무너뜨린다. 대신 낡은 이름을 세 번째 반환값으로 올려 호출자가
    근접 후보와 함께 보고하게 한다.

    Returns:
        ``(dp_local_names, how, stale_declared)`` — ``how`` 는 ``"declared"`` /
        ``"suffix"`` / ``"exact"`` / ``"none"``. ``stale_declared`` 는 ``applies_to``
        에 적혀 있으나 그래프에 없는 이름들이다. ``dp_local_names`` 에는 **실재하는
        것만** 담는다 (예전에는 낡은 이름을 그대로 돌려줘 ``resolved_via_suffix`` 에
        해상된 것처럼 기록됐다).
    """
    dps = _domain_dp_locals(g)
    if declared:
        by_lower = {d.lower(): d for d in sorted(dps)}
        present: list[str] = []
        stale: list[str] = []
        for name in declared:
            hit = by_lower.get(name.lower())
            if hit is None:
                stale.append(name)
            else:
                present.append(hit)
        return (present, "declared", stale)

    key_lower = key.lower()
    exact = sorted(d for d in dps if d.lower() == key_lower)
    if exact:
        return (exact, "exact", [])
    suffix = sorted(d for d in dps if d.lower().endswith(key_lower))
    if suffix:
        return (suffix, "suffix", [])
    return ([], "none", [])


def check_value_ranges(
    g: Graph,
    *,
    rules_dir: str | None = None,
) -> dict[str, object]:
    """9. DatatypeProperty 값 범위 검증 — 도메인 규칙 위반 탐지.

    규칙 키는 **class prefix 를 뺀 이름** 으로 적는다 (``temperatureC``). Path B
    정책상 실제 DP 는 ``{classCamelLower}{Suffix}`` 형태이므로 접미로 해상한다.
    단위가 갈리는 키는 규칙에 ``"applies_to": ["dpName", …]`` 를 적어 명시하라.

    **0건 검사는 PASS 가 아니다.** 어떤 규칙도 DP 에 닿지 못하면 ``passed=False``
    로 보고한다 — "위반이 없다" 와 "검사를 못 했다" 가 같은 결과로 보이면 게이트가
    꺼진 것을 아무도 모른다 (이 리포에서 반복된 실패 모드).
    """
    ranges_path = (
        rules_path("value_ranges.json") if rules_dir is None
        else rules_path("value_ranges.json", base=rules_dir)
    )
    if not os.path.exists(ranges_path):
        return {
            "name": "값 범위 검증",
            "passed": True,
            "message": "value_ranges.json 규칙 파일 없음 (건너뜀)",
            "checked": 0,
            "violations": [],
        }

    with open(ranges_path, encoding="utf-8") as f:
        rules = json.load(f).get("ranges", {})

    violations: list[dict] = []
    checked = 0
    unmatched: list[str] = []
    resolution: dict[str, list[str]] = {}
    stale_declared: dict[str, dict] = {}
    measured_rules: set[str] = set()
    dp_locals = _domain_dp_locals(g)

    for key, rule in rules.items():
        validate_prop_name(key)
        min_val = rule.get("min")
        max_val = rule.get("max")
        desc = rule.get("description", key)
        declared = rule.get("applies_to")
        if isinstance(declared, str):
            declared = [declared]

        targets, how, stale = _resolve_range_targets(g, key, declared)
        if stale:
            # 낡은 applies_to 는 "미매칭" 과 구분해 보고한다 — 조치가 다르다
            # (규칙 파일의 이름 갱신 vs 규칙 자체가 이 도메인에 없음).
            stale_declared[key] = {
                "missing": stale,
                "candidates": _near_miss_candidates(stale, dp_locals),
            }
        if not targets:
            unmatched.append(key)
            continue
        if how != "exact":
            resolution[key] = targets

        matched_any = False
        for prop_name in targets:
            validate_prop_name(prop_name)
            count_rows = _query(
                g, f'SELECT (COUNT(*) AS ?c) WHERE {{ ?s {NS_PREFIX}:{prop_name} ?v }}',
            )
            total = int(count_rows[0]["c"]) if count_rows else 0
            if total == 0:
                continue
            matched_any = True
            measured_rules.add(key)
            checked += 1

            filter_parts = []
            if min_val is not None:
                filter_parts.append(f"xsd:decimal(?val) < {min_val}")
            if max_val is not None:
                filter_parts.append(f"xsd:decimal(?val) > {max_val}")
            if not filter_parts:
                continue

            filter_expr = " || ".join(filter_parts)
            rows = _query(g, f'''
                SELECT ?s ?val WHERE {{
                    ?s {NS_PREFIX}:{prop_name} ?val .
                    FILTER(DATATYPE(?val) IN (
                        xsd:decimal, xsd:float, xsd:double, xsd:integer,
                        xsd:int, xsd:long, xsd:short
                    ))
                    FILTER({filter_expr})
                }} LIMIT 5
            ''')

            if rows:
                violations.append({
                    "property": prop_name,
                    "rule_key": key,
                    "description": desc,
                    "expected_range": f"[{min_val}, {max_val}]",
                    "violation_count": len(rows),
                    "sample_values": [r.get("val") for r in rows[:5]],
                })
        if not matched_any:
            unmatched.append(key)

    # 규칙이 하나도 DP 에 닿지 못하면 게이트가 사실상 꺼진 것이다.
    vacuous = bool(rules) and checked == 0
    result: dict[str, object] = {
        "name": "값 범위 검증",
        "passed": not violations and not vacuous,
        "checked_properties": checked,
        "total_rules": len(rules),
        # 어떤 규칙이 실제로 값을 재고 있는지 최상위에 노출한다. checked_properties 는
        # DP 개수라 "규칙 7개 중 5개만 살아 있다" 를 감춘다 — 부분적으로 꺼진 게이트가
        # 통과로 보이던 자리다 (실측: 7규칙 중 2개가 낡은 이름으로 0건이었다).
        "rules_measured": len(measured_rules),
        "violations": violations,
    }
    if resolution:
        result["resolved_via_suffix"] = resolution
    if unmatched:
        result["unmatched_rules"] = sorted(unmatched)
    if stale_declared:
        result["applies_to_stale"] = stale_declared
    if vacuous:
        result["message"] = (
            f"규칙 {len(rules)}개 중 어느 것도 A-Box DP 에 매칭되지 않았다 "
            f"(미매칭: {sorted(unmatched)[:5]}) — 값 범위 검증이 사실상 꺼져 있다. "
            "Path B 정책상 DP 는 class-prefixed 이므로 규칙 키를 class prefix 를 뺀 "
            "이름으로 적거나, 각 규칙에 \"applies_to\": [\"실제DP이름\"] 을 명시하라."
        )
        logger.warning("check_value_ranges: %s", result["message"])
    return result


def check_numeric_outliers(g: Graph) -> dict[str, object]:
    """13. 수치 이상치 탐지 — DP 수치값의 3σ/IQR 초과 이상치 (Paulheim 2017).

    SPARQL 엔진이 prop 별 count/min/max/avg/stdev 를 GROUP BY 로 집계하고
    후보 prop 에 대해서만 Python 이 IQR/3σ 경계를 계산해 outlier 리스트를
    분리 쿼리로 조회한다. 리터럴 전수 Python 전송 제거.
    """
    numeric_filter = " || ".join(
        f"DATATYPE(?o) = <{dt}>" for dt in _NUMERIC_XSD
    )

    # Pass 1: prop 별 요약 통계 (Oxigraph SPARQL 집계)
    agg_q = f"""
        SELECT ?p (COUNT(?o) AS ?n) (AVG(xsd:double(?o)) AS ?avg)
               (MIN(xsd:double(?o)) AS ?minv) (MAX(xsd:double(?o)) AS ?maxv)
               (SUM(xsd:double(?o) * xsd:double(?o)) AS ?sumsq)
        WHERE {{
            ?s ?p ?o .
            FILTER(isLiteral(?o))
            FILTER(STRSTARTS(STR(?p), "{DOMAIN_NS}"))
            FILTER({numeric_filter})
        }} GROUP BY ?p
    """
    summary: list[tuple[str, int, float, float, float, float]] = []
    for row in g.query(agg_q):
        p = row[0]
        try:
            n = int(row[1]) if row[1] is not None else 0
        except (TypeError, ValueError):
            n = 0
        if n < 3:
            continue
        avg = float(row[2]) if row[2] is not None else 0.0
        minv = float(row[3]) if row[3] is not None else 0.0
        maxv = float(row[4]) if row[4] is not None else 0.0
        sumsq = float(row[5]) if row[5] is not None else 0.0
        # 분산 = E[X²] - (E[X])² → sample stdev 근사 (n>1)
        var = max(sumsq / n - avg * avg, 0.0)
        if n > 1:
            var *= n / (n - 1)
        stdev = var ** 0.5
        summary.append((local(str(p)), n, avg, minv, maxv, stdev))

    if not summary:
        return {
            "name": "수치 이상치 탐지",
            "passed": True,
            "message": "수치형 DP 값 없음",
            "checked_properties": 0,
            "outlier_count": 0,
            "outliers": [],
        }

    outliers = []
    total_outlier_count = 0
    total_values = 0
    checked_props = 0

    for prop_name, n, mean, _minv, _maxv, stdev in summary:
        total_values += n
        checked_props += 1

        # IQR 은 정확히 계산하려면 전 값이 필요하므로 prop 별로 한 번 더 쿼리한다.
        # 큰 값은 대부분 한 prop 뿐이므로 전체 리터럴 전송보다 훨씬 저렴.
        iqr_q = f"""
            SELECT ?val WHERE {{
                ?s <{DOMAIN_NS}{prop_name}> ?val .
                FILTER(isLiteral(?val))
                FILTER({numeric_filter.replace('?o', '?val')})
            }} ORDER BY ASC(xsd:double(?val))
        """
        sorted_vals = []
        for r in g.query(iqr_q):
            try:
                sorted_vals.append(float(r[0]))
            except (TypeError, ValueError):
                continue
        nn = len(sorted_vals)
        if nn < 3:
            continue
        _q1_idx = (nn - 1) * 0.25
        _q3_idx = (nn - 1) * 0.75
        q1 = sorted_vals[int(_q1_idx)] + (_q1_idx % 1) * (
            sorted_vals[min(int(_q1_idx) + 1, nn - 1)] - sorted_vals[int(_q1_idx)]
        )
        q3 = sorted_vals[int(_q3_idx)] + (_q3_idx % 1) * (
            sorted_vals[min(int(_q3_idx) + 1, nn - 1)] - sorted_vals[int(_q3_idx)]
        )
        iqr = q3 - q1
        if iqr > 0:
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
        elif stdev > 0:
            lower = mean - 3 * stdev
            upper = mean + 3 * stdev
        else:
            continue

        prop_outliers = [v for v in sorted_vals if v < lower or v > upper]
        if prop_outliers:
            total_outlier_count += len(prop_outliers)
            outliers.append({
                "property": prop_name,
                "count": nn,
                "mean": round(mean, 4),
                "stdev": round(stdev, 4),
                "outlier_count": len(prop_outliers),
                "outlier_samples": sorted(prop_outliers)[:5],
            })

    outlier_rate = total_outlier_count / max(total_values, 1)
    # 이상치는 항상 존재 가능 (Tukey 1.5*IQR 은 정상 정규분포에서도 0.7% 탐지).
    # 5% 를 넘으면 데이터 품질 경고. 환경변수 NUMERIC_OUTLIER_THRESHOLD 로 조정.
    import os as _os
    rate_threshold = float(_os.getenv("NUMERIC_OUTLIER_THRESHOLD", "0.05"))

    return {
        "name": "수치 이상치 탐지",
        "passed": outlier_rate <= rate_threshold,
        "outlier_rate_pct": round(outlier_rate * 100, 3),
        "threshold_pct": rate_threshold * 100,
        "checked_properties": checked_props,
        "outlier_count": total_outlier_count,
        "outliers": outliers[:20],
    }


def _dps_where_dash_is_data(tbox: Graph | None) -> set[str]:
    """원천 컬럼이 ``"-"`` 를 정상 데이터로 담는 DP 의 local name 집합을 반환한다.

    A-Box 생성기는 값 어휘가 작은 닫힌 집합인 컬럼에서 ``"-"`` 를 NULL 필터링
    대상에서 뺀다. 부호 컬럼은 음수를 ``+`` 와 짝을 이루는 ``-`` 로 인코딩하기
    때문이다. 이 check 가 같은 판정을 쓰지 않으면 정상 적재된 그 값들을 모두
    잘못된 문자열로 보고한다. 무엇이 데이터인지에 대해 적재기와 검증기의 판정이
    같아야 한다.

    DP 이름은 Path B 에서 class prefix 가 붙어 컬럼 코드로 되돌릴 수 없으므로
    ``dcterms:source`` 를 거쳐 원천 컬럼을 찾는다.
    """
    if tbox is None:
        return set()
    try:
        from tools.abox_generation import _detect_sentinel_value_columns
        from tools.validation_support.checks.temporal_cardinality import (
            _dp_source_columns,
        )
    except Exception as e:                                   # pragma: no cover
        logger.debug("dash-as-data resolution unavailable: %s", e)
        return set()

    import csv as _csv
    import glob as _glob
    import sys as _sys

    from config import SOURCE_RAWDATA_DIR

    dp_to_column = _dp_source_columns(tbox)
    if not dp_to_column:
        return set()

    value_columns: set[str] = set()
    try:
        if not os.path.isdir(SOURCE_RAWDATA_DIR):
            return set()
        _csv.field_size_limit(_sys.maxsize)
        for csv_path in _glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")):
            try:
                with open(csv_path, encoding="utf-8-sig", errors="replace") as f:
                    rows = list(_csv.DictReader(f))
            except Exception as e:
                logger.debug("CSV read failed for %s: %s", csv_path, e)
                continue
            value_columns |= _detect_sentinel_value_columns(rows)
    except Exception as e:
        logger.debug("_dps_where_dash_is_data failed: %s", e)
        return set()

    upper = {c.upper() for c in value_columns}
    return {dp for dp, col in dp_to_column.items() if col in upper}


def check_string_patterns(
    g: Graph, *, class_tiers: dict[str, str] | None = None,
    tbox: Graph | None = None,
) -> dict[str, object]:
    """16. 문자열 패턴 검증 — 무효 문자열/단일값 집중 탐지.

    SPARQL 엔진으로 prop 별 total/invalid_count 를 GROUP BY 집계하고,
    단일값 집중은 prop 별로 TOP-1 쿼리로 확인한다. 리터럴 전수 Python 수집
    제거.

    class_tiers 제공 시 활성 티어 중 가장 관대한(최대) concentration_alert 임계값 사용.
    tbox 제공 시 ``"-"`` 가 실제 값인 컬럼 유래 DP 는 무효 판정에서 면제
    (:func:`_dps_where_dash_is_data`).
    """
    xsd_string_str = str(XSD.string)

    # RDF 1.1 에서 plain literal 은 xsd:string 으로 정규화되므로 datatype
    # 단일 비교로 충분하다. langString (langtag) 는 제외된다 — 기존 동작과 동일.
    string_filter = f"isLiteral(?o) && DATATYPE(?o) = <{xsd_string_str}>"
    # 무효 패턴 비교용 VALUES 리스트 (strip().lower() 에 대응해 LCASE + 원본 모두 허용)
    INVALID_PATTERNS = {"n/a", "na", "null", "undefined", "none", "nan", "-", ""}
    # SPARQL IN 은 콤마 구분. 빈 문자열 포함.
    invalid_values_sparql = ", ".join(f'"{v}"' for v in INVALID_PATTERNS)

    # DPs whose source column encodes "-" as a real value (sign columns). Counted
    # separately below so the exemption is auditable instead of silent.
    dash_is_data = _dps_where_dash_is_data(tbox)

    # Pass 1: prop 별 total + invalid count 동시 집계. dash count 는 따로 뽑아
    # 면제 대상에서 차감할 수 있게 한다.
    q = f"""
        SELECT ?p
               (COUNT(?o) AS ?total)
               (SUM(IF(
                    LCASE(STR(?o)) IN ({invalid_values_sparql}),
                    1, 0
               )) AS ?invalid)
               (SUM(IF(STR(?o) = "-", 1, 0)) AS ?dash)
        WHERE {{
            ?s ?p ?o .
            FILTER(STRSTARTS(STR(?p), "{DOMAIN_NS}"))
            FILTER({string_filter})
        }} GROUP BY ?p
    """
    summary: list[tuple[str, int, int]] = []
    dash_exempted = 0
    for row in g.query(q):
        try:
            total = int(row[1]) if row[1] is not None else 0
            invalid = int(row[2]) if row[2] is not None else 0
            dash = int(row[3]) if row[3] is not None else 0
        except (TypeError, ValueError):
            continue
        if total == 0:
            continue
        prop_name = local(str(row[0]))
        if prop_name in dash_is_data and dash:
            invalid -= dash
            dash_exempted += dash
        summary.append((prop_name, total, invalid))

    if not summary:
        return {
            "name": "문자열 패턴 검증",
            "passed": True,
            "message": "문자열 DP 값 없음",
            "checked_properties": 0,
            "warnings": [],
        }

    # 티어별 concentration 임계값
    concentration_threshold = 0.9
    if class_tiers is not None:
        tier_thresholds = get_tier_thresholds()
        active_tiers = set(class_tiers.values())
        tier_t = [
            tier_thresholds[t]["concentration_alert"]
            for t in active_tiers if t in tier_thresholds
        ]
        if tier_t:
            concentration_threshold = max(tier_t)

    warnings: list[dict] = []
    checked_props = len(summary)

    for prop_name, total, invalid in summary:
        if invalid > 0:
            warnings.append({
                "property": prop_name,
                "issue": "무효 문자열 값",
                "invalid_count": invalid,
                "total": total,
                "rate_pct": round(invalid / total * 100, 1),
            })
        # 단일값 집중 — prop 별로 TOP-1 쿼리 (소규모)
        if total >= 5:
            dom_q = f"""
                SELECT ?o (COUNT(?s) AS ?c) WHERE {{
                    ?s <{DOMAIN_NS}{prop_name}> ?o .
                    FILTER({string_filter})
                }} GROUP BY ?o ORDER BY DESC(?c) LIMIT 1
            """
            rows = list(g.query(dom_q))
            if rows:
                dom_val = str(rows[0][0])
                try:
                    dom_cnt = int(rows[0][1])
                except (TypeError, ValueError):
                    dom_cnt = 0
                concentration = dom_cnt / total if total else 0
                if concentration > concentration_threshold:
                    warnings.append({
                        "property": prop_name,
                        "issue": f"단일값 집중 (>{concentration_threshold * 100:.0f}%)",
                        "dominant_value": dom_val[:50],
                        "concentration_pct": round(concentration * 100, 1),
                        "total": total,
                    })

    invalid_warnings = [w for w in warnings if w.get("issue") == "무효 문자열 값"]
    return {
        "name": "문자열 패턴 검증",
        "passed": len(invalid_warnings) == 0,
        "checked_properties": checked_props,
        "warning_count": len(warnings),
        "invalid_warning_count": len(invalid_warnings),
        "warnings": warnings[:20],
        # Audit trail for the sign-column exemption — a non-zero count here means
        # "-" values were kept as data, not that they were overlooked.
        "dash_as_data_properties": sorted(dash_is_data),
        "dash_values_exempted": dash_exempted,
    }


def check_relationship_outliers(g: Graph) -> dict[str, object]:
    """18. Relationship Pattern 이상치 — OP별 인스턴스당 사용 횟수 이상치 (Paulheim 2017).

    Pass1 SPARQL GROUP BY 로 prop 별 (subject, count) 를 Rust 엔진이 만들고,
    Python 은 prop 별 IQR/3σ 임계 계산 + outlier 필터링만 수행. 전체 OP
    트리플 Python 전송 제거.
    """
    # (prop, subject) 별 count — Oxigraph GROUP BY
    q = f"""
        SELECT ?p ?s (COUNT(?o) AS ?c) WHERE {{
            ?s ?p ?o .
            FILTER(isIRI(?o))
            FILTER(STRSTARTS(STR(?p), "{DOMAIN_NS}"))
        }} GROUP BY ?p ?s
    """
    prop_counts: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in g.query(q):
        try:
            cnt = int(row[2]) if row[2] is not None else 0
        except (TypeError, ValueError):
            continue
        prop_counts[local(str(row[0]))].append((str(row[1]), cnt))

    if not prop_counts:
        return {
            "name": "Relationship Pattern 이상치",
            "passed": True,
            "message": "ObjectProperty 트리플 없음",
            "checked_properties": 0,
            "outliers": [],
        }

    outliers = []
    checked_props = 0

    for prop_name, entries in prop_counts.items():
        if len(entries) < 3:
            continue
        checked_props += 1

        counts = [cnt for _, cnt in entries]
        mean = statistics.mean(counts)
        stdev = statistics.stdev(counts) if len(counts) > 1 else 0

        sorted_counts = sorted(counts)
        n = len(sorted_counts)
        _q1_idx = (n - 1) * 0.25
        _q3_idx = (n - 1) * 0.75
        q1 = sorted_counts[int(_q1_idx)] + (_q1_idx % 1) * (
            sorted_counts[min(int(_q1_idx) + 1, n - 1)] - sorted_counts[int(_q1_idx)]
        )
        q3 = sorted_counts[int(_q3_idx)] + (_q3_idx % 1) * (
            sorted_counts[min(int(_q3_idx) + 1, n - 1)] - sorted_counts[int(_q3_idx)]
        )
        iqr = q3 - q1
        if iqr == 0 and stdev == 0:
            continue

        # ``iqr == 0`` (간선 수가 거의 일정) 일 때 예전엔 ``mean + 3*stdev`` 로
        # 폴백했다. mean/stdev 는 **오염에 견고하지 않은** 추정량이라, 이상치가
        # 자기를 잡을 임계치를 스스로 끌어올린다 — 오염이 심할수록 확실히 통과한다
        # (2026-08-08 실측: hub 9/100 이면 9건 검출, 10/100 이면 **0건**, 임계치가
        # 이상치 값을 추월).
        #
        # median + MAD 는 50% 까지 견고하다. MAD 도 0 이면 (값이 정말 상수)
        # 중앙값을 넘는 값 자체가 이상이므로 중앙값을 임계치로 쓴다.
        if iqr > 0:
            threshold = q3 + 1.5 * iqr
        else:
            median = sorted_counts[n // 2]
            mad = statistics.median(
                [abs(c - median) for c in sorted_counts],
            ) if n > 1 else 0
            # 1.4826: MAD → 정규분포 표준편차 환산 상수. 3-sigma 와 같은 감도.
            threshold = (median + 3 * 1.4826 * mad) if mad > 0 else median
        for subject, cnt in entries:
            if cnt > threshold:
                outliers.append({
                    "property": prop_name,
                    "subject": local(subject),
                    "count": cnt,
                    "mean": round(mean, 2),
                    "stdev": round(stdev, 2),
                    "threshold": round(threshold, 2),
                    "sigma": round((cnt - mean) / stdev, 2) if stdev > 0 else 0,
                })

    outliers.sort(key=lambda x: -x["sigma"])
    # Relationship outlier 도 분포상 tail 은 자연 발생. 인스턴스 수 대비 5%
    # 이내면 통과. 환경변수 REL_OUTLIER_THRESHOLD 로 조정.
    import os as _os
    rate_threshold = float(_os.getenv("REL_OUTLIER_THRESHOLD", "0.05"))
    total_subjects = sum(len(v) for v in prop_counts.values()) or 1
    outlier_rate = len(outliers) / total_subjects

    return {
        "name": "Relationship Pattern 이상치",
        "passed": outlier_rate <= rate_threshold,
        "checked_properties": checked_props,
        "outlier_count": len(outliers),
        "outlier_rate_pct": round(outlier_rate * 100, 3),
        "threshold_pct": rate_threshold * 100,
        "outliers": outliers[:20],
    }
