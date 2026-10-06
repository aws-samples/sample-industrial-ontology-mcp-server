"""시맨틱 딕셔너리 생성/조회 도구

T-Box(스키마)와 A-Box(인스턴스)를 분석하여
클래스·프로퍼티·통계·SPARQL 가이드를 포함하는
시맨틱 딕셔너리 JSON을 생성한다.

모든 도메인 지식(클래스 그룹, SPARQL 예시, 질문 템플릿, 공정 흐름 등)은
T-Box 분석 결과에서 동적으로 생성된다.

이 파일은 Namespace 상수를 import 앞에 정의하는 구조라 E402 를 피할 수 없다.
아래 억제는 그 이유이며 선재 부채(SIM105)도 함께 남아 있다 — 새 코드에 적용하지 말 것.
"""
# ruff: noqa: E402, SIM105

import json
import logging
import os
from collections import defaultdict

import rdflib
from rdflib import OWL, RDF, RDFS, XSD, BNode, Literal, Namespace, URIRef

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")

from config import ABOX_PATH, INFERRED_PATH, LPG_SEMANTIC_DICT_PATH, SEMANTIC_DICT_PATH, TBOX_PATH
from domain.namespaces import (
    DOMAIN_CONFIG,
    DOMAIN_INST_NS,
    DOMAIN_NS,
    NS_PREFIX,
    sanitize_sparql_value,
)
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name

# read_semantic_dictionary / read_lpg_semantic_dictionary 의 except 절이 쓴다.
# import 없이 호출되어 예외 경로에서 NameError 가 났다 (ruff F821 로 발견).
from tools.common import error_response

logger = logging.getLogger(__name__)

# ── 헬퍼 ──────────────────────────────────────


def _get_labels(g, uri):
    """그래프에서 URI의 en/ko rdfs:label 반환"""
    en, ko = "", ""
    for _, _, o in g.triples((uri, RDFS.label, None)):
        if isinstance(o, Literal):
            if o.language == "en":
                en = str(o)
            elif o.language == "ko":
                ko = str(o)
    return en, ko


def _get_comments(g, uri):
    """그래프에서 URI의 en/ko rdfs:comment 반환"""
    en, ko = "", ""
    for _, _, o in g.triples((uri, RDFS.comment, None)):
        if isinstance(o, Literal):
            if o.language == "en":
                en = str(o)
            elif o.language == "ko":
                ko = str(o)
    return en, ko


import re as _re

from domain.rules_paths import rules_path

_CODE_MEANINGS_PATH = rules_path("code_meanings.json")
_CANONICAL_HINTS_PATH = rules_path("canonical_hints.json")
# 컬럼별 권위 메타데이터 (선택, 배포가 제공). 데이터 표준 사전에서 결정적으로
# 추출한 정의문·관리방법·단위·코드 자릿수 구조를 담는다.
_STD_ITEM_METADATA_PATH = rules_path("std_item_metadata.json")
# 도메인 전문가(SME)가 손으로 관리하는 업무 규칙 (선택). 기계 추출로 얻을 수 없는
# 코드값·판정식을 담는다.
_SME_BUSINESS_RULES_PATH = rules_path("sme_business_rules.json")
# 사용자 구어체 용어 → 컬럼/판정식 (선택, 배포가 제공하는 용어 사전)
_USER_TERM_DICT_PATH = rules_path("user_term_dictionary.json")
_KOREAN_SYNONYMS_PATH = rules_path("korean_synonyms.json")
# DP label/comment 에 괄호로 박힌 원본 소스 컬럼 코드를 추출한다.
# 예: "표면 결함 코드(CODE_COL_1)" → "CODE_COL_1".
_COLUMN_CODE_RE = _re.compile(r"\(([A-Z][A-Z0-9_]{2,})\)")
# A-Box 에 값이 하나도 없는 DP 를 역색인에서 표시하는 마커. 질의에 쓰면 에러 없이
# 0건이 나오므로 "매핑은 맞지만 못 쓴다" 를 구분해야 한다.
_EMPTY_DP_MARKER = "∅비어있음"

_code_meanings_cache: dict | None = None
_canonical_hints_cache: dict | None = None
_std_item_cache: dict | None = None
_sme_rules_cache: dict | None = None


def _read_json_section(path: str, section: str | None = None) -> dict:
    """Read a rules JSON (optionally one top-level section). {} when unreadable.

    Domain-neutral by construction: a project without the file gets an empty
    map and every downstream injection becomes a no-op. ``TypeError`` is caught
    alongside the I/O and decode errors because a caller that patches
    ``builtins.open`` (as the generator's own unit tests do) hands back an
    object ``json.load`` cannot consume — a rules file being unreadable must
    never fail dictionary generation, whatever the reason.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError) as e:
        logger.warning("%s 로드 실패: %s", os.path.basename(path), e)
        return {}
    if section is None:
        return data if isinstance(data, dict) else {}
    value = data.get(section, {})
    return value if isinstance(value, dict) else {}


def _strip_meta(mapping: dict) -> dict:
    """Drop ``_``-prefixed annotation keys, keeping only payload entries."""
    return {k: v for k, v in mapping.items() if not str(k).startswith("_")}


def _cache_if_loaded(value: dict, cache_name: str) -> dict:
    """Memoize only a **non-empty** load; leave the cache unset otherwise.

    Caching an empty result would make a transient read failure permanent for
    the process. That is not hypothetical: the generator's own unit tests patch
    ``builtins.open``, so a rules read during one test returns ``{}`` and every
    later call in the same process — including a real dictionary generation —
    would reuse it and silently emit a section missing its rules. Re-reading a
    small JSON when it is genuinely absent costs one ``os.path.exists``.
    """
    if value:
        globals()[cache_name] = value
    return value


def _load_sme_business_rules() -> dict:
    """rules/domain/sme_business_rules.json (SME 가 손으로 관리하는 업무 규칙) 을 읽는다."""
    if _sme_rules_cache is not None:
        return _sme_rules_cache
    return _cache_if_loaded(
        _read_json_section(_SME_BUSINESS_RULES_PATH), "_sme_rules_cache",
    )


def _load_code_meanings() -> dict:
    """코드값 의미와 SME 업무 그룹을 여러 출처에서 병합해 읽는다.

    반환 형태는 ``{"columns": {COL: {val: meaning}}, "business_groups": {COL: {...}}}``.

    출처는 셋이며 아래로 갈수록 우선한다 (컬럼 단위로 뒤의 출처가 이긴다):

    1. ``code_meanings.json``: 코드 설명 시트의 결정적 추출물.
    2. ``canonical_hints.json`` 의 ``business_groups``: SME 가 정한 상위 묶음.
    3. ``sme_business_rules.json``: 추출 대상 시트가 **구조적으로** 제공할 수 없는
       코드값. 다른 항목을 조합하거나 참조만 하는 컬럼처럼 코드 설명 시트에 값 행이
       없는 컬럼은 추출물에 코드 의미가 0건이다. 코드 체계가 추출 대상 밖의 업무
       자료에만 있는 컬럼도 있다. 이런 값은 재실행이 덮어쓰는 생성 추출물에
       덧대지 않고 이 파일에서 손으로 관리하며, 같은 컬럼이면 이 출처가 이긴다.

    도메인 중립: 모든 출처는 선택이다.
    """
    global _code_meanings_cache
    if _code_meanings_cache is not None:
        return _code_meanings_cache

    columns = dict(_read_json_section(_CODE_MEANINGS_PATH, "columns"))
    groups = dict(_read_json_section(_CANONICAL_HINTS_PATH, "business_groups"))

    sme = _load_sme_business_rules()
    for col, spec in _strip_meta(sme.get("code_meanings") or {}).items():
        codes = spec.get("codes") if isinstance(spec, dict) else None
        if codes:
            # SME 규칙 파일이 정본이다. 같은 컬럼이 양쪽에 있으면 SME 값으로
            # 덮는다 (같은 컬럼의 코드값이 두 출처에서 다르게 정의된 경우).
            columns[col.upper()] = {**columns.get(col.upper(), {}), **codes}
    for col, spec in _strip_meta(sme.get("business_groups") or {}).items():
        if isinstance(spec, dict):
            groups[col.upper()] = {**groups.get(col.upper(), {}), **spec}

    merged = {"columns": columns, "business_groups": groups}
    if columns or groups:  # 빈 결과는 캐시하지 않는다 — §_cache_if_loaded
        _code_meanings_cache = merged
    return merged


def _load_std_item_metadata() -> dict:
    """rules/domain/std_item_metadata.json 을 읽어 {"items", "code_structure"} 로 반환한다.

    권위 텍스트 (정식 정의문 / 관리방법 / 단위) 와 코드 자릿수 구조를 담는다. 둘 다
    배포가 제공하는 데이터 표준 사전에서 원문 그대로 옮긴 값이다. LLM 이 라벨을
    풀어 쓴 설명문은 정의문의 공식·조건과 단위를 잃기 쉬우므로 원문을 따로 싣는다.
    파일이 없으면 빈 구조를 반환한다 (도메인 중립).
    """
    global _std_item_cache
    if _std_item_cache is not None:
        return _std_item_cache
    data = _read_json_section(_STD_ITEM_METADATA_PATH)
    loaded = {
        "items": data.get("items") or {},
        "code_structure": data.get("code_structure") or {},
    }
    if data:  # 빈 결과는 캐시하지 않는다 — §_cache_if_loaded
        _std_item_cache = loaded
    return loaded


def _load_canonical_designations() -> dict:
    """Load the canonical_designations section of rules/domain/canonical_hints.json."""
    if _canonical_hints_cache is not None:
        return _canonical_hints_cache
    return _cache_if_loaded(
        _read_json_section(_CANONICAL_HINTS_PATH, "canonical_designations"),
        "_canonical_hints_cache",
    )


def _resolve_canonical_designations(canonical: dict, classes_dict: dict) -> dict:
    """canonical designation 이 지금 존재하는 DP 이름을 가리키도록 다시 잇는다.

    **왜**: ``canonical_hints.json`` 은 SME 가 검증한 집계 규칙을 DP 이름으로
    기록한다 (가상의 예: ``"filter_property": "processResultDWeightCol2"``). 그
    이름은 LLM 이 짓고 **S2 가 T-Box 를 재생성할 때마다 바뀌므로**, 힌트가 더는
    없는 프로퍼티를 조용히 가리키게 된다 (예: ``processResultDWeightCol2`` →
    ``processResultDCoilWeight``). 그러면 "중량 0 제외" 같은 필수 필터 규칙이
    딕셔너리에 텍스트로는 있어도 질의에 쓸 수 없다.

    그래서 **CSV 컬럼 코드** 로 해석한다. 컬럼 코드는 LLM 이 아니라 소스 스키마에서
    오므로 안정적이다. 각 힌트는 이미 컬럼을 담고
    (``"filter_column": "WEIGHT_COL_2"``) 모든 DP 는 ``source_columns`` 를 담으므로
    매핑이 결정적이다.

    designation 마다 두 필드를 추가한다:
      - ``<x>_property_resolved``: 지금 실제로 존재하는 DP 이름
      - ``_resolution_status``: 감사용 ``ok`` / ``partial`` / ``stale``

    원래 값은 보존해 리뷰어가 무엇이 어긋났는지 볼 수 있게 한다.

    Args:
        canonical: 힌트 파일의 원본 ``canonical_designations``.
        classes_dict: 딕셔너리의 ``classes`` 섹션 (DP → source_columns).

    Returns:
        해석 필드를 추가한 새 dict. 예외를 던지지 않는다.
    """
    if not canonical:
        return canonical

    # (class, UPPER(column)) → [dp names], plus a class-agnostic fallback.
    by_class_col: dict[tuple[str, str], list[str]] = {}
    for cls_name, cls_info in (classes_dict or {}).items():
        for dp_name, dp_info in (cls_info.get("datatype_properties") or {}).items():
            if not isinstance(dp_info, dict):
                continue
            for col in dp_info.get("source_columns") or []:
                by_class_col.setdefault(
                    (cls_name, str(col).strip().upper()), [],
                ).append(dp_name)

    resolved: dict = {}
    for intent, spec in canonical.items():
        if not isinstance(spec, dict):
            resolved[intent] = spec
            continue
        out = dict(spec)
        cls_name = spec.get("class")
        declared_dps = existing = 0
        # Every "<x>property" key pairs with a "<x>column" key by convention
        # ("property"/"column", "filter_property"/"filter_column", ...).
        for key, val in spec.items():
            if not key.endswith("property") or not isinstance(val, str):
                continue
            declared_dps += 1
            col_key = key[: -len("property")] + "column"
            col = spec.get(col_key)
            candidates = (
                by_class_col.get((cls_name, str(col).strip().upper()), [])
                if cls_name and col else []
            )
            if candidates:
                # Prefer the declared name if it still exists; else the first
                # column match (deterministic — source_columns is 1:1 by design).
                current = val if val in candidates else sorted(candidates)[0]
                out[f"{key}_resolved"] = current
                if current == val:
                    existing += 1
            elif cls_name and _dp_exists(classes_dict, cls_name, val):
                out[f"{key}_resolved"] = val
                existing += 1
        if declared_dps == 0:
            out["_resolution_status"] = "no_property_refs"
        elif existing == declared_dps:
            out["_resolution_status"] = "ok"
        elif any(k.endswith("_resolved") for k in out):
            out["_resolution_status"] = "resolved_by_column"
        else:
            out["_resolution_status"] = "stale"
        resolved[intent] = out

    stale = [i for i, s in resolved.items()
             if isinstance(s, dict) and s.get("_resolution_status") == "stale"]
    drifted = [i for i, s in resolved.items()
               if isinstance(s, dict)
               and s.get("_resolution_status") == "resolved_by_column"]
    if drifted:
        logger.warning(
            "canonical_hints: DP 이름이 T-Box 와 어긋나 컬럼 기준으로 재해석 %d건 "
            "— hints 파일의 *_property 값을 갱신 권장: %s",
            len(drifted), drifted,
        )
    if stale:
        logger.warning(
            "canonical_hints: 컬럼으로도 해석 불가 %d건 — 규칙이 무효화됨: %s",
            len(stale), stale,
        )
    return resolved


def _dp_exists(classes_dict: dict, cls_name: str, dp_name: str) -> bool:
    """True if ``cls_name`` declares ``dp_name`` in the dictionary."""
    cls_info = (classes_dict or {}).get(cls_name) or {}
    return dp_name in (cls_info.get("datatype_properties") or {})


def _column_codes(info: dict) -> list[str]:
    """DP 가 유래한 소스 컬럼 코드를 권위가 높은 것부터 모두 반환한다.

    ``dcterms:source`` (``source_columns`` 로 노출) 가 권위 있는 연결이다. T-Box
    생성기가 기록하며 추측하지 않는다. DP 에 그것이 없을 때만 일부 라벨이 담는
    ``"(COLUMN_CODE)"`` 토큰으로 폴백한다. 예: "표면 결함 코드(CODE_COL_1)".

    텍스트 복구만 쓰면 라벨에 괄호 코드가 없는 모든 DP 에서 코드 의미와 SME 업무
    그룹이 조용히 빠진다. 이름 추측에 기대는 것과 같은 실패 유형이므로 출처 표기를
    먼저 본다.
    """
    codes = [c.upper() for c in (info.get("source_columns") or []) if c]
    if codes:
        return codes
    for text in (info.get("label_ko", ""), info.get("description_ko", "")):
        if not text:
            continue
        m = _COLUMN_CODE_RE.search(text)
        if m:
            return [m.group(1).upper()]
    return []


def _is_numeric_value(v) -> bool:
    """Return True if value can be coerced to float."""
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


# A3: 자연스럽게 고변동이라 unit_warning 트리거를 피해야 할 DP 키워드.
# 금액 / 재고 / 카운트 / 시간 등은 본질적으로 여러 자리수를 걸쳐 분포하므로
# max/min ratio 만으로 "단위 불일치" 를 추정할 수 없다. 이 키워드가 prop_name
# 에 등장하면 휴리스틱을 생략하고 None 반환.
_UNIT_SKIP_KEYWORDS: tuple[str, ...] = (
    # monetary
    "cost", "price", "usd", "krw", "eur", "jpy", "amount", "revenue",
    "budget", "fee", "charge",
    # inventory / counting
    "stock", "count", "quantity", "inventory",
    # time / duration
    "duration", "seconds", "minutes", "hours", "days", "elapsed",
    # rates / probabilities — 대개 0~1 또는 % 스케일이라 ratio 무의미
    "rate", "probability", "score",
)


def _detect_unit_mismatch(values, prop_name: str):
    """A3: Heuristic unit-inconsistency detection from value distribution.

    Rules (deterministic, no LLM):
      - UNIT_MISMATCH_RATIO env var (default 50) controls max/min ratio threshold.
        Set to 0 to disable the check entirely.
      - Require ≥5 strictly positive numeric samples (otherwise stats are unstable).
      - Skip entirely if prop_name matches ``_UNIT_SKIP_KEYWORDS`` (cost / stock /
        count / duration 등) — 자연 고변동 DP 는 ratio 로 단위 불일치 판정 불가.
      - Keyword match on prop_name → suggest a canonical unit (mm / celsius / kg).
    Returns a dict {severity, ratio, reason, suggestion, sample_low, sample_high}
    when a warning should be emitted; otherwise None.
    """
    threshold_env = os.getenv("UNIT_MISMATCH_RATIO", "50")
    try:
        threshold = float(threshold_env)
    except ValueError:
        threshold = 50.0
    if threshold <= 0:
        return None
    prop_lower = (prop_name or "").lower()
    # 자연 고변동 DP 는 skip — false positive 방어
    if any(k in prop_lower for k in _UNIT_SKIP_KEYWORDS):
        return None
    pos_values = [float(v) for v in values if _is_numeric_value(v) and float(v) > 0]
    if len(pos_values) < 5:
        return None
    lo, hi = min(pos_values), max(pos_values)
    ratio = hi / lo
    if ratio < threshold:
        return None
    hint = None
    if any(
        k in prop_lower
        for k in ("length", "width", "height", "diameter", "thickness", "depth")
    ):
        hint = "mm"
    elif any(k in prop_lower for k in ("temperature", "temp")):
        hint = "celsius"
    elif any(k in prop_lower for k in ("weight", "mass")):
        hint = "kg"
    sorted_vals = sorted(pos_values)
    return {
        "severity": "warning",
        "ratio": round(ratio, 1),
        "reason": f"max/min ratio {ratio:.0f}x — 단위 불일치 의심",
        "suggestion": (
            f"단일 단위로 정규화 권장: {hint} 기준"
            if hint
            else "단일 단위로 정규화 권장"
        ),
        "sample_low": [round(v, 3) for v in sorted_vals[:3]],
        "sample_high": [round(v, 3) for v in sorted_vals[-3:]],
    }


def _compute_stats(values, xsd_type, prop_name: str = ""):
    """프로퍼티 값 리스트로부터 통계 정보 계산.

    D4: value_stats dict 추가 — percentile (p10/p50/p90) 명시적 라벨링으로
    Claude 가 자연어 → SPARQL 변환 시 "쿼리 값이 알려진 범위 밖" 을 자동
    판단 가능. 기존 필드 (min/max/example_values/distinct_values) 는
    backward compat 을 위해 유지.

    A3: ``prop_name`` 인자를 전달하면 숫자 DP 값 분포를 분석해 단위 불일치가
    의심될 때 ``stats['unit_warning']`` 을 덧붙인다. 기본값은 "" 이므로 기존
    호출부 호환.
    """
    stats = {"count": len(values)}
    if not values:
        return stats
    unique = sorted(set(values))
    # D4: 명시적 구조의 value_stats — 기존 필드와 중복되더라도 유지 (의미적 명료)
    value_stats: dict = {"count": len(values)}
    if xsd_type in ("decimal", "integer", "float", "double"):
        try:
            nums = sorted([float(v) for v in values if v])
            if nums:
                stats["min"] = nums[0]
                stats["max"] = nums[-1]
                n = len(nums)
                # 선형보간 백분위 (0-indexed: (n-1)*p 위치)
                def _pctl(arr, p):
                    idx = (len(arr) - 1) * p
                    lo = int(idx)
                    hi = min(lo + 1, len(arr) - 1)
                    return arr[lo] + (idx - lo) * (arr[hi] - arr[lo])
                # 소비부의 기존 round(..., 6) 관행에 맞춰 12자리 계정 ID 검사 오탐을 막는다.
                p10 = round(_pctl(nums, 0.1), 6)
                p50 = round(_pctl(nums, 0.5), 6)
                p90 = round(_pctl(nums, 0.9), 6)
                stats["example_values"] = [p10, p50, p90]
                # D4: 명시적 percentile 라벨
                value_stats.update({
                    "min": nums[0],
                    "max": nums[-1],
                    "p10": p10,
                    "p50": p50,
                    "p90": p90,
                })
                # A3: 단위 불일치 탐지 (prop_name 힌트 + 값 분포)
                warning = _detect_unit_mismatch(nums, prop_name)
                if warning is not None:
                    stats["unit_warning"] = warning
        except ValueError:
            stats["example_values"] = unique[:3]
    elif xsd_type == "dateTime":
        stats["min"] = min(unique)
        stats["max"] = max(unique)
        n = len(unique)
        stats["example_values"] = [unique[0], unique[n // 2], unique[-1]]
        value_stats.update({
            "min": stats["min"],
            "max": stats["max"],
            "p50": unique[n // 2],  # datetime 은 median 만 의미 있음
        })
    elif xsd_type == "string":
        if len(unique) <= 20:
            stats["distinct_values"] = unique
        else:
            stats["distinct_count"] = len(unique)
            stats["example_values"] = unique[:5]
        value_stats["distinct_count"] = len(unique)
    else:
        stats["example_values"] = unique[:3]
        value_stats["distinct_count"] = len(unique)
    stats["value_stats"] = value_stats
    return stats


# ── A-Box 단일 패스 통계 ─────────────────────────


def _single_pass_abox_stats(abox, steel_ns, dt_prop_info, obj_prop_names):
    """A-Box 통계 수집 — SPARQL 집계 기반.

    instance_counts / op_counts 는 Oxigraph Rust SPARQL 엔진이 GROUP BY
    로 집계하고, prop_values 는 (class, prop, value) 3-튜플 스트림을 받아
    Python 이 dict 로 그룹핑한다. 기존의 A-Box 전수 iteration 과 주어별
    rdf:type 재조회를 제거한다.

    Returns:
        instance_counts: {class_uri: int}
        prop_values: {(class_name, prop_name): [str]}
        op_counts: {prop_name: int}
    """
    instance_counts: dict = {}
    op_counts: dict = defaultdict(int)
    prop_values: dict = defaultdict(list)

    # ── 1) 인스턴스 카운트 (class URI 기준) ──────────────
    for row in abox.query(
        "SELECT ?cls (COUNT(?s) AS ?n) WHERE { ?s a ?cls . FILTER(isIRI(?cls)) } "
        "GROUP BY ?cls"
    ):
        try:
            instance_counts[URIRef(str(row[0]))] = int(row[1])
        except (TypeError, ValueError):
            pass

    # ── 2) ObjectProperty 트리플 카운트 (steel 네임스페이스) ──
    # 네임스페이스는 문자열 리터럴 안에 들어가므로 리터럴 경계 문자를 이스케이프한다.
    ns_literal = sanitize_sparql_value(str(steel_ns))
    op_q = f"""
        SELECT ?p (COUNT(?o) AS ?n) WHERE {{
            ?s ?p ?o .
            FILTER(STRSTARTS(STR(?p), "{ns_literal}"))
            FILTER(isIRI(?o))
        }} GROUP BY ?p
    """
    for row in abox.query(op_q):
        pname = _local_name(str(row[0]))
        if pname in obj_prop_names:
            try:
                op_counts[pname] = int(row[1])
            except (TypeError, ValueError):
                pass

    # ── 3) DatatypeProperty 값 (class × prop × value) ──
    # rdf:type 이 붙은 steel 네임스페이스 인스턴스에 한해 리터럴 값을 조회.
    # 이전 Python 루프의 주어별 type 캐시를 SPARQL join 으로 대체.
    dt_q = f"""
        SELECT ?cls ?p ?o WHERE {{
            ?s a ?cls ; ?p ?o .
            FILTER(STRSTARTS(STR(?cls), "{ns_literal}"))
            FILTER(STRSTARTS(STR(?p), "{ns_literal}"))
            FILTER(isLiteral(?o))
        }}
    """
    for row in abox.query(dt_q):
        cls_name = _local_name(str(row[0]))
        pname = _local_name(str(row[1]))
        if pname in dt_prop_info:
            prop_values[(cls_name, pname)].append(str(row[2]))

    return dict(instance_counts), prop_values, dict(op_counts)


# ── 추출 함수 ─────────────────────────────────


def _load_ontology_data(use_inferred: bool = False):
    """로컬 파일에서 T-Box와 A-Box(또는 추론 결과) TTL을 로드하여 rdflib Graph 튜플로 반환.

    Args:
        use_inferred: True면 all_inferred.ttl을 A-Box 대신 사용.
            추론 클래스(RunningEquipmentStatus 등)의 instance_count가 정확해짐.
    """
    with open(TBOX_PATH, encoding="utf-8") as f:
        t_box_content = f.read()

    a_box_content = ""
    abox_path = INFERRED_PATH if use_inferred else ABOX_PATH
    try:
        with open(abox_path, encoding="utf-8") as f:
            a_box_content = f.read()
    except FileNotFoundError:
        # inferred 없으면 A-Box 폴백
        if use_inferred:
            try:
                with open(ABOX_PATH, encoding="utf-8") as f:
                    a_box_content = f.read()
            except FileNotFoundError:
                pass
        # A-Box도 없으면 빈 그래프

    tbox = _new_graph()
    tbox.parse(data=t_box_content, format="turtle")

    abox = _new_graph()
    if a_box_content:
        abox.parse(data=a_box_content, format="turtle")

    return tbox, abox


def _extract_classes(tbox):
    """T-Box에서 steel 네임스페이스의 owl:Class 목록과 서브클래스 맵 추출"""
    steel_classes = sorted(
        [
            c
            for c in tbox.subjects(RDF.type, OWL.Class)
            if isinstance(c, URIRef) and str(c).startswith(DOMAIN_NS)
        ],
        key=lambda x: _local_name(x),
    )

    subclass_map = defaultdict(set)
    for s, _, o in tbox.triples((None, RDFS.subClassOf, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            subclass_map[o].add(s)

    return steel_classes, subclass_map


def _extract_properties(tbox, abox):
    """T-Box/A-Box에서 DatatypeProperty, ObjectProperty 정보 추출.

    A-Box 순회는 _single_pass_abox_stats에 위임하여 단일 패스로 처리한다.
    """
    # DatatypeProperty
    dt_prop_info = {}
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        name = _local_name(s)
        en, ko = _get_labels(tbox, s)
        desc_en, desc_ko = _get_comments(tbox, s)
        domains = [
            _local_name(d)
            for _, _, d in tbox.triples((s, RDFS.domain, None))
            if isinstance(d, URIRef)
        ]
        ranges = [
            _local_name(r)
            for _, _, r in tbox.triples((s, RDFS.range, None))
            if isinstance(r, URIRef)
        ]
        is_functional = (s, RDF.type, OWL.FunctionalProperty) in tbox
        # dcterms:source — 이 DP 가 유래한 CSV 컬럼 코드. T-Box 생성 프롬프트
        # (04-property-rules.md 의 "DatatypeProperty declaration" 절) 가 필수로
        # 요구하며, A-Box 생성기가 컬럼↔DP 를 추측 없이 연결하는 근거이자 SME 가
        # DP 의 출처를 역추적하는 통로다. 미표기 DP 는 키가 빠지므로 소비자는
        # 존재 여부를 확인해야 한다.
        source_columns = sorted(
            str(src).strip()
            for _, _, src in tbox.triples((s, _DCTERMS_NS.source, None))
            if str(src).strip()
        )
        dt_prop_info[name] = {
            "label_en": en,
            "label_ko": ko,
            "range": ranges[0] if ranges else "",
            "domains": domains,
            "description_en": desc_en,
            "description_ko": desc_ko,
            "functional": is_functional,
        }
        if source_columns:
            dt_prop_info[name]["source_columns"] = source_columns

    # ObjectProperty (T-Box only)
    obj_props = []
    for s in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        en, ko = _get_labels(tbox, s)
        desc_en, desc_ko = _get_comments(tbox, s)
        domains = [
            _local_name(d)
            for _, _, d in tbox.triples((s, RDFS.domain, None))
            if isinstance(d, URIRef)
        ]
        ranges = [
            _local_name(r)
            for _, _, r in tbox.triples((s, RDFS.range, None))
            if isinstance(r, URIRef)
        ]
        inverses = [
            _local_name(i)
            for _, _, i in tbox.triples((s, OWL.inverseOf, None))
            if isinstance(i, URIRef)
        ]
        sub_props = [
            _local_name(sp)
            for _, _, sp in tbox.triples((s, RDFS.subPropertyOf, None))
            if isinstance(sp, URIRef)
        ]
        is_transitive = (s, RDF.type, OWL.TransitiveProperty) in tbox
        obj_props.append(
            {
                "uri": s,
                "name": _local_name(s),
                "label_en": en,
                "label_ko": ko,
                "description_en": desc_en,
                "description_ko": desc_ko,
                "domains": domains,
                "ranges": ranges,
                "inverseOf": inverses[0] if inverses else None,
                "subPropertyOf": sub_props[0] if sub_props else None,
                "transitive": is_transitive,
            }
        )

    outgoing_by_class = defaultdict(list)
    incoming_by_class = defaultdict(list)
    for prop in obj_props:
        for dom in prop["domains"]:
            outgoing_by_class[dom].append(prop)
        for rng in prop["ranges"]:
            incoming_by_class[rng].append(prop)

    # A-Box 단일 패스: 프로퍼티 값, OP 카운트, 인스턴스 카운트를 한 번에 수집
    obj_prop_names = {op["name"] for op in obj_props}
    instance_counts, prop_values, op_counts = _single_pass_abox_stats(
        abox, DOMAIN_NS, dt_prop_info, obj_prop_names
    )

    return {
        "dt_prop_info": dt_prop_info,
        "obj_props": obj_props,
        "outgoing_by_class": outgoing_by_class,
        "incoming_by_class": incoming_by_class,
        "prop_values": prop_values,
        "op_counts": op_counts,
        "instance_counts": instance_counts,
    }


# ── 통계 및 딕셔너리 구성 ─────────────────────


def _get_equivalent_class_restriction(tbox, cls) -> dict | None:
    """클래스의 owl:equivalentClass restriction (BNode) 을 dict 로 반환.

    onProperty 가 없는 restriction 은 표현 불가로 간주 → None.
    hasValue / someValuesFrom 둘 다 없는 경우도 None.
    """
    for _, _, eq in tbox.triples((cls, OWL.equivalentClass, None)):
        if not isinstance(eq, BNode):
            continue
        on_prop, has_value, some_values = None, None, None
        for _, _, p in tbox.triples((eq, OWL.onProperty, None)):
            on_prop = _local_name(p)
        for _, _, v in tbox.triples((eq, OWL.hasValue, None)):
            has_value = str(v)
        for _, _, sv in tbox.triples((eq, OWL.someValuesFrom, None)):
            some_values = _local_name(sv)
        if on_prop:
            result = {"onProperty": on_prop}
            if has_value is not None:
                result["hasValue"] = has_value
            if some_values is not None:
                result["someValuesFrom"] = some_values
            return result
    return None


def _get_union_members(tbox, cls) -> list[str] | None:
    """owl:unionOf 멤버 list. 없으면 None."""
    for _, _, u in tbox.triples((cls, OWL.unionOf, None)):
        return [
            _local_name(m)
            for m in rdflib.collection.Collection(tbox, u)
            if isinstance(m, URIRef)
        ]
    return None


def _get_disjoint_with(tbox, cls) -> list[str]:
    """owl:disjointWith 클래스 list (정렬). 없으면 빈 list."""
    return sorted(
        _local_name(d)
        for _, _, d in tbox.triples((cls, OWL.disjointWith, None))
        if isinstance(d, URIRef)
    )


_DT_STATS_KEYS = (
    "min", "max",
    "example_values", "distinct_values", "distinct_count",
    "value_stats",   # D4: percentile dict
    "unit_warning",  # A3: 단위 불일치 경고
)


_NUMBERED_COLUMN_RE = _re.compile(r"^(?P<base>.*?[A-Z])_?(?P<index>\d+)$")


def _base_column_code(col_code: str, items: dict) -> str | None:
    """Return the unnumbered base of an enumerated column, if the base is known.

    A numbered column maps to its unnumbered base. Returns None when the column
    is not numbered, or when the base is not a registered standard item — never
    guesses a base that the authority does not define.
    """
    match = _NUMBERED_COLUMN_RE.match(col_code)
    if not match:
        return None
    for candidate in (match.group("base"), match.group("base").rstrip("_")):
        if candidate and candidate != col_code and candidate in items:
            return candidate
    return None


def _attach_std_item_authority(entry: dict, std: dict, col_code: str) -> None:
    """소스 컬럼 하나에 대한 데이터 표준 권위 필드를 붙인다.

    권위 출처가 실제로 값을 제공하고 entry 에 아직 그 키가 없을 때만 추가한다
    (code_meanings 규칙과 같이 첫 컬럼 코드가 이긴다):

    - ``sme_definition``: 정의문 원문 (``STD_DEFINITION_COL``). 생성된
      ``description`` 은 LLM 이 *라벨* 을 풀어 쓴 것이라 라벨을 되풀이하기 쉽다.
      정의문은 숫자의 의미를 바꾸는 공식이나 조건을 담을 수 있다 (가상의 예:
      "자재 유형 A 는 측정값, 유형 B 는 계산값").
    - ``derivation_note``: 값의 생성·사용 방법 (``STD_MGT_METHOD_COL``). 라벨과
      설명문에서는 복구할 수 없는 정보다.
    - ``unit`` / ``unit_class``: 기계가 읽는 단위. 풀어 쓴 설명문은 단위를 빠뜨릴
      수 있으므로 전용 필드로 산문 의존을 없앤다.
    - ``code_structure``: 코드 자릿수 구조. ``SUBSTR`` 기반 SME 판정의 전제다.
      같은 컬럼이 품종별로 다르게 배치되면 ``variants`` 를 담는다.

    ``std["items"]`` 는 표준 사전의 서로 다른 두 항목이 함께 주장하는 컬럼 코드를 일부러
    제외한다. 그래서 모호한 코드는 그럴듯한 오답 정의 대신 아무것도 기여하지 않는다.

    **번호 계열 폴백.** 소스 스키마는 표준 사전의 한 항목을 번호 접미사가 붙은 열거형
    컬럼으로 펼칠 수 있고 (가상의 예: ``MEAS_TEMP_COL_101..``), 표준 사전은 번호
    없는 base 만 등록할 수 있다. 이 경우 정의문은 있지만 정확 일치로는 닿지 않는다.
    base 에 정의가 있으면 그것을 붙이고 ``_definition_from_base`` 에 base 컬럼을
    기록해, 그 텍스트가 해당 인덱스 하나가 아니라 계열 전체를 설명한다는 것을 읽는
    사람이 알게 한다.

    이 폴백은 번호 컬럼이 base 계열의 일원이라는 전제에 기댄다. ``base+N`` 컬럼이
    표준 사전에 따로 등록돼 있으면 정확 일치가 먼저 이겨 base 정의를 상속하지
    않는다. 등록되지 않은 번호 컬럼이 base 와 다른 의미를 가진다면 틀린 텍스트를
    물려받으므로, 그런 컬럼은 표준 사전에 별도 항목으로 등록해야 한다.
    """
    item = std["items"].get(col_code)
    base_code = None
    if not item:
        base_code = _base_column_code(col_code, std["items"])
        if base_code:
            item = std["items"].get(base_code)
    if item:
        for source_key, entry_key in (
            ("definition", "sme_definition"),
            ("management", "derivation_note"),
            ("unit", "unit"),
            ("unit_class", "unit_class"),
        ):
            value = item.get(source_key)
            if value and entry_key not in entry:
                entry[entry_key] = value
                if base_code and "_definition_from_base" not in entry:
                    entry["_definition_from_base"] = base_code
    structure = std["code_structure"].get(col_code) or (
        std["code_structure"].get(base_code) if base_code else None
    )
    if structure and "code_structure" not in entry:
        entry["code_structure"] = structure


def _build_dt_prop_entry(
    info: dict, prop_name: str, class_name: str, prop_values: dict,
    inherited_from: str | None = None,
    instance_count: int = 0, emit_population: bool = False,
) -> dict:
    """단일 DatatypeProperty 의 entry dict 빌드 (정의 + stats + inheritance).

    ``emit_population`` 이 True 면 (v2, include_stats=True) A-Box 채움 신호를 명시:
      - ``is_populated``: 항상 부여 (값 ≥1 이면 True). value_stats 키 부재를 추론하지
        않고도 빈 DP 를 구분하게 한다.
      - ``populated_count`` / ``coverage``: populated DP 에만. coverage 는
        ``dp_count / instance_count`` (서브클래스 값 합산 방어로 1.0 클램프).
    v1(vocabulary contract) 에서는 emit_population=False 라 신호를 방출하지 않는다
    (빈 A-Box 로 모든 DP 가 비어 보이므로 오표기 회피).
    """
    stats = _compute_stats(
        prop_values.get((class_name, prop_name), []),
        info["range"],
        prop_name=prop_name,
    )
    entry = {
        "label_en": info["label_en"],
        "label_ko": info["label_ko"],
        "range": f"xsd:{info['range']}" if info["range"] else "",
    }
    if inherited_from:
        entry["inherited_from"] = inherited_from
    # 출처 CSV 컬럼 (T-Box dcterms:source). A-Box 생성기의 vocabulary contract
    # 경로가 이름 추측 없이 컬럼↔DP 를 잇는 근거이며, SME 가 딕셔너리만 보고
    # DP 의 원본 컬럼을 역추적할 수 있게 한다.
    if info.get("source_columns"):
        entry["source_columns"] = info["source_columns"]
    if info["description_ko"]:
        entry["description"] = info["description_ko"]
    if not inherited_from and info["functional"]:
        entry["functional"] = True
    for key in _DT_STATS_KEYS:
        if key in stats:
            entry[key] = stats[key]
    # 코드값 DP 에 코드값 의미와 SME 업무 그룹을 붙여, NL→SPARQL 엔진이 업무 용어를
    # 올바른 원시 코드 필터로 옮기게 한다. 키는 DP 의 소스 컬럼 코드다
    # (``_column_codes``: ``source_columns`` 우선, 없으면 라벨에 박힌 코드).
    col_codes = _column_codes(info)
    if col_codes:
        cm = _load_code_meanings()
        std = _load_std_item_metadata()
        for col_code in col_codes:
            meanings = cm["columns"].get(col_code)
            if meanings and "code_meanings" not in entry:
                entry["code_meanings"] = meanings
            groups = cm["business_groups"].get(col_code)
            if groups and "business_groups" not in entry:
                entry["business_groups"] = groups
            _attach_std_item_authority(entry, std, col_code)
    if emit_population:
        dp_count = stats.get("count", 0)
        entry["is_populated"] = dp_count > 0
        if dp_count > 0:
            entry["populated_count"] = dp_count
            if instance_count > 0:
                entry["coverage"] = round(min(dp_count / instance_count, 1.0), 4)
    return entry


def _build_class_dt_props(
    name: str, superclasses: list[str], dt_prop_info: dict, prop_values: dict,
    instance_count: int = 0, emit_population: bool = False,
) -> dict:
    """클래스의 datatype_properties dict (직접 + 상속)."""
    result = {}
    for prop_name, info in dt_prop_info.items():
        if name in info["domains"]:
            result[prop_name] = _build_dt_prop_entry(
                info, prop_name, name, prop_values,
                instance_count=instance_count, emit_population=emit_population,
            )
    # 상속 프로퍼티 (이미 본인 도메인에 있으면 skip)
    for sc in superclasses:
        for prop_name, info in dt_prop_info.items():
            if sc in info["domains"] and prop_name not in result:
                result[prop_name] = _build_dt_prop_entry(
                    info, prop_name, name, prop_values, inherited_from=sc,
                    instance_count=instance_count, emit_population=emit_population,
                )
    return result


def _build_op_outgoing_incoming(
    name: str, outgoing_by_class: dict, incoming_by_class: dict,
    op_counts: dict | None = None,
) -> tuple[list, list]:
    """클래스의 outgoing / incoming ObjectProperty entry list.

    ``op_counts`` 가 주어지면 각 항목에 ``triple_count`` / ``is_populated`` 를
    붙인다 — LLM 이 값 0건 관계를 고르지 않게 하는 유일한 신호다. 예전에는 이
    인자가 없어 클래스 단위 OP 항목 **396개 전부가 population 신호 0개** 였다
    (DP 는 같은 위치에서 ``is_populated`` 를 항상 받는다).
    """
    # ``None`` 과 빈 dict 를 구분한다. ``None`` = v1(A-Box 전, 신호 없음),
    # ``{}`` = v2 인데 측정 결과가 비어 있음(= 전부 0건, 신호를 붙여야 한다).
    # ``op_counts or {}`` 로 뭉개면 두 경우가 같아져 v1 에 거짓 신호가 심긴다.
    emit = op_counts is not None
    counts = op_counts or {}

    def _sig(prop_name: str) -> dict:
        if not emit:
            return {}
        c = counts.get(prop_name, 0)
        return {"triple_count": c, "is_populated": c > 0}

    out = [
        {
            "property": p["name"],
            "label_ko": p["label_ko"],
            "target": p["ranges"],
            **_sig(p["name"]),
            **({"inverseOf": p["inverseOf"]} if p["inverseOf"] else {}),
            **({"subPropertyOf": p["subPropertyOf"]} if p["subPropertyOf"] else {}),
        }
        for p in outgoing_by_class.get(name, [])
    ]
    inn = [
        {
            "property": p["name"],
            "label_ko": p["label_ko"],
            "source": p["domains"],
            **_sig(p["name"]),
            **({"inverseOf": p["inverseOf"]} if p["inverseOf"] else {}),
        }
        for p in incoming_by_class.get(name, [])
    ]
    return out, inn


def _attach_restriction_metadata(
    cls_entry: dict, classes_dict: dict, restriction: dict,
    name: str, superclasses: list[str], dt_props_detail: dict,
) -> None:
    """equivalentClass Restriction 발견 시 cls_entry 에 SPARQL note + 부모 통계 보강."""
    cls_entry["owl_equivalentClass_restriction"] = restriction
    on_prop = restriction.get("onProperty", "")
    has_value = restriction.get("hasValue", "")
    some_values = restriction.get("someValuesFrom", "")
    parent = superclasses[0] if superclasses else ""
    pfx = NS_PREFIX
    if has_value:
        cls_entry["sparql_query_note"] = (
            f"all_inferred.ttl 로드 시 직접 쿼리 가능: ?x a {pfx}:{name}. "
            f'또는 부모 클래스로도 가능: ?x a {pfx}:{parent} ; '
            f'{pfx}:{on_prop} "{has_value}"^^xsd:string'
        )
    elif some_values:
        cls_entry["sparql_query_note"] = (
            f"all_inferred.ttl 로드 시 직접 쿼리 가능: ?x a {pfx}:{name}. "
            f"또는 부모 클래스로도 가능: ?x a {pfx}:{parent} ; "
            f"{pfx}:{on_prop} ?y . ?y a {pfx}:{some_values}"
        )
    # 부모 DP 통계를 자식의 비어있는 필드에 propagate
    if parent in classes_dict:
        parent_dt = classes_dict[parent].get("datatype_properties", {})
        for pn, pi in dt_props_detail.items():
            pp = parent_dt.get(pn)
            if not pp:
                continue
            for key in (
                "example_values", "distinct_values", "distinct_count",
                "min", "max",
            ):
                if key not in pi and key in pp:
                    pi[key] = pp[key]


def _build_classes_dict(
    tbox, steel_classes, subclass_map, props, include_stats: bool = True,
) -> dict:
    """클래스별 entry (label/desc/superclasses/instance_count/dt_props/op_props) 통합."""
    dt_prop_info = props["dt_prop_info"]
    outgoing_by_class = props["outgoing_by_class"]
    incoming_by_class = props["incoming_by_class"]
    prop_values = props["prop_values"]
    instance_counts = props["instance_counts"]

    classes_dict: dict = {}
    for cls in steel_classes:
        name = _local_name(cls)
        en, ko = _get_labels(tbox, cls)
        desc_en, desc_ko = _get_comments(tbox, cls)
        superclasses = sorted(
            _local_name(o)
            for _, _, o in tbox.triples((cls, RDFS.subClassOf, None))
            if isinstance(o, URIRef)
            and _local_name(o) not in ("Class", "Thing", "Resource")
        )
        subclasses = sorted(
            _local_name(sub)
            for sub in subclass_map.get(cls, set())
            if _local_name(sub) != "Nothing"
        )

        # instance_count 를 dt_props 빌드 전에 계산 (coverage 산출용). 값은 동일하고
        # 아래 cls_entry["instance_count"] 에도 재사용.
        inst_ct = instance_counts.get(cls, 0)
        dt_props_detail = _build_class_dt_props(
            name, superclasses, dt_prop_info, prop_values,
            instance_count=inst_ct, emit_population=include_stats,
        )
        # v1(vocabulary contract) 은 A-Box 가 아직 없으므로 신호를 붙이지 않는다 —
        # DP 의 ``emit_population=include_stats`` 와 같은 규약이다. v1 에서 전부
        # ``is_populated=False`` 로 찍으면 값이 있는 OP 에도 거짓을 심는다.
        out_props, in_props = _build_op_outgoing_incoming(
            name, outgoing_by_class, incoming_by_class,
            props.get("op_counts") if include_stats else None,
        )

        cls_entry: dict = {
            "label_en": en,
            "label_ko": ko,
            "description_en": desc_en,
            "description_ko": desc_ko,
            "superclasses": superclasses,
        }
        if subclasses:
            cls_entry["subclasses"] = subclasses
        cls_entry["instance_count"] = inst_ct
        cls_entry["datatype_properties"] = dt_props_detail

        restriction = _get_equivalent_class_restriction(tbox, cls)
        if restriction:
            _attach_restriction_metadata(
                cls_entry, classes_dict, restriction,
                name, superclasses, dt_props_detail,
            )

        union = _get_union_members(tbox, cls)
        if union:
            cls_entry["owl_unionOf"] = union
        disjoint = _get_disjoint_with(tbox, cls)
        if disjoint:
            cls_entry["owl_disjointWith"] = disjoint

        cls_entry["object_properties_outgoing"] = out_props
        cls_entry["object_properties_incoming"] = in_props

        # T-Box 가 owl:deprecated 로 "쓰지 말라" 고 표시한 클래스를 표식 없이
        # 게시하면 LLM 이 그것을 정상 어휘로 고른다. 실측 (2026-08-28): T-Box
        # deprecated 13건인데 딕셔너리 전문에 그 문자열이 **0회** 였다.
        if (cls, OWL.deprecated, Literal(True)) in tbox:
            cls_entry["deprecated"] = True
            marker_ko, marker_en = "[DEPRECATED — 사용 금지]", "[DEPRECATED]"
            for key, marker in (
                ("description_ko", marker_ko), ("description_en", marker_en),
            ):
                text = cls_entry.get(key) or ""
                if marker not in text:
                    cls_entry[key] = f"{marker} {text}".strip()

        classes_dict[name] = cls_entry

    return classes_dict


def _estimated_op_provenance() -> dict[str, dict]:
    """OPs whose triples come from a *bucket join*, keyed by property name.

    2026-08-28 실측: ``hasStackEquipment`` 의 딕셔너리 설명은 "대기 배출이 측정된
    굴뚝 또는 배출 설비를 연결한다" 인데, 실제 트리플은 tacit ``shared_column_join``
    이 만든 **같은 구역 설비** 다 — 배출 측정 1건이 설비 7.15대와 연결되고 distinct
    object 가 50 = 전체 설비다(판별력 0). 설명이 데이터와 정반대라 LLM 이 그 관계를
    측정 사실로 읽고 top-N 을 만든다.

    딕셔너리 전문에 ``tacit`` / ``estimated`` / ``confidence`` 문자열이 0회였다.
    규칙 파일에는 ``_confidence: medium`` + "SME 확인 필요" 가 있었으므로 문제는
    지식 부재가 아니라 **전달 누락** 이다.

    도메인 중립: 프로퍼티 이름을 코드에 박지 않고 규칙 파일에서 읽는다. 파일이
    없거나 형식이 다르면 빈 dict — 신호가 없는 것이 거짓 신호보다 낫다.
    """
    try:
        from domain.rules_paths import rules_path

        with open(rules_path("tacit_rules.json"), encoding="utf-8") as fh:
            mappings = json.load(fh).get("mappings") or []
    except Exception:
        return {}

    estimated: dict[str, dict] = {}
    for rule in mappings:
        if not isinstance(rule, dict) or rule.get("strategy") != "shared_column_join":
            continue
        name = rule.get("op")
        if not name:
            continue
        bucket = (
            rule.get("source_bucket_column")
            or rule.get("source_lookup_bucket_column")
            or rule.get("target_bucket_column")
        )
        estimated[name] = {
            "derivation": "tacit_bucket_join",
            "confidence": str(rule.get("_confidence") or "unspecified").lower(),
            "bucket_column": bucket,
            "caveat": (
                "추정 관계 — 기록된 FK 가 아니라 같은 버킷(구역) 공유로 만든 다대다 "
                "링크다. 측정 사실로 읽지 말 것. 이 관계로 순위/top-N 을 만들면 "
                "같은 버킷 구성원이 동점이 되어 임의로 잘린다."
            ),
        }
    return estimated


def _build_object_properties_dict(
    obj_props: list, op_counts: dict, *, emit_population: bool = True,
    abox_op_counts: dict | None = None,
) -> dict:
    """ObjectProperty 별 메타 + triple_count 통합 dict.

    Args:
        op_counts: 딕셔너리가 보고할 카운트 (``use_inferred`` 에 따라 추론 또는
            A-Box 기준).
        abox_op_counts: **A-Box 전용** 카운트. ``op_counts`` 가 추론 기준일 때만
            의미가 있다 — 두 값을 비교해 "추론에만 있는 관계" 를 표시한다.
            ``None`` 이면 비교를 건너뛴다 (v1 이거나 이미 A-Box 기준).
    """
    estimated = _estimated_op_provenance()
    result = {}
    for prop in sorted(obj_props, key=lambda x: x["name"]):
        entry = {
            "label_en": prop["label_en"],
            "label_ko": prop["label_ko"],
            "description_en": prop["description_en"],
            "description_ko": prop["description_ko"],
            "domain": prop["domains"],
            "range": prop["ranges"],
        }
        if prop["inverseOf"]:
            entry["inverseOf"] = prop["inverseOf"]
        if prop["subPropertyOf"]:
            entry["subPropertyOf"] = prop["subPropertyOf"]
        if prop["transitive"]:
            entry["transitive"] = True
        # ``is_populated`` 는 **항상** 부여한다 (DP 와 동일 계약,
        # ``_build_datatype_properties`` 참조). 예전에는 ``op_counts`` 에 키가 있을
        # 때만 ``triple_count`` 를 붙여, 값 0건 OP 는 **키 자체가 없었다** — 실측
        # (2026-08-12): OP 229개 중 신호 보유 14개 vs DP 254/254. 그래서 NL→SPARQL
        # 소비자가 DP 는 ∅ 로 회피할 수 있지만 OP 는 구분 근거가 없어 빈 관계를
        # 고르고, 0행이 에러 없이 "정답처럼" 반환됐다.
        # v1(vocabulary contract) 은 A-Box 전이라 신호를 붙이지 않는다 — 전부
        # False 로 찍으면 값이 있는 OP 에도 거짓을 심는다 (DP 의
        # ``emit_population`` 과 같은 규약).
        if emit_population:
            count = op_counts.get(prop["name"], 0)
            entry["triple_count"] = count
            entry["is_populated"] = count > 0
            # ``triple_count`` 가 추론 그래프에서 왔다면, 그 트리플이 A-Box 에도
            # 있는지 구분해야 한다. 없으면 ``a_box.ttl`` 질의는 0행이다 —
            # ``is_populated: true`` 만 보고 그 관계를 고른 LLM 이 조용히 틀린
            # 결과를 받는다 (``_build_metadata`` 의 counts_source 주석 참조).
            if abox_op_counts is not None:
                pre_inf = abox_op_counts.get(prop["name"], 0)
                entry["pre_inference_triple_count"] = pre_inf
                if count and not pre_inf:
                    entry["requires_inferred_graph"] = True
                    entry["query_note"] = (
                        "이 관계는 추론(owl:inverseOf 등)으로만 존재한다 — "
                        "a_box.ttl 만 로드하면 0행이다. all_inferred.ttl 을 "
                        "쓰거나 역방향 프로퍼티로 질의하라"
                        + (f" (inverseOf: {prop['inverseOf']})"
                           if prop.get("inverseOf") else "")
                    )
        # 추정 유래 표시는 v1(구조 계약)에도 붙인다 — 파생 방식은 A-Box 유무와
        # 무관한 사실이고, 이 신호가 없으면 소비자가 버킷 조인을 측정 사실로 읽는다.
        if prop["name"] in estimated:
            entry["provenance"] = estimated[prop["name"]]
            # 설명문에도 표식을 넣는다. ``provenance`` 만 두면 T-Box 의 rdfs:comment
            # 가 그대로 남아 **정반대 주장**을 한다 — 실측: hasStackEquipment 의
            # description_ko 는 "대기 배출이 측정된 굴뚝 또는 배출 설비를 연결한다"
            # 인데 실제 트리플은 같은 구역 설비다. LLM 은 설명문을 먼저 읽으므로
            # 별도 필드보다 이 한 줄이 오독을 막는다.
            for key in ("description_ko", "description_en"):
                marker = "[추정: 버킷 조인]" if key.endswith("ko") else "[ESTIMATED: bucket join]"
                text = entry.get(key) or ""
                if marker not in text:
                    entry[key] = f"{marker} {text}".strip()
        result[prop["name"]] = entry
    return result


def _build_functional_properties_dict(dt_prop_info: dict) -> dict:
    """FunctionalProperty 만 추출해 별도 섹션."""
    result = {}
    for name, info in dt_prop_info.items():
        if not info["functional"]:
            continue
        result[name] = {
            "label_en": info["label_en"],
            "label_ko": info["label_ko"],
            "range": f"xsd:{info['range']}" if info["range"] else "",
            "description": info["description_ko"],
            "domain_class": info["domains"][0] if info["domains"] else "",
        }
    return result


def _build_column_to_classes_index(classes_dict: dict) -> dict:
    """{CSV 컬럼 → [클래스]} 역색인. 같은 컬럼을 여러 클래스가 가질 때 선택 근거다.

    **왜 필요한가**: 질의를 쓰려면 "이 컬럼이 어느 클래스에 있나" 를 알아야 하는데,
    딕셔너리는 클래스별로만 나열돼 있어 **클래스를 하나 고른 뒤 그 안을 뒤지는**
    순서가 된다. 그러면 고른 클래스에 컬럼이 없을 때 "이 클래스엔 없구나" 하고
    다른 클래스에서 찾게 되고, 결국 여러 클래스를 조인하는 잘못된 질의가 나온다.

    가상의 예: 집계에 필요한 세 컬럼 (CODE_COL_1 / WEIGHT_COL_1 / DATE_COL_2) 이
    **MaterialA 한 클래스에 모두 있는데**, MaterialADetail 을 먼저 골라 WEIGHT_COL_1
    이 없는 것을 보고 MaterialA 를 조인하면 모집단이 바뀌어 건수가 어긋난다. 이
    역색인이 있으면 "세 컬럼을 다 가진 클래스" 를 한눈에 찾는다.

    ``is_populated=False`` 인 DP 는 값이 없어 질의에 쓸 수 없으므로 표시해 둔다.

    **전체 컬럼을 등재한다.** 단일 클래스 컬럼은 선택 고민이 없다는 이유로 빼면
    안 된다. 역색인의 실제 용도는 "선택 고민 해소" 가 아니라 **"이 컬럼이 KG 에
    있는가, 있으면 어디에"** 조회다. 여러 클래스가 공유하는 컬럼만 남기면 필수 제외
    필터나 측정 본체처럼 **한 클래스에만 있는 핵심 컬럼이 조회되지 않고**, 역색인에서
    못 찾은 사람은 "KG 에 없다" 고 결론내고 엉뚱한 대체 컬럼을 쓰게 된다.
    """
    index: dict[str, list[str]] = {}
    for cls_name, cls_info in classes_dict.items():
        for dp_name, dp_info in (cls_info.get("datatype_properties") or {}).items():
            if not isinstance(dp_info, dict):
                continue
            populated = dp_info.get("is_populated")
            for col in dp_info.get("source_columns") or []:
                key = str(col).strip().upper()
                if not key:
                    continue
                mark = "" if populated is not False else f" ({_EMPTY_DP_MARKER})"
                index.setdefault(key, []).append(f"{cls_name}.{dp_name}{mark}")
    return {col: sorted(owners) for col, owners in sorted(index.items())}


def _resolve_columns_to_dps(
    columns, col_index: dict[str, list[str]],
) -> tuple[dict, str, list[str]]:
    """SME 규칙의 컬럼 코드를 지금 그 컬럼을 담는 DP 이름으로 매핑한다.

    ``({COL: [Class.dp]}, status, empty_columns)`` 를 반환한다. status 는:

    - ``ok``: 모든 컬럼이 **값이 있는** DP 하나 이상으로 해석됐다.
    - ``partial``: 일부 컬럼만 해석됐고 나머지는 없거나 값이 없다.
    - ``absent``: 쓸 수 있는 것이 없다. 이 KG 로는 규칙을 평가할 수 없다.

    모든 DP 가 비어 있는 컬럼은 해석되지 *않은* 것으로 센다. 그것을 ``ok`` 로
    선언하면 실행 가능해 보이지만 아무것도 반환하지 않는 규칙이 된다. 가상의 예로
    자재 매수를 세는 컬럼을 세 클래스가 선언했지만 A-Box 에서 셋 다 비어 있으면,
    그 컬럼에 대한 COUNT 는 에러 없이 0 을 낸다. 빈 컬럼은 따로 이름을 남겨
    "매핑이 틀림" 과 "매핑은 맞고 데이터가 없음" 을 구분하게 한다.

    DP 이름이 아니라 **컬럼** 으로 해석하는 것은 ``_resolve_canonical_designations``
    와 같은 안정성 논리다. DP 이름은 LLM 이 짓고 S2 재생성마다 바뀌지만 소스 컬럼
    코드는 바뀌지 않는다.
    """
    resolved: dict[str, list[str]] = {}
    missing: list[str] = []
    empty: list[str] = []
    for col in columns or []:
        key = str(col).strip().upper()
        owners = col_index.get(key)
        if not owners:
            missing.append(key)
            continue
        populated = [o for o in owners if _EMPTY_DP_MARKER not in o]
        if populated:
            resolved[key] = populated
        else:
            # 매핑은 맞지만 값이 없다 — 질의에 쓰면 조용히 0건이 된다.
            empty.append(key)
            resolved[key] = owners
    if not resolved or not any(
        col not in empty for col in resolved
    ):
        status = "absent"
    elif missing or empty:
        status = "partial"
    else:
        status = "ok"
    return resolved, status, empty


def _build_business_rules(classes_dict: dict, col_index: dict) -> dict:
    """SME 업무판정 규칙 섹션. 규칙 파일에는 있으나 딕셔너리에 없던 판정 기준을 싣는다.

    **왜 필요한가**: 컬럼과 코드값이 딕셔너리에 다 있어도 "그 코드를 어떻게 묶어
    무엇으로 판정하는가" 가 없으면, 문법이 맞는 질의를 쓰고도 값이 틀린다. 예를 들어
    집계 기준일의 경계 (교대 기준 시각 등) 를 모르면 일자별 집계 전부가 어긋나고,
    처리 방법 같은 구분 기준을 모르면 구분 A/B 를 나눌 수 없다.

    규칙은 두 소스에서 온다:
      - ``sme_business_rules.json``: SME 가 관리하는 판정식·파생계산·자릿수 판정
      - ``user_term_dictionary.json``: 구어체 용어 → 컬럼 (용어 사전 추출물)

    각 규칙의 컬럼 참조는 **현재 존재하는 DP 로 재해석** 해 붙인다. 재해석 실패는
    숨기지 않고 ``_resolution`` 에 ``absent``/``partial`` 로 남긴다. 현 KG 로 답할
    수 없는 질문 (원본 컬럼이 제공되지 않은 판정) 에 엔진이 억지 매핑하지 않게 하는
    것이 목적이다.

    도메인-중립: 두 파일이 없으면 ``{}`` 를 반환해 섹션이 생기지 않는다.
    """
    sme = _load_sme_business_rules()
    terms = _read_json_section(_USER_TERM_DICT_PATH, "terms")
    if not sme and not terms:
        return {}

    section: dict = {
        "_comment": (
            "SME 업무사전이 명시한 업무판정 규칙. 집계 질의를 쓰기 전에 "
            "여기서 해당 용어의 판정 기준을 먼저 확인할 것 — 코드값만 보고 필터하면 "
            "문법은 맞고 값이 틀린다. 각 규칙의 columns 는 현재 DP 로 재해석돼 "
            "resolved_properties 에 실려 있고, _resolution=absent 면 그 규칙은 현 KG 로 "
            "판정 불가(원본 컬럼 미제공)다."
        ),
    }
    if sme.get("_sources"):
        section["_sources"] = sme["_sources"]

    # 파생 측정/판정 — 컬럼을 현재 DP 로 재해석해 실행 가능성을 표시.
    measures: dict = {}
    for name, spec in _strip_meta(sme.get("derived_measures") or {}).items():
        if not isinstance(spec, dict):
            continue
        out = dict(spec)
        resolved, status, empty = _resolve_columns_to_dps(
            spec.get("columns"), col_index,
        )
        if resolved:
            out["resolved_properties"] = resolved
        out["_resolution"] = status
        if empty:
            out["_empty_in_abox"] = {
                "columns": empty,
                "note": (
                    "컬럼↔DP 매핑은 맞으나 해당 DP 가 A-Box 에서 전부 비어 있다. "
                    "이 규칙으로 집계하면 에러 없이 0건이 나온다 — 값 없음을 "
                    "'해당 없음' 으로 보고하지 말 것."
                ),
            }
        # 선언된 kg_feasible 과 실측 재해석이 어긋나면 감사 신호를 남긴다.
        if spec.get("kg_feasible") is True and status != "ok":
            out["_resolution_warning"] = (
                "kg_feasible=true 로 선언됐으나 일부 컬럼이 현 KG 에 없거나 "
                "값이 비어 있다 — 재확인 필요."
            )
        measures[name] = out
    if measures:
        section["derived_measures"] = measures

    # 자릿수 판정 (SUBSTR 계열). 레이아웃 자체는 DP 의 code_structure 가 권위 소스.
    positional: dict = {}
    for col, rules in _strip_meta(sme.get("positional_rules") or {}).items():
        resolved, status, empty = _resolve_columns_to_dps([col], col_index)
        entry: dict = {"rules": rules}
        if resolved:
            entry["resolved_properties"] = resolved.get(col.upper(), [])
        if status != "ok":
            entry["_resolution"] = status
        if empty:
            entry["_empty_in_abox"] = True
        positional[col.upper()] = entry
    if positional:
        section["positional_rules"] = positional

    # 구어체 용어 → 컬럼 → 현재 DP. 옛 DP 이름을 그대로 싣지 않는다.
    term_map: dict = {}
    for col, spec in terms.items():
        if not isinstance(spec, dict):
            continue
        user_terms = [t for t in (spec.get("user_terms") or []) if t]
        if not user_terms:
            continue
        key = str(col).strip().upper()
        resolved, status, empty = _resolve_columns_to_dps([key], col_index)
        entry = {
            "column": key,
            "column_name": spec.get("column_name", ""),
            "user_terms": user_terms,
        }
        if resolved:
            entry["resolved_properties"] = resolved.get(key, [])
        if status != "ok":
            entry["_resolution"] = status
        if empty:
            entry["_empty_in_abox"] = True
        for term in user_terms:
            term_map.setdefault(term, []).append(entry)
    if term_map:
        section["user_term_to_property"] = dict(sorted(term_map.items()))

    if sme.get("unmapped_sme_terms"):
        section["unmapped_sme_terms"] = sme["unmapped_sme_terms"]
    return section


def _descendant_names(parent: str, subclass_map: dict, ns: str) -> list[str]:
    """``parent`` 의 모든 후손 로컬명 (BFS, 순환 방어, **결정적**).

    2026-08-30: 두 곳이 비결정적이었다 — docstring 은 BFS 라 적혀 있는데
    ``queue.pop()`` 은 LIFO(DFS)였고, ``subclass_map`` 의 값이 **set** 이라 형제
    방문 순서가 PYTHONHASHSEED 에 좌우됐다. 호출자
    (``_build_class_quick_reference``)가 결과를 ``[:3]`` 으로 자르므로 순서가 곧
    **선택**이 된다 — 실측 5-seed 재현: ``class_quick_reference`` 83개 중 23개가
    흔들리고 **12개는 내용까지 달랐다** (예: ``EnergyConsumption`` 의 상속 표기
    대표가 ``FuelConsumption`` ↔ ``GasEnergy``).

    이 값은 ``tools/bedrock.py`` 가 LLM 프롬프트에 직접 주입한다. 즉 같은 KG 로
    질의해도 실행마다 다른 속성을 안내했다.

    ``collections.deque`` + 형제 정렬로 진짜 BFS 를 결정적으로 돈다.
    """
    from collections import deque

    from domain.uri_conventions import local_name as _ln

    seen: set[str] = set()
    queue: deque[str] = deque([parent])
    out: list[str] = []
    while queue:
        cur = queue.popleft()          # FIFO — docstring 이 약속한 BFS
        children = sorted(
            _ln(str(child))
            for child in subclass_map.get(URIRef(ns + cur), set())
        )
        for name in children:          # 형제는 이름 순 (set 순회 비결정성 제거)
            if name in seen or name == parent:
                continue
            seen.add(name)
            out.append(name)
            queue.append(name)
    return out


def _build_class_quick_reference(
    classes_dict: dict, subclass_map: dict | None = None, ns: str | None = None,
) -> dict:
    """클래스별 단축 참조 문자열 (DP/OP 한 줄 요약 — LLM 프롬프트용).

    추상 부모는 자체 DP/OP 가 없어 빈 문자열이 된다. 실측 (2026-08-28): 95개 중
    **26개가 공백**이고 그중 최다가 ``EquipmentOperationRecord`` (인스턴스 36,560 —
    이 KG 최대 클래스)다. LLM 은 그것을 "질의할 속성이 없다" 로 읽으므로 가장 큰
    클래스가 질의 대상에서 빠진다.

    ``subclass_map`` 이 주어지면 **후손의 속성을 상속 표기**로 채운다 (원래 값이
    빈 경우에만 — 자체 속성이 있으면 건드리지 않는다).
    """
    result = {}
    for cls_name, cls_info in classes_dict.items():
        parts = []
        for pn, pi in cls_info.get("datatype_properties", {}).items():
            rng = pi.get("range", "").replace("xsd:", "")
            # 빈 DP 마커(∅): v2 에서만 is_populated 가 부여되므로 identity 체크.
            # v1 은 키 부재(None)라 마커가 절대 붙지 않는다.
            empty = ",∅" if pi.get("is_populated") is False else ""
            dv = pi.get("distinct_values")
            if dv and len(dv) <= 10:
                parts.append(f"{pn}({rng}:{'/'.join(str(v) for v in dv)})")
            else:
                parts.append(f"{pn}({rng}{empty})")
        op_parts = [
            f"{op['property']}→{'|'.join(op.get('target', []))}"
            for op in cls_info.get("object_properties_outgoing", [])
        ]
        if op_parts:
            parts.append("| ObjProp: " + ", ".join(op_parts))
        result[cls_name] = " ".join(parts) if parts else ""

    # ── 빈 부모를 후손 속성으로 채운다 ────────────────────────────────
    if subclass_map and ns:
        for cls_name, text in list(result.items()):
            if text:
                continue                      # 자체 속성이 있으면 건드리지 않는다
            inherited: list[str] = []
            for child in _descendant_names(cls_name, subclass_map, ns):
                child_text = result.get(child) or ""
                if child_text:
                    inherited.append(f"[{child}] {child_text}")
                if len(inherited) >= 3:       # 프롬프트 비용 — 대표 3개까지
                    break
            if inherited:
                result[cls_name] = (
                    "(추상 클래스 — 자체 속성 없음. 후손 속성으로 질의) "
                    + " ".join(inherited)
                )
    return result


def _compute_class_statistics(
    abox, tbox, steel_classes, subclass_map, props, include_stats: bool = True,
    abox_op_counts: dict | None = None,
):
    """클래스 + ObjectProperty + FunctionalProperty + quick reference 통합.

    Args:
        abox_op_counts: A-Box 전용 OP 카운트. ``props["op_counts"]`` 가 추론
            기준일 때 "추론에만 있는 관계" 를 표시하는 데 쓴다.

    반환: (classes_dict, obj_props_dict, func_props, class_quick_ref).
    """
    classes_dict = _build_classes_dict(
        tbox, steel_classes, subclass_map, props, include_stats=include_stats,
    )
    obj_props_dict = _build_object_properties_dict(
        props["obj_props"], props["op_counts"], emit_population=include_stats,
        abox_op_counts=abox_op_counts,
    )
    func_props = _build_functional_properties_dict(props["dt_prop_info"])
    class_quick_ref = _build_class_quick_reference(
        classes_dict, subclass_map, str(DOMAIN_NS),
    )
    return classes_dict, obj_props_dict, func_props, class_quick_ref


# ── 동적 딕셔너리 (T-Box 분석 기반) ──────────────


def _build_common_mistakes(tbox, class_names):
    """SPARQL 쿼리에서 자주 발생하는 실수/팁 딕셔너리를 T-Box에서 동적 생성한다.

    Args:
        tbox: T-Box rdflib Graph
        class_names: T-Box 클래스명 집합 (set[str])
    """
    import re

    # 1) classes: 약칭 → 실제 클래스명 매핑을 T-Box에서 동적 생성
    # 전략: 클래스명의 접미사("Master", "Process", "History" 등)를 제거한 약칭과
    #       하위 단어(PascalCase 분리)를 키로, 원래 클래스명을 값으로 매핑
    class_aliases = {}
    for cname in sorted(class_names):
        # PascalCase를 단어로 분리: "EquipmentMaster" → ["Equipment", "Master"]
        words = re.findall(r"[A-Z][a-z0-9]*", cname)
        if len(words) >= 2:
            # "EquipmentMaster" → "Equipment" 약칭
            for suffix in ("Master", "History", "Status", "Events", "Data", "Results", "Quality"):
                if cname.endswith(suffix) and cname != suffix:
                    alias = cname[: -len(suffix)]
                    if alias not in class_names and alias not in class_aliases:
                        class_aliases[alias] = cname
            # "ProcessBlastFurnace" → "BlastFurnace" 약칭
            if words[0] == "Process" and len(words) > 1:
                short = "".join(words[1:])
                if short not in class_names and short not in class_aliases:
                    class_aliases[short] = cname

    # 2) properties: 자주 혼동되는 프로퍼티명 매핑을 T-Box에서 동적 생성
    prop_hints = {}
    dt_props = {}  # name → domains
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        pname = _local_name(s)
        domains = [
            _local_name(d)
            for _, _, d in tbox.triples((s, RDFS.domain, None))
            if isinstance(d, URIRef)
        ]
        dt_props[pname] = domains

    # "ID" vs "Id" 혼동 감지
    for pname in dt_props:
        if pname.endswith("Id"):
            wrong = pname[:-2] + "ID"
            prop_hints[wrong] = f"{pname} (소문자 d)"

    # 특정 클래스에만 존재하는 프로퍼티 주석
    for pname, domains in dt_props.items():
        if len(domains) == 1:
            prop_hints.setdefault(
                pname,
                f"{domains[0]} 클래스에만 존재",
            )

    # 3) functions: SPARQL 환경 호환성 — 일부 SPARQL 엔진은 아래 함수 미지원/제한
    functions = {
        "YEAR(?date)": "엔진 호환성 한계 가능 — 범위 비교로 대체 안전",
        "MONTH(?date)": "엔진 호환성 한계 가능 — 범위 비교로 대체 안전",
        "DAY(?date)": "엔진 호환성 한계 가능 — 중첩 IF + 날짜 범위 권장",
        "HOUR(?ts)": '엔진 호환성 한계 가능 — STRAFTER(STR(?ts), "T") 문자열 비교 권장',
        "ROUND(?x, 2)": "SPARQL ROUND()는 인자 1개만",
        "GROUP_CONCAT": "엔진별 미지원 가능",
        "NOW()": "엔진별 날짜 산술 미지원 가능 — 고정 날짜 사용",
    }

    return {
        "classes": class_aliases,
        "properties": prop_hints,
        "functions": functions,
    }


def _build_sparql_guide(classes_dict, tbox, obj_props,
                        canonical_designations: dict | None = None,
                        business_rules: dict | None = None):
    """SPARQL 가이드 (엔진 호환성, 안티패턴, 공통 패턴) 를 T-Box에서 동적 생성한다.

    Args:
        classes_dict: 클래스 딕셔너리 (이미 구성된 것)
        tbox: T-Box rdflib Graph
        obj_props: ObjectProperty 목록 (list[dict])
        canonical_designations: SME 검증 집계 규칙. 주어지면 가이드 맨 앞에
            ``MUST_READ_FIRST`` 포인터를 심는다 — 이 가이드만 읽고 질의를 쓰면
            문법은 맞아도 모집단/필수 필터를 놓쳐 값이 틀린다.
    """
    pfx = NS_PREFIX  # domain-local prefix (e.g. "steel", "fin", "health")

    # ── FK 프로퍼티 탐지 ──
    # T-Box에서 "Id" 로 끝나는 DatatypeProperty 중 여러 클래스에 공유되는 것을 FK 후보로 간주
    fk_properties = []
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        pname = _local_name(s)
        if pname.endswith("Id") or pname.endswith("Code"):
            fk_properties.append(pname)

    fk_join_hint = ", ".join(fk_properties[:5]) if fk_properties else "FK 프로퍼티"

    sparql_guide = {
        "engine_compatibility": {
            "date_filter": (
                'YEAR()/MONTH() 대신 범위 비교: FILTER(?date >= "2025-09-01T00:00:00"^^xsd:dateTime '
                '&& ?date < "2025-10-01T00:00:00"^^xsd:dateTime)'
            ),
            "aggregation": "COUNT, SUM, AVG 사용 시 반드시 GROUP BY 필요",
            "date_type": "모든 날짜는 xsd:dateTime 타입",
            "no_inference": "엔진이 OWL 추론을 수행하지 않을 수 있음. all_inferred.ttl 로드 시 추론 클래스 직접 쿼리 가능",
            "join_by_fk_not_timestamp": (
                f"서로 다른 클래스 간 조인은 {fk_join_hint} 등 FK 또는 ObjectProperty 사용. "
                "timestamp 조인 금지"
            ),
        },
        "anti_patterns": _build_anti_patterns(tbox, classes_dict, obj_props, pfx),
        "common_patterns": _build_common_patterns(tbox, classes_dict, pfx),
    }

    # ── inference_only_classes ──
    inference_classes = {}
    for cls_name, cls_info in classes_dict.items():
        if cls_info.get("owl_equivalentClass_restriction"):
            restriction = cls_info["owl_equivalentClass_restriction"]
            parents = cls_info.get("superclasses", [])
            parent = parents[0] if parents else ""
            on_prop = restriction.get("onProperty", "")
            has_value = restriction.get("hasValue", "")
            some_values = restriction.get("someValuesFrom", "")
            if has_value:
                inference_classes[cls_name] = {
                    "parent": parent,
                    "filter": f'{pfx}:{on_prop} "{has_value}"',
                }
            elif some_values:
                inference_classes[cls_name] = {
                    "parent": parent,
                    "filter": f"{pfx}:{on_prop} ?any{some_values}",
                }

    sparql_guide["engine_compatibility"]["inference_only_classes"] = {
        "description": (
            "OWL 추론 기반 분류 클래스. all_inferred.ttl 로드 시 직접 쿼리 가능. "
            "부모+FILTER도 동일 결과."
        ),
        "classes": inference_classes,
    }

    # 도메인 집계 규칙으로 가는 포인터. 이 가이드는 SPARQL **문법** 규칙만 담고
    # 있어서, 질의를 쓰는 사람이 "어느 클래스가 모집단인가 / 어떤 필터가 필수인가"
    # 를 여기서 찾지 못한다. canonical_designations 는 딕셔너리 뒤쪽 섹션에 있으므로
    # 앞 섹션이 그곳을 가리키지 않으면, 필수 필터를 빼 건수가 부풀거나 잘못된
    # 모집단 클래스를 골라 건수가 어긋나는 오답이 난다.
    business_rules = business_rules or {}
    if canonical_designations or business_rules:
        must_read = {
            "description": (
                "**집계 질의를 쓰기 전에 canonical_designations 와 business_rules 를 "
                "먼저 읽어라.** 이 sparql_guide 는 SPARQL 문법 규칙만 담는다. 어느 "
                "클래스가 모집단인지, 어떤 필터가 필수인지, 어느 날짜 컬럼이 기준인지는 "
                "canonical_designations 에 있고, SME 가 쓰는 업무용어를 어떤 코드값 "
                "조합으로 판정하는지는 business_rules 에 있다. 둘 중 하나라도 빼면 "
                "문법은 맞아도 값이 틀린다."
            ),
            "why_it_matters": (
                "실측 오답 사례 — 필수 필터(산출물 중량 != 0) 를 빼면 건수가 부풀고, "
                "모집단 클래스를 상세 테이블로 잘못 고르면 건수가 수배 어긋난다. "
                "계상일 경계(교대 기준 시각) 를 모르고 자정으로 자르면 일자별 "
                "집계가 매일 어긋난다."
            ),
        }
        if canonical_designations:
            # 밑줄로 시작하는 키는 주석/메타이므로 intent 목록에서 제외한다.
            must_read["canonical_designations_intents"] = sorted(
                k for k in canonical_designations if not k.startswith("_")
            )
        if business_rules.get("derived_measures"):
            must_read["business_rules_measures"] = sorted(
                business_rules["derived_measures"]
            )
            infeasible = sorted(
                name for name, spec in business_rules["derived_measures"].items()
                if isinstance(spec, dict) and spec.get("kg_feasible") is False
            )
            if infeasible:
                must_read["not_answerable_from_kg"] = {
                    "measures": infeasible,
                    "note": (
                        "원본 컬럼이 제공된 CSV 에 없어 현 KG 로 판정 불가. "
                        "대체 컬럼으로 근사하지 말고 답할 수 없음을 밝힐 것 "
                        "(business_rules.derived_measures 의 _blocker 참조)."
                    ),
                }
        sparql_guide["MUST_READ_FIRST"] = must_read

    return sparql_guide, inference_classes


def _build_anti_patterns(tbox, classes_dict, obj_props, pfx):
    """안티패턴 딕셔너리를 동적 생성한다.

    Generic SPARQL 안티패턴은 그대로 유지하고,
    도메인 특정 안티패턴은 T-Box 분석에서 생성한다.
    """
    anti_patterns = {
        # -- Generic SPARQL anti-patterns (도메인/엔진 무관) --
        "no_year_function": "FILTER(YEAR(?ts)=2025) → 범위 비교로 대체",
        "no_dateTime_cast": "xsd:dateTime(?ts) 캐스팅 불필요",
        "no_now_arithmetic": "NOW()-dayTimeDuration 미지원 → 고정 날짜",
        "no_aggregation_without_group_by": "집계 함수 시 GROUP BY 필수",
        "no_inference_class_in_query": (
            "all_inferred.ttl 로드 시 추론 클래스 사용 가능. a_box.ttl만이면 부모+FILTER"
        ),
        "no_select_alias_ref": "SELECT 절에서 다른 별칭 참조 불가 → 서브쿼리 사용",
        "no_unbound_group_by": "GROUP BY 변수는 반드시 트리플 패턴에서 바인딩",
        "no_count_distinct_for_rate": "COUNT(DISTINCT)는 비율이 아님 → SUM(IF)/COUNT(*) 사용",
        "no_timestamp_join": "다른 클래스 간 timestamp 조인 금지 → FK 프로퍼티 사용",
        "no_uri_string_parsing": "STR(?x) URI 파싱 금지 → DatatypeProperty 사용",
        "no_round_with_precision": "ROUND(?x,2) 불가 → 인자 1개만",
        "no_null_keyword": "SPARQL에 NULL 없음 → IF(조건,1,0)+SUM",
        "no_optional_bind_only": "OPTIONAL{BIND(...)} 무의미 → 밖에서 BIND",
        "no_variable_name_collision": "주어와 값에 같은 변수명 금지",
        "no_filter_on_optional_variable": "FILTER 대상은 필수 패턴으로, 나머지만 OPTIONAL",
        "no_hour_function": 'HOUR() 미지원 → STRAFTER(STR(?ts),"T") 비교',
        "no_optional_rematch": "OPTIONAL 안에서 이미 바인딩된 변수 재매칭 금지",
        "no_nonexistent_filter_value": "데이터에 없는 값으로 FILTER 금지 → distinct_values 확인",
        "no_group_concat": "GROUP_CONCAT 일부 엔진 미지원 가능",
        "no_day_function": "DAY() 미지원 → 중첩 IF 날짜 범위",
        "no_redundant_filter_with_inference_class": "추론 클래스 사용 시 동일 조건 FILTER 중복 불필요",
        "no_unbound_variable_in_filter": "FILTER/SELECT 변수는 트리플 패턴에서 바인딩 필수",
        # D4: 실측 범위 밖 값으로 FILTER 시 0건 반환 위험 — value_stats 확인 안내
        "filter_out_of_range": (
            "DP 의 value_stats.{min, max, p10, p90} 범위 밖 값으로 FILTER 금지. "
            "0건 반환은 'no data' 가 아니라 'out of range' 일 수 있음. "
            "쿼리 전 각 DP 엔트리의 value_stats 확인 — common_patterns.value_range_check 참조."
        ),
    }

    # -- 도메인 특정 안티패턴: T-Box에서 동적 감지 --

    # "Id" 프로퍼티의 대소문자 혼동 주의
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        pname = _local_name(s)
        if pname.endswith("Id"):
            wrong = pname[:-2] + "ID"
            anti_patterns[f"no_case_{pname}"] = f"{wrong}→{pname} 대소문자 구분"
            break  # 하나의 예시만

    # 단일 domain 프로퍼티 주의 (다른 클래스에서 사용 금지)
    single_domain_props = []
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        pname = _local_name(s)
        domains = [
            _local_name(d) for _, _, d in tbox.triples((s, RDFS.domain, None))
            if isinstance(d, URIRef) and str(d).startswith(DOMAIN_NS)
        ]
        if len(domains) == 1:
            single_domain_props.append((pname, domains[0]))

    if single_domain_props:
        sample = single_domain_props[0]
        anti_patterns["no_property_on_wrong_class"] = (
            f"{sample[0]}는 {sample[1]}에만 존재. 프로퍼티의 domain 확인 필수"
        )

    # ObjectProperty 이름 혼동 방지 (inverse 쌍)
    for prop in obj_props:
        if prop.get("inverseOf"):
            anti_patterns.setdefault(
                "no_wrong_direction_property",
                f"{prop['name']}와 {prop['inverseOf']}는 inverse 관계. 방향 확인 필수",
            )
            break

    return anti_patterns


def _compute_two_hop_paths(
    classes_dict: dict,
    abox_stats: dict | None = None,
    max_per_class: int = 10,
) -> dict[str, list[dict]]:
    """각 클래스에서 시작하는 자주 쓰일 만한 2홉 경로 top-N 을 계산한다.

    점수 휴리스틱:
    - master/catalog tier target: +3
    - transaction tier target: +1
    - FK 네이밍 OP (has* / is*Of 등): +2
    - self-referential (src==tgt): -100 (제외)
    - 인스턴스 희박 tgt: -1

    Args:
        classes_dict: 클래스별 outgoing ObjectProperty 정보 (from T-Box)
        abox_stats: A-Box 통계 (per_class.instance_count) — tier 판정용. None이면 모두 master.
        max_per_class: 클래스당 최대 경로 개수

    Returns:
        {cls_name: [{path, via, target, score, sparql_hint}, ...]}
        클래스당 최대 max_per_class 개.
    """

    result: dict[str, list[dict]] = {}

    # 클래스별 tier 판정 (abox_stats 기반, 없으면 default master)
    def _tier_of(cls_name: str) -> str:
        if not abox_stats:
            return "master"
        stats = abox_stats.get("per_class", {}).get(cls_name, {})
        inst_count = stats.get("instance_count", 0)
        if inst_count == 0:
            return "inferred"
        if inst_count > 500:
            return "transaction"
        return "master"

    def _op_fk_bonus(op_name: str | None) -> int:
        """FK 네이밍 패턴인지 점수화."""
        if not op_name:
            return 0
        if op_name.startswith("has") or op_name.startswith("is") or op_name.endswith("Of"):
            return 2
        return 0

    for src_name, src_info in classes_dict.items():
        candidates = []
        for op1 in src_info.get("object_properties_outgoing", []):
            p1 = op1.get("property")
            targets1 = op1.get("target", [])
            for mid_name in targets1:
                if mid_name == src_name:
                    continue  # immediate self-loop skip
                mid_info = classes_dict.get(mid_name, {})
                for op2 in mid_info.get("object_properties_outgoing", []):
                    p2 = op2.get("property")
                    targets2 = op2.get("target", [])
                    for tgt_name in targets2:
                        if tgt_name == src_name:
                            continue  # self-referential 2hop 제외
                        score = 0
                        tier = _tier_of(tgt_name)
                        if tier in ("master", "catalog"):
                            score += 3
                        elif tier == "transaction":
                            score += 1
                        elif tier == "inferred":
                            score -= 1
                        score += _op_fk_bonus(p1) + _op_fk_bonus(p2)
                        candidates.append({
                            "path": [p1, p2],
                            "via": mid_name,
                            "target": tgt_name,
                            "score": score,
                        })

        # 정렬 + dedup (같은 path+via+target 조합 제거) + top-N
        seen = set()
        unique = []
        for c in sorted(candidates, key=lambda x: (-x["score"], x["target"], x["via"])):
            key = (tuple(c["path"]), c["via"], c["target"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
            if len(unique) >= max_per_class:
                break

        # sparql_hint 추가
        for c in unique:
            c["sparql_hint"] = (
                f"?x a {NS_PREFIX}:{src_name} ; {NS_PREFIX}:{c['path'][0]} ?m . "
                f"?m {NS_PREFIX}:{c['path'][1]} ?t"
            )

        if unique:
            result[src_name] = unique

    return result


def _build_common_patterns(tbox, classes_dict, pfx):
    """T-Box 구조에서 공통 SPARQL 패턴을 동적 생성한다."""
    patterns = {}

    # 모든 클래스에 적용: 클래스별 인스턴스 수 조회
    patterns["class_distribution"] = {
        "description": "클래스별 인스턴스 수",
        "sparql": (
            f"SELECT ?class (COUNT(?x) AS ?count) WHERE {{ "
            f"?x a ?class . FILTER(STRSTARTS(STR(?class), \"{DOMAIN_NS}\")) "
            f"}} GROUP BY ?class ORDER BY DESC(?count)"
        ),
    }

    # dateTime 프로퍼티가 있는 클래스: 날짜 범위 필터
    _generated_date_pattern = False
    for cls_name, cls_info in classes_dict.items():
        if _generated_date_pattern:
            break
        for pname, pinfo in cls_info.get("datatype_properties", {}).items():
            if pinfo.get("range") == "xsd:dateTime":
                patterns["date_range_filter"] = {
                    "description": f"날짜 범위 필터 ({cls_name}.{pname})",
                    "sparql": (
                        f"SELECT ?x ?date WHERE {{ "
                        f"?x a {pfx}:{cls_name} ; {pfx}:{pname} ?date . "
                        f'FILTER(?date >= "2025-01-01T00:00:00"^^xsd:dateTime && '
                        f'?date < "2025-02-01T00:00:00"^^xsd:dateTime) }}'
                    ),
                }
                _generated_date_pattern = True
                break

    # "status" 프로퍼티가 있는 클래스: 상태별 카운트
    for cls_name, cls_info in classes_dict.items():
        dt_props = cls_info.get("datatype_properties", {})
        for pname in dt_props:
            if "status" in pname.lower():
                patterns["count_by_status"] = {
                    "description": f"{cls_info.get('label_ko', cls_name)} 상태별 카운트",
                    "sparql": (
                        f"SELECT ?status (COUNT(*) AS ?cnt) WHERE {{ "
                        f"?x a {pfx}:{cls_name} ; {pfx}:{pname} ?status "
                        f"}} GROUP BY ?status"
                    ),
                }
                break
        if "count_by_status" in patterns:
            break

    # ObjectProperty 관계 탐색
    for cls_name, cls_info in classes_dict.items():
        out_props = cls_info.get("object_properties_outgoing", [])
        if out_props:
            op = out_props[0]
            targets = op.get("target", [])
            target = targets[0] if targets else "Thing"
            patterns["entity_relationship"] = {
                "description": f"엔티티 관계 탐색 ({cls_name} → {target})",
                "sparql": (
                    f"SELECT ?x ?related WHERE {{ "
                    f"?x a {pfx}:{cls_name} ; {pfx}:{op['property']} ?related }}"
                ),
            }
            break

    # followedBy (또는 transitive 관계) 가 있으면 흐름 패턴
    for _cls_name, cls_info in classes_dict.items():
        for op in cls_info.get("object_properties_outgoing", []):
            if op["property"] in ("followedBy", "precedes", "hasNext"):
                patterns["process_flow"] = {
                    "description": "흐름/순서 관계",
                    # RDFLib용 SPARQL 예시이며 SQL executor에 전달되지 않는다.
                    "sparql": f"SELECT ?from ?to WHERE {{ ?from {pfx}:{op['property']} ?to }}",  # nosec B608
                }
                break
        if "process_flow" in patterns:
            break

    # decimal 프로퍼티 집계: 첫 번째 decimal 프로퍼티로 예시
    for cls_name, cls_info in classes_dict.items():
        dt_props = cls_info.get("datatype_properties", {})
        decimal_props = [p for p, i in dt_props.items() if i.get("range") == "xsd:decimal"]
        if len(decimal_props) >= 2:
            p1, p2 = decimal_props[0], decimal_props[1]
            patterns["numeric_aggregation"] = {
                "description": f"{cls_info.get('label_ko', cls_name)} 수치 집계",
                "sparql": (
                    f"SELECT (AVG(?v1) AS ?avg1) (SUM(?v2) AS ?sum2) WHERE {{ "
                    f"?x a {pfx}:{cls_name} ; {pfx}:{p1} ?v1 ; {pfx}:{p2} ?v2 }}"
                ),
            }
            break

    # 야간 시간대 필터 (dateTime 프로퍼티가 있으면)
    for cls_name, cls_info in classes_dict.items():
        dt_props = cls_info.get("datatype_properties", {})
        for pname, pinfo in dt_props.items():
            if pinfo.get("range") == "xsd:dateTime" and "timestamp" in pname.lower():
                patterns["night_shift_filter"] = {
                    "description": "야간 시간대 필터 (HOUR 대체)",
                    "sparql": (
                        f"SELECT ?x ?ts WHERE {{ "
                        f"?x a {pfx}:{cls_name} ; {pfx}:{pname} ?ts . "
                        f'FILTER(STRAFTER(STR(?ts),"T")>="22:00:00" || '
                        f'STRAFTER(STR(?ts),"T")<"06:00:00") }} LIMIT 20'
                    ),
                }
                break
        if "night_shift_filter" in patterns:
            break

    # 조건부 카운트 (status/string enum 프로퍼티)
    for cls_name, cls_info in classes_dict.items():
        dt_props = cls_info.get("datatype_properties", {})
        for pname, pinfo in dt_props.items():
            distinct = pinfo.get("distinct_values", [])
            if distinct and 2 <= len(distinct) <= 10 and pinfo.get("range") == "xsd:string":
                val = distinct[0]
                patterns["conditional_count"] = {
                    "description": "조건부 카운트",
                    "sparql": (
                        f"SELECT (COUNT(*) AS ?total) "
                        f"(SUM(?flag) AS ?matched) WHERE {{ "
                        f"?x a {pfx}:{cls_name} ; {pfx}:{pname} ?v . "
                        f'BIND(IF(?v="{val}",1,0) AS ?flag) }}'
                    ),
                }
                break
        if "conditional_count" in patterns:
            break

    # 2홉 경로 예시 (D1) — 가장 많이 쓰이는 경로 하나를 샘플
    for cls_name, cls_info in classes_dict.items():
        two_hop = cls_info.get("object_properties_2hop", [])
        if two_hop:
            top = two_hop[0]
            patterns["two_hop_path"] = {
                "description": (
                    f"2홉 경로 예시 ({cls_name} → {top['via']} → {top['target']})"
                ),
                "sparql": (
                    f"SELECT ?x ?m ?t WHERE {{ "
                    f"?x a {pfx}:{cls_name} ; {pfx}:{top['path'][0]} ?m . "
                    f"?m {pfx}:{top['path'][1]} ?t }}"
                ),
            }
            break

    # 범위 기반 FILTER 가이드 (D4) — 실제 데이터 분포 밖 값은 0건 리턴 위험.
    # value_stats 에 p10/p90 이 있는 numeric DP 중 "가장 대표적" 인 것 선정:
    # 1) p10 < p90 (degenerate 제외), 2) count 최대 (분포 가장 풍부).
    # 부동소수점 표시는 반올림 6자리로 가독성 개선 — rdflib/Oxigraph 모두
    # float literal 로 안전 파싱.
    _best_dp: tuple | None = None  # (count, cls_name, pname, vs)
    for cls_name, cls_info in classes_dict.items():
        dt_props = cls_info.get("datatype_properties", {})
        for pname, pinfo in dt_props.items():
            vs = pinfo.get("value_stats") or {}
            if "p10" not in vs or "p90" not in vs:
                continue
            if vs["p10"] >= vs["p90"]:
                continue  # degenerate (상수 DP) 제외
            count = vs.get("count", 0)
            if _best_dp is None or count > _best_dp[0]:
                _best_dp = (count, cls_name, pname, vs)

    if _best_dp is not None:
        _, cls_name, pname, vs = _best_dp
        p10 = round(vs["p10"], 6)
        p90 = round(vs["p90"], 6)
        vmin = round(vs["min"], 6) if isinstance(vs["min"], float) else vs["min"]
        vmax = round(vs["max"], 6) if isinstance(vs["max"], float) else vs["max"]
        patterns["value_range_check"] = {
            "description": f"범위 내 필터 예시 ({cls_name}.{pname})",
            "sparql": (
                f"SELECT ?x ?v WHERE {{ "
                f"?x a {pfx}:{cls_name} ; {pfx}:{pname} ?v . "
                f"FILTER(?v >= {p10} && ?v <= {p90}) }}"
            ),
            "note": (
                f"{pname} 실측 범위: [{vmin}, {vmax}] (count={vs.get('count', 0)}). "
                f"전체 80% 가 [{p10}, {p90}] 내에 분포. "
                f"이 밖의 값으로 FILTER 시 0건 반환 가능 — 'no data' vs "
                f"'out of range' 구분 필수."
            ),
        }

    return patterns


def _build_question_templates(tbox, classes_dict):
    """질문 유형별 SPARQL 패턴 템플릿을 T-Box 구조에서 동적 생성한다.

    T-Box 클래스의 한국어 레이블, DatatypeProperty, ObjectProperty 관계를 분석하여
    질문 유형을 자동 분류한다.

    Args:
        tbox: T-Box rdflib Graph
        classes_dict: 클래스 딕셔너리 (이미 구성된 것)
    """
    templates = {}

    # 각 클래스를 기능 카테고리별로 분류 (T-Box 분석 기반)
    # 분류 전략: 프로퍼티 이름 패턴 + 클래스 이름 패턴 + 한국어 레이블
    for cls_name, cls_info in classes_dict.items():
        # 질의 진입점이 확정 0행이면 템플릿을 만들지 않는다. 실측 (2026-08-28):
        # 63개 중 **15개**가 인스턴스 0 클래스를 required_classes 로 지목했고 그중
        # 11개는 T-Box 에서 owl:deprecated 였다. "납기 지연 발주 몇 건?" 같은 질문에
        # LLM 이 DelayedPurchaseOrder 를 가장 먼저 고르고 확정적으로 0행을 받는다.
        #
        # v1(vocabulary contract)은 A-Box 전이라 instance_count 가 없다 — 그때는
        # 거르지 않는다(키 부재를 0 으로 읽으면 전 클래스가 탈락한다).
        if cls_info.get("deprecated"):
            continue
        if "instance_count" in cls_info and not cls_info["instance_count"]:
            continue
        label_ko = cls_info.get("label_ko", "")
        dt_props = cls_info.get("datatype_properties", {})
        # ⚠️ **set 을 쓰지 말 것.** 예전에는 ``set(dt_props.keys())`` 였고, 그 set 이
        # 아래 ``fk_props[0]`` / ``quantity_props[:5]`` 로 흘러 실행마다 **다른 값을
        # 골랐다**. PYTHONHASHSEED 5회 재현 (2026-08-30):
        #
        #   대기 배출 모니터링.key_join : airEmissionMonitoringStackId ↔ …MonitorId
        #
        # 이 둘은 순서 차이가 아니라 **의미가 다르다** — StackId 는 배출구(FK),
        # MonitorId 는 측정기 자기 식별자다. 조인 힌트가 실행마다 FK↔PK 로 뒤바뀌었다.
        # 딕셔너리는 ``read_semantic_dictionary`` 가 원문 그대로 LLM 컨텍스트로 넘긴다.
        #
        # 정렬로 고정한다 — 어느 후보가 "옳은" FK 인지는 CSV 실측이 필요한 별개
        # 문제이고, 근거 없이 한쪽으로 고정하면 "결정적으로 틀린" 힌트가 되어 진동보다
        # 나쁠 수 있다. 여기서는 **재현 가능성**만 회복한다.
        prop_names = sorted(dt_props.keys())
        out_ops = cls_info.get("object_properties_outgoing", [])

        # -- "상태" 관련 클래스: status 프로퍼티 보유 --
        status_props = [p for p in prop_names if "status" in p.lower()]
        if status_props:
            key = f"{label_ko or cls_name}_상태"
            templates.setdefault(key, {
                "description": f"{label_ko or cls_name} 상태 분석",
                "keywords": [label_ko, "상태", "현황", "status"] if label_ko else ["상태", "현황"],
                "required_classes": [cls_name],
                "key_pattern": "SUM(IF(조건,1,0))/COUNT(*)",
            })

        # -- "수량/생산" 관련 클래스: quantity/rate/yield 프로퍼티 --
        quantity_props = [p for p in prop_names if any(
            kw in p.lower() for kw in ("quantity", "rate", "yield", "amount")
        )]
        if quantity_props:
            key = f"{label_ko or cls_name}_수량"
            props_str = ",".join(quantity_props[:5])
            templates.setdefault(key, {
                "description": f"{label_ko or cls_name} 수량/비율 분석",
                "keywords": ([label_ko] if label_ko else []) + ["수량", "생산량", "달성률"],
                "required_classes": [cls_name],
                "key_properties": props_str,
            })

        # -- "에너지" 관련 클래스: energy/efficiency/consumption 이름 패턴 --
        if any(kw in cls_name.lower() for kw in ("energy", "consumption", "fuel", "emission")):
            key = f"{label_ko or cls_name}"
            fk_props = [p for p in prop_names if p.endswith("Id") or p.endswith("Code")]
            templates.setdefault(key, {
                "description": f"{label_ko or cls_name} 분석",
                "keywords": ([label_ko] if label_ko else []) + ["에너지", "소비"],
                "required_classes": [cls_name],
                **({"key_join": fk_props[0]} if fk_props else {}),
            })

        # -- "품질" 관련 클래스: quality/analysis/properties 이름 패턴 --
        if any(kw in cls_name.lower() for kw in ("quality", "analysis", "mechanical", "chemical", "ndt")):
            key = f"{label_ko or cls_name}"
            fk_props = [p for p in prop_names if p.endswith("Id") or p.endswith("Code")]
            templates.setdefault(key, {
                "description": f"{label_ko or cls_name} 분석",
                "keywords": ([label_ko] if label_ko else []) + ["품질"],
                "required_classes": [cls_name],
                **({"join_key": fk_props[0]} if fk_props else {}),
            })

        # -- 공정 흐름: followedBy/precedes OP를 가진 클래스 --
        flow_ops = [op for op in out_ops if op["property"] in ("followedBy", "precedes", "hasNext")]
        if flow_ops:
            templates.setdefault("공정_흐름", {
                "description": "공정 흐름/순서",
                "keywords": ["공정", "흐름", "순서"],
                "flow_property": flow_ops[0]["property"],
            })

    # -- 시간 관련 범용 템플릿 (dateTime 프로퍼티가 존재하면 추가) --
    has_datetime = False
    for cls_info in classes_dict.values():
        for pinfo in cls_info.get("datatype_properties", {}).values():
            if pinfo.get("range") == "xsd:dateTime":
                has_datetime = True
                break
        if has_datetime:
            break

    if has_datetime:
        templates.setdefault("야간_분석", {
            "description": "야간 시간대 데이터",
            "keywords": ["야간", "밤", "심야"],
            "method": "STRAFTER(STR(?ts),'T') 문자열 비교",
        })
        templates.setdefault("주차별_비교", {
            "description": "주차별 비교",
            "keywords": ["주차별", "첫째주", "마지막주"],
            "method": "BIND(IF(날짜범위)) 패턴",
        })

    return templates


def _build_process_flow(tbox, classes_dict):
    """공정 흐름 정보를 T-Box의 followedBy/precedes ObjectProperty에서 동적 생성한다.

    T-Box에 followedBy/precedes 같은 순서 관계가 없으면 빈 딕셔너리를 반환한다.

    Args:
        tbox: T-Box rdflib Graph
        classes_dict: 클래스 딕셔너리 (이미 구성된 것)
    """
    # T-Box에서 순서 관계 OP 탐지
    # 1차: 명시적 이름 매칭
    # 2차: TransitiveProperty 중 흐름 관련 키워드 포함
    flow_prop_name = None
    flow_prop_uri = None
    inverse_prop_name = None
    is_transitive = False

    _FLOW_EXACT_NAMES = {"followedBy", "precedes", "hasNext", "comesAfter"}
    _FLOW_KEYWORDS = {"follow", "next", "sequence", "step", "precede", "chain"}

    for s in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if not str(s).startswith(DOMAIN_NS):
            continue
        pname = _local_name(s)
        if pname in _FLOW_EXACT_NAMES:
            flow_prop_name = pname
            flow_prop_uri = s
            is_transitive = (s, RDF.type, OWL.TransitiveProperty) in tbox
            for _, _, inv in tbox.triples((s, OWL.inverseOf, None)):
                if isinstance(inv, URIRef):
                    inverse_prop_name = _local_name(inv)
            break

    # 2차: TransitiveProperty 중 흐름 관련 키워드 매칭
    if not flow_prop_name:
        for s in tbox.subjects(RDF.type, OWL.TransitiveProperty):
            if not str(s).startswith(DOMAIN_NS):
                continue
            pname = _local_name(s)
            pname_lower = pname.lower()
            if any(kw in pname_lower for kw in _FLOW_KEYWORDS):
                flow_prop_name = pname
                flow_prop_uri = s
                is_transitive = True
                for _, _, inv in tbox.triples((s, OWL.inverseOf, None)):
                    if isinstance(inv, URIRef):
                        inverse_prop_name = _local_name(inv)
                break

    if not flow_prop_name:
        return {}

    # 해당 OP의 domain/range 클래스 분석 → 공정 관련 클래스 찾기
    flow_classes = set()
    for _, _, d in tbox.triples((flow_prop_uri, RDFS.domain, None)):
        if isinstance(d, URIRef) and str(d).startswith(DOMAIN_NS):
            flow_classes.add(_local_name(d))
    for _, _, r in tbox.triples((flow_prop_uri, RDFS.range, None)):
        if isinstance(r, URIRef) and str(r).startswith(DOMAIN_NS):
            flow_classes.add(_local_name(r))

    # domain이 owl:Thing이면 T-Box의 모든 "Process" 포함 클래스를 수집
    if not flow_classes or "Thing" in flow_classes:
        flow_classes = {
            name for name in classes_dict
            if "process" in name.lower() or "step" in name.lower()
        }

    # 시퀀스 구성: 클래스별 label_ko 포함
    sequence = []
    for i, cname in enumerate(sorted(flow_classes), start=1):
        cls_info = classes_dict.get(cname, {})
        entry = {
            "step": i,
            "class": cname,
            "label_ko": cls_info.get("label_ko", cname),
        }
        sequence.append(entry)

    description_parts = [f"{flow_prop_name} 기반 공정/순서 흐름"]
    if is_transitive:
        description_parts.append(f"{flow_prop_name}는 TransitiveProperty")
    if inverse_prop_name:
        description_parts.append(f"{inverse_prop_name}는 inverseOf")

    return {
        "description": ". ".join(description_parts) + ".",
        "flow_property": flow_prop_name,
        **({"inverse_property": inverse_prop_name} if inverse_prop_name else {}),
        "transitive": is_transitive,
        "sequence": sequence,
    }


def _enrich_metadata(metadata: dict, *, is_being_written: bool = False) -> dict:
    """추론/LPG 변환 후 메타데이터를 최신 상태로 동기화한다.

    - inference loss manifest에서 보존률 반영
    - LPG semantic dictionary 존재 여부 + 통계 반영
    - T-Box/Inferred 대비 staleness 감지

    ``is_being_written``: 호출자가 **지금 이 딕셔너리를 저장하려는 중** 인가.

    2026-08-30 실측: 이 함수의 **유일한 배포 호출자는 생성 경로**
    (``generate_semantic_dictionary``) 다. ``read_semantic_dictionary`` 는 파일을
    그대로 반환하고 이 함수를 부르지 않는다. 즉 예전 구현의 mtime 비교는 **항상**
    잘못된 문맥에서 돌았고, 조회 분기에는 도달하는 배포 호출자가 없다:

    ==================================== ================================
    호출자                                 디스크의 딕셔너리 파일
    ==================================== ================================
    ``generate_semantic_dictionary`` (생성) **직전 세대** → mtime 무의미
    (조회 문맥 — 현재 배포 호출자 없음)      검사 대상 그 자체 → mtime 유효
    ==================================== ================================

    조회 분기를 남겨 두는 이유: 딕셔너리 신선도를 판정하려는 소비자가 생기면
    (도구 응답에 실으려는 등) 이 함수를 부를 것이고, 그때 필요한 계약이다. 지금은
    테스트만 그 경로에 도달한다 — ``tests/test_dict_sync.py::TestStaleness``.

    생성 문맥에서 파일 mtime 으로 신선도를 재면 **자기 이전 세대**를 재게 된다.
    2026-08-30 실측: S10 이 11:06:40 에 쓴 딕셔너리에 ``stale: true`` /
    ``tbox_newer_than_dict: false`` 가 각인됐다. 그 값은 S6.5 가 10:47:41 에 쓴
    v1 파일을 ``all_inferred.ttl`` (10:57:44) 과 비교한 결과다 — 갓 만든 산출물에
    "낡았다" 가 찍혔고, 기준선 실행에서는 우연히 반대로 찍혀 두 실행의 값이 서로
    반대 방향으로 틀렸다.

    이 리포에는 같은 형태가 반복 기록돼 있다: 게이트가 삭제 스텝 앞에서 재서
    배포되지 않는 중간 상태를 측정했고 (``step_22f``), guard 가 S2 초안과 S3
    완료본을 비교해 48분을 폐기했다. 측정은 자기가 판정하려는 **대상**을 재야 한다.

    그래서 생성 문맥에서는 mtime 비교를 하지 않고, 대신 이 딕셔너리가 **어느
    세대의 입력으로 만들어졌는지** 를 각인한다 (``source_mtimes``). 조회 시점에
    그 값을 현재 파일 mtime 과 비교하면 신선도를 정확히 알 수 있다 — 각인이
    없던 예전에는 조회 쪽도 "딕셔너리 파일 자신의 mtime" 이라는 간접 신호에
    의존했다.
    """

    # 추론 그래프 통계
    if os.path.exists(INFERRED_PATH):
        try:
            metadata["inferred_file_size_bytes"] = os.path.getsize(INFERRED_PATH)
            inf_manifest_path = os.path.join(
                os.path.dirname(INFERRED_PATH), "inference_loss_manifest.json"
            )
            if os.path.exists(inf_manifest_path):
                with open(inf_manifest_path, encoding="utf-8") as f:
                    inf_lm = json.load(f)
                # 매니페스트 값을 검증 없이 복사하면 안 된다. 이 필드는 "추론이 의미
                # 있는 트리플을 얼마나 보존했나" 라는 품질 주장인데, 딕셔너리의 소비자는
                # 코드가 아니라 **LLM** 이므로 허구값이 사실로 전달되고 어떤 게이트도
                # 대조하지 않는다. 실측 (2026-09-01): 테스트가 배포 매니페스트를
                # ``{tbox: 4922, abox: -4921}`` 로 덮은 상태였다 — 산술이 성립하지
                # 않는 값이다.
                from tools.inference import manifest_arithmetic_error
                if manifest_arithmetic_error(
                    inf_lm.get("input") or {}, inf_lm.get("output") or {},
                ):
                    logger.warning(
                        "inference_loss_manifest 값이 실행에서 나올 수 없다 — "
                        "inference_preservation_rate 를 기록하지 않는다",
                    )
                else:
                    metadata["inference_preservation_rate"] = (
                        inf_lm.get("preservation_score", {})
                        .get("meaningful_triples_ratio")
                    )
        except Exception:  # noqa: BLE001 — 선택적 보존율 메타데이터가 깨지면 해당 값만 생략한다
            pass

    # LPG 변환 상태
    if os.path.exists(LPG_SEMANTIC_DICT_PATH):
        metadata["lpg_dictionary_available"] = True
        try:
            with open(LPG_SEMANTIC_DICT_PATH, encoding="utf-8") as f:
                lpg_meta = json.load(f).get("metadata", {})
            metadata["lpg_nodes"] = lpg_meta.get("total_nodes", 0)
            metadata["lpg_relationships"] = lpg_meta.get("total_relationships", 0)
        except Exception:  # noqa: BLE001 — 선택적 LPG 통계가 깨지면 카운트만 생략한다
            pass
    else:
        metadata["lpg_dictionary_available"] = False

    # Staleness 감지 — 무엇을 기준으로 재는가가 이 블록의 전부다 (docstring 참조).
    try:
        if is_being_written:
            # 생성 문맥: 디스크의 파일은 직전 세대다. 신선도를 판정하는 대신
            # **이 딕셔너리가 읽은 입력의 세대**를 각인해, 이후 조회가 정확히
            # 판정할 수 있게 한다.
            source_mtimes: dict[str, float] = {}
            for key, path in (("inferred", INFERRED_PATH), ("tbox", TBOX_PATH)):
                if os.path.exists(path):
                    source_mtimes[key] = os.path.getmtime(path)
            if source_mtimes:
                metadata["source_mtimes"] = source_mtimes
            # 갓 생성된 산출물은 정의상 낡지 않았다. 이 두 필드를 생략하지 않고
            # 명시하는 이유: 소비자(LLM)가 키 부재를 "판정 실패" 가 아니라
            # "정상" 으로 오독하는 함정이 이 리포에 기록돼 있다 (필드 부재 ≠ 값 0).
            metadata["stale"] = False
            metadata["tbox_newer_than_dict"] = False
        elif os.path.exists(SEMANTIC_DICT_PATH):
            # 조회 문맥: 파일이 곧 검사 대상이다. 생성 때 각인된 입력 세대가 있으면
            # 그것과 비교한다 (파일 mtime 은 재작성·복사로 흔들리는 간접 신호다).
            recorded = metadata.get("source_mtimes") or {}
            dict_mtime = os.path.getmtime(SEMANTIC_DICT_PATH)
            if os.path.exists(INFERRED_PATH):
                base = recorded.get("inferred", dict_mtime)
                metadata["stale"] = os.path.getmtime(INFERRED_PATH) > base
            if os.path.exists(TBOX_PATH):
                base = recorded.get("tbox", dict_mtime)
                metadata["tbox_newer_than_dict"] = os.path.getmtime(TBOX_PATH) > base
    except Exception as exc:
        logger.warning(
            "시맨틱 딕셔너리 신선도 판정 실패; stale 플래그를 갱신하지 않는다: %s",
            exc,
        )

    return metadata


# ── MCP 도구 ──────────────────────────────────


def _load_tbox_and_abox(use_inferred: bool, include_stats: bool):
    """T-Box + A-Box 로드 (v1: 빈 abox / v2: 정상 abox).

    Returns: (tbox, abox) 또는 (None, error_response_str) — 실패 시 두 번째가 str.
    """
    if include_stats:
        try:
            tbox, abox = _load_ontology_data(use_inferred=use_inferred)
            return tbox, abox
        except FileNotFoundError as e:
            return None, json.dumps(
                {"success": False, "error": str(e),
                 "message": f"T-Box 로드 실패: {TBOX_PATH}"},
                ensure_ascii=False,
            )
        except Exception as e:
            return None, json.dumps(
                {"success": False, "error": str(e), "message": "T-Box 로드 실패"},
                ensure_ascii=False,
            )

    # v1 모드 — T-Box 만 로드, A-Box 는 빈 그래프
    try:
        with open(TBOX_PATH, encoding="utf-8") as f:
            t_box_content = f.read()
        tbox = _new_graph()
        tbox.parse(data=t_box_content, format="turtle")
        return tbox, _new_graph()
    except FileNotFoundError as e:
        return None, json.dumps(
            {"success": False, "error": str(e),
             "message": f"T-Box 로드 실패: {TBOX_PATH}"},
            ensure_ascii=False,
        )
    except Exception as e:
        return None, json.dumps(
            {"success": False, "error": str(e),
             "message": "T-Box 로드 실패 (v1 mode)"},
            ensure_ascii=False,
        )


def _abox_only_op_counts(props: dict) -> dict | None:
    """**추론 전** 그래프에서 OP 트리플을 센다 → ``{op_name: count}``.

    ``use_inferred=True`` 로 만든 딕셔너리의 ``triple_count`` 는 추론 파생분을
    포함한다. 그 값만으로는 "추론 없이도 있는 관계" 와 "추론에만 있는 관계" 를
    구분할 수 없고, 후자를 추론 없이 질의하면 0행이다.

    ## 무엇을 "추론 전" 으로 볼 것인가 (2026-08-30)

    ``a_box.ttl`` **만** 읽으면 안 된다 — 실제 질의 경로
    (:func:`domain.tbox_utils.load_graph`) 는 **A-Box + master_data + tacit** 을
    합치고, 그것이 SPARQL 사용자가 보는 그래프다. ``a_box.ttl`` 만 세면 tacit 이
    채우는 관계가 0으로 잡혀 정상 관계에 ``requires_inferred_graph`` 를 잘못
    붙인다 (실측: ``hasBlastFurnaceEquipment`` 는 a_box 4,320 + tacit 4,320).

    실패하면 ``None`` — 비교를 건너뛴다 (없는 정보를 0으로 단정하면 같은 오탐이
    생긴다).
    """
    try:
        from domain.tbox_utils import load_graph

        graph, _tacit_files = load_graph(use_inferred=False)
        names = {op["name"] for op in props.get("obj_props", [])}
        _ic, _pv, counts = _single_pass_abox_stats(
            graph, str(DOMAIN_NS), {}, names,
        )
        return dict(counts)
    except Exception as exc:  # noqa: BLE001 — 비교는 부가 정보다
        logger.warning(
            "추론 전 OP 카운트 수집 실패 — 추론/비추론 구분 표시를 생략한다: %s",
            exc,
        )
        return None


def _build_metadata(
    tbox, abox, include_stats: bool, use_inferred: bool = False,
) -> dict:
    """딕셔너리 metadata 블록 빌드 (네임스페이스 + 버전 플래그).

    ## ``counts_source`` 가 왜 필요한가 (2026-08-30 실측)

    ``use_inferred`` 는 ``triple_count`` / ``instance_count`` / ``is_populated`` 의
    **의미를 바꾸는데** 산출물에는 아무 기록이 없었다. 결과: 배포 딕셔너리가 OP
    108개를 ``is_populated: true`` 로 보고했지만 ``a_box.ttl`` 기준으로는 55개다.
    차이 53개는 추론이 ``owl:inverseOf`` 로 파생한 역방향 링크다::

        equipmentHasGasEnergy   triple_count: 720   ← 추론 그래프
                                a_box.ttl        : 0   ← LLM 이 질의하는 곳

    딕셔너리의 소비자는 코드가 아니라 **LLM** 이다. 어느 그래프에 그 트리플이
    있는지 모르면 ``a_box.ttl`` 로 질의해 0행을 받고, 그 0행이 오류 없이
    "정답처럼" 반환된다 — 이 리포가 반복해서 겪은 실패다.

    그래서 ``counts_source`` 로 **어느 그래프를 셌는지** 각인하고,
    ``inferred_only_properties`` 로 "추론 그래프에만 있는 OP" 를 열거한다.
    """
    from domain.namespaces import IOF_CORE, IOF_MAINT, NS_INST_PREFIX

    inst_pfx = NS_INST_PREFIX
    pfx = NS_PREFIX
    domain_info = DOMAIN_CONFIG.get("domain", {})
    return {
        "version": "2.0",
        "stats_included": include_stats,
        "dictionary_stage": "v2_full" if include_stats else "v1_vocabulary_contract",
        # 통계가 어느 그래프에서 나왔는가 — 소비자가 질의 대상을 고르는 근거.
        "counts_source": (
            "inferred" if (include_stats and use_inferred)
            else "abox" if include_stats else "none"
        ),
        "counts_source_file": (
            os.path.basename(INFERRED_PATH) if (include_stats and use_inferred)
            else os.path.basename(ABOX_PATH) if include_stats else None
        ),
        "counts_source_note": (
            "triple_count / instance_count / is_populated 는 all_inferred.ttl "
            "기준이다. a_box.ttl 만 로드하면 추론 파생 트리플이 없으므로 "
            "inferred_only_properties 에 열거된 프로퍼티는 0행을 반환한다."
            if (include_stats and use_inferred)
            else "triple_count / instance_count / is_populated 는 a_box.ttl "
            "기준이다 (추론 파생분 미포함)."
            if include_stats
            else "v1 vocabulary contract — 통계 없음."
        ),
        "domain": domain_info.get("name", ""),
        "domain_ko": domain_info.get("name_ko", ""),
        "ontology_namespace": DOMAIN_NS,
        "instance_namespace": DOMAIN_INST_NS,
        "prefixes": {
            pfx: DOMAIN_NS,
            inst_pfx: DOMAIN_INST_NS,
            "iof-core": IOF_CORE,
            "iof-maint": IOF_MAINT,
            "xsd": str(XSD),
            "owl": str(OWL),
            "rdfs": str(RDFS),
            "rdf": str(RDF),
        },
        "instance_uri_pattern": f"{inst_pfx}:{{ClassName}}_{{NNNN}}",
        "tbox_triples": len(tbox),
        "abox_triples": len(abox),
        "all_dates_are_xsd_dateTime": True,
    }


def _translate_missing_descriptions_en(classes_dict: dict) -> int:
    """description_ko 만 있는 클래스에 description_en 을 LLM 번역으로 보강.

    Bedrock 호출 실패 시 warn + 기존 한국어 description 유지. 본 함수는 부수
    효과 (classes_dict mutate) — 호출자는 반환값 (번역 성공 클래스 수) 만 참조.
    """
    missing_en = {
        cls: info["description_ko"]
        for cls, info in classes_dict.items()
        if info.get("description_ko") and not info.get("description_en")
    }
    if not missing_en:
        return 0
    try:
        from tools.bedrock import invoke_bedrock_text
        ko_list = "\n".join(f"- {cls}: {desc}" for cls, desc in missing_en.items())
        prompt = (
            "아래 한국어 온톨로지 클래스 설명을 영어로 번역하세요.\n"
            "JSON 형식으로 출력: {\"ClassName\": \"English description\", ...}\n"
            "클래스명은 그대로 유지하세요.\n\n" + ko_list
        )
        resp = invoke_bedrock_text(prompt, max_tokens=4096, temperature=0)
        import re as _re
        json_match = _re.search(r"\{[\s\S]*\}", resp)
        if not json_match:
            return 0
        en_descs = json.loads(json_match.group())
        translated = 0
        for cls, en_desc in en_descs.items():
            if cls in classes_dict:
                classes_dict[cls]["description_en"] = en_desc
                translated += 1
        logger.info("description_en 생성: %d/%d 클래스", translated, len(missing_en))
        return translated
    except Exception as e:
        logger.warning("description_en 자동 생성 실패 (Bedrock): %s", e)
        return 0


def _save_dictionary(dictionary: dict) -> None:
    """딕셔너리를 SEMANTIC_DICT_PATH 에 JSON 직렬화 저장."""
    dict_json = json.dumps(dictionary, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(SEMANTIC_DICT_PATH), exist_ok=True)
    with open(SEMANTIC_DICT_PATH, "w", encoding="utf-8") as f:
        f.write(dict_json)


def _build_success_response(
    classes_dict: dict, obj_props_dict: dict, func_props: dict,
    sparql_guide: dict, question_templates: dict, inference_classes: dict,
    include_stats: bool,
) -> str:
    """성공 응답 JSON 빌드 (v1/v2 라벨 + 통계)."""
    statistics = {
        "classes": len(classes_dict),
        "object_properties": len(obj_props_dict),
        "functional_properties": len(func_props),
        "datatype_property_entries": sum(
            len(c.get("datatype_properties", {})) for c in classes_dict.values()
        ),
        "anti_patterns": len(sparql_guide["anti_patterns"]),
        "common_patterns": len(sparql_guide["common_patterns"]),
        "question_templates": len(question_templates),
        "inference_classes": len(inference_classes),
    }
    version = "v2" if include_stats else "v1"
    message = (
        "시맨틱 딕셔너리 v2 생성 완료 (T-Box 구조 + A-Box 통계)"
        if include_stats
        else "시맨틱 딕셔너리 v1 생성 완료 (T-Box 구조만, A-Box 생성용 vocabulary contract)"
    )
    return json.dumps(
        {
            "success": True,
            "version": version,
            "message": message,
            "path": SEMANTIC_DICT_PATH,
            "statistics": statistics,
        },
        ensure_ascii=False,
        indent=2,
    )


def generate_semantic_dictionary(
    use_inferred: bool = False,
    include_stats: bool = True,
) -> str:
    """T-Box/A-Box를 분석하여 시맨틱 딕셔너리 JSON을 생성하고 로컬 파일에 저장한다.

    로컬 T-Box(TBOX_PATH)와 A-Box(또는 추론 결과)를 읽어
    클래스·프로퍼티·통계·SPARQL 가이드·질문 템플릿을 포함하는
    시맨틱 딕셔너리를 생성하여 SEMANTIC_DICT_PATH에 저장한다.

    Bedrock 호출: 한국어 설명(``description_ko``)만 있고 영문 설명(``description_en``)이
    없는 클래스가 하나라도 있으면, ``include_stats`` 값과 관계없이 Amazon Bedrock 에 번역
    요청 1건을 보내 영문 설명을 채운다 (오류가 나면 ``tools.bedrock`` 의 재시도 규칙에
    따라 같은 요청을 다시 보낼 수 있다). 요청에는 해당 클래스의 이름과 한국어 설명이 담겨 모델로 전송되고
    모델 호출 비용이 발생한다. 모든 클래스에 영문 설명이 있으면 호출하지 않는다.
    호출이 실패하면 경고를 남기고 한국어 설명만 유지한다.

    Args:
        use_inferred: True면 all_inferred.ttl 기반으로 생성.
            추론 클래스(RunningEquipmentStatus 등)의 instance_count가 정확해짐.
            False면 기존 동작 (a_box.ttl 기반).
        include_stats: True (default) 면 A-Box 통계 포함 — v2 완전본.
            False 면 T-Box 구조만 — **v1 "vocabulary contract"** (기능 단계).
            v1 은 A-Box 생성 **전** 단계 (S6.5) 에서 A-Box 가 참조할 DP 이름
            목록을 확정하는 용도. instance_count / distinct_values / value_stats
            등 통계 필드가 모두 비어있거나 0.

    Returns:
        JSON string with {success, message, path, statistics, version ('v1'|'v2')}.
    """
    try:
        # 1. T-Box / A-Box 로드 (v1: empty abox)
        tbox, abox_or_err = _load_tbox_and_abox(use_inferred, include_stats)
        if tbox is None:
            return abox_or_err  # type: ignore[return-value]
        abox = abox_or_err

        # 2. 클래스 + 프로퍼티 + 통계
        steel_classes, subclass_map = _extract_classes(tbox)
        props = _extract_properties(tbox, abox)
        # 추론 그래프로 셌다면 A-Box 카운트를 따로 구해 두 값을 비교한다 —
        # "추론에만 있는 관계" 를 표시해야 소비자가 질의 대상을 고를 수 있다.
        abox_op_counts = (
            _abox_only_op_counts(props) if (include_stats and use_inferred) else None
        )
        classes_dict, obj_props_dict, func_props, class_quick_ref = (
            _compute_class_statistics(
                abox, tbox, steel_classes, subclass_map, props,
                include_stats=include_stats,
                abox_op_counts=abox_op_counts,
            )
        )

        # 3. 2홉 경로 — _build_sparql_guide 가 참조하므로 classes_dict 에 먼저 병합
        abox_stats_for_tier = None
        try:
            abox_stats_path = os.path.join(
                os.path.dirname(ABOX_PATH), "abox_stats.json",
            )
            if os.path.exists(abox_stats_path):
                with open(abox_stats_path, encoding="utf-8") as f:
                    abox_stats_for_tier = json.load(f)
        except Exception as e:
            logger.debug(
                "abox_stats.json 로드 실패, 2홉 tier는 default (master): %s", e,
            )
        two_hop_paths = _compute_two_hop_paths(
            classes_dict, abox_stats=abox_stats_for_tier,
        )
        for cls_name, paths in two_hop_paths.items():
            if cls_name in classes_dict:
                classes_dict[cls_name]["object_properties_2hop"] = paths

        # 4. SPARQL 가이드 + 동적 보조 정보
        # canonical / business_rules 를 가이드보다 먼저 만들어야 MUST_READ_FIRST
        # 포인터를 심을 수 있다.
        canonical = _load_canonical_designations()
        # 컬럼 → 클래스 역색인. "이 컬럼이 KG 에 있는가, 있으면 어디에" 조회용이며
        # SME 규칙의 컬럼 참조를 현재 DP 로 재해석하는 근거이기도 하다.
        column_index = _build_column_to_classes_index(classes_dict)
        business_rules = _build_business_rules(classes_dict, column_index)
        sparql_guide, inference_classes = _build_sparql_guide(
            classes_dict, tbox, props["obj_props"], canonical, business_rules,
        )
        class_names = {_local_name(c) for c in steel_classes}
        common_mistakes = _build_common_mistakes(tbox, class_names)
        question_templates = _build_question_templates(tbox, classes_dict)
        process_flow = _build_process_flow(tbox, classes_dict)

        # 5. 딕셔너리 조립 + 메타 enrich
        dictionary = {
            "metadata": _build_metadata(
                tbox, abox, include_stats, use_inferred=use_inferred,
            ),
            "sparql_guide": sparql_guide,
            "process_flow": process_flow,
            "common_mistakes": common_mistakes,
            "column_to_classes": column_index,
            "class_quick_reference": class_quick_ref,
            "question_templates": question_templates,
            "classes": classes_dict,
            "object_properties": obj_props_dict,
            "functional_properties": func_props,
        }
        # SME 업무판정 규칙 (판정식 / 파생계산 / 구어체 용어 매핑). 규칙 파일이
        # 없는 도메인에서는 빈 dict 라 섹션이 생기지 않는다.
        if business_rules:
            dictionary["business_rules"] = business_rules
        # SME-verified canonical designations (which class/column/date-axis is
        # authoritative when several candidates exist). Omitted when unset so the
        # dictionary stays domain-neutral.
        if canonical:
            # DP 이름은 S2 재생성마다 바뀌므로 CSV 컬럼 기준으로 현재 이름을
            # 재해석한다. 그러지 않으면 규칙 파일의 property 참조가 옛 이름을
            # 가리켜 규칙이 사실상 무효가 된다.
            dictionary["canonical_designations"] = _resolve_canonical_designations(
                canonical, classes_dict,
            )
        # ``is_being_written=True`` — 아래 ``_save_dictionary`` 가 아직 안 돌았으므로
        # 디스크의 딕셔너리는 직전 세대다. 이 플래그를 빼면 갓 만든 산출물에
        # ``stale: true`` 가 찍힌다 (2026-08-30 실측, ``_enrich_metadata`` docstring).
        dictionary["metadata"] = _enrich_metadata(
            dictionary["metadata"], is_being_written=True,
        )

        # 6. description_en 보강 (Bedrock LLM)
        _translate_missing_descriptions_en(classes_dict)

        # 7. 저장 + 응답
        _save_dictionary(dictionary)
        return _build_success_response(
            classes_dict, obj_props_dict, func_props,
            sparql_guide, question_templates, inference_classes,
            include_stats,
        )
    except Exception as e:
        return json.dumps(
            {"success": False, "error": str(e)},
            ensure_ascii=False,
        )


def read_semantic_dictionary() -> str:
    """로컬 파일에서 시맨틱 딕셔너리 JSON을 읽는다. (SEMANTIC_DICT_PATH)

    generate_semantic_dictionary로 생성된 딕셔너리를 조회한다.
    클래스·프로퍼티·통계·SPARQL 가이드·질문 템플릿 등을 포함한다.
    """
    try:
        with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return json.dumps(
            {
                "error": f"시맨틱 딕셔너리가 없습니다 ({SEMANTIC_DICT_PATH}). generate_semantic_dictionary를 먼저 실행하세요.",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return error_response(e, logger=logger)


def read_lpg_semantic_dictionary() -> str:
    """Neo4j LPG 전용 시맨틱 딕셔너리 JSON을 읽는다.

    generate_lpg_semantic_dictionary로 생성된 딕셔너리를 조회한다.
    Neo4j 라벨·프로퍼티·관계 타입·2홉 경로 정보를 포함한다.
    """
    try:
        with open(LPG_SEMANTIC_DICT_PATH, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return json.dumps(
            {
                "error": f"LPG 시맨틱 딕셔너리가 없습니다 ({LPG_SEMANTIC_DICT_PATH}). generate_lpg_semantic_dictionary를 먼저 실행하세요.",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return error_response(e, logger=logger)
