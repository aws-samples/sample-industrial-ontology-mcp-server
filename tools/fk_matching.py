"""FK value 퍼지 매칭 — 4단계 fallback (exact → normalized → levenshtein → prefix).

A-Box 생성 시 FK 컬럼 값이 master_data 의 인스턴스 URI 와 변형으로 인해
일치하지 않는 경우 자동 복원을 시도한다. 자사 도메인 ERP/SAP/Excel exports
에서 흔한 ID 포맷 불일치 (하이픈, 언더스코어, 대소문자, trailing space) 를
안전하게 흡수한다.

본 모듈은 도메인-중립이며 `abox_generation.py` hot loop 에서 호출되므로
성능에 민감하다. normalized 경로는 O(1) 딕셔너리 조회, levenshtein/prefix
는 opt-in 으로만 활성화된다 (기본 off — false positive 방지).

설정: `rules/domain/domain_config.json` 의 `fk_fuzzy_match` 섹션
    {"normalized": bool, "levenshtein": bool, "prefix": bool}

통계: 단계별 카운터는 `_fk_match_stage_counter` 에 누적되고,
`get_and_clear_stats()` 로 generate_abox 응답에 포함된다.
"""

from __future__ import annotations

import threading
import unicodedata

from rdflib import URIRef

# ── Stage counter (module-level, thread-safe) ──────────────────────────
# abox_generation.py 는 CSV prefetch 외엔 단일 스레드 hot loop 지만,
# 향후 병렬화 도입 대비 Lock 으로 보호. 비용은 경합 없으면 무시할 수준.
_fk_match_stage_counter: dict[str, int] = {
    "exact": 0,
    "normalized": 0,
    "levenshtein": 0,
    "prefix": 0,
    "unresolved": 0,
}
_counter_lock = threading.Lock()


def _bump_stage(stage: str) -> None:
    """Thread-safe stage counter increment."""
    with _counter_lock:
        _fk_match_stage_counter[stage] = _fk_match_stage_counter.get(stage, 0) + 1


def get_and_clear_stats() -> dict[str, int]:
    """Return a snapshot of the stage counter and reset it.

    generate_abox 시작 시 호출해 이전 실행 잔여분을 비우고, 종료 시
    호출해 결과 JSON 에 포함. 원자적으로 읽고-초기화 하기 위해 lock
    을 전역에서 한 번 잡는다.
    """
    global _fk_match_stage_counter
    with _counter_lock:
        snapshot = dict(_fk_match_stage_counter)
        _fk_match_stage_counter = {
            "exact": 0,
            "normalized": 0,
            "levenshtein": 0,
            "prefix": 0,
            "unresolved": 0,
        }
    return snapshot


def clear_stats() -> None:
    """Reset the stage counter without reading it (used at generate_abox start)."""
    get_and_clear_stats()


# ── Public API ─────────────────────────────────────────────────────────


def _normalize_fk_value(v: str) -> str:
    """FK value 의 변형을 제거해 매칭 가능한 canonical form 으로 변환.

    처리:
        - NFKC 정규화 (fullwidth → ASCII, 호환 문자 통합)
        - 양끝 공백 strip
        - 대소문자 → lowercase
        - 하이픈 / 언더스코어 / 도트 / 내부 공백 / em dash / en dash 제거

    예:
        "EQ-001" → "eq001"
        "eq_001" → "eq001"
        "EQ.001" → "eq001"
        "  EQ001 " → "eq001"
        "ＥＱ－００１" → "eq001"  (fullwidth, SAP/ERP 한일 exports)
        "EQ—001"   → "eq001"  (em dash)
        "EQ–001"   → "eq001"  (en dash)
    """
    if not v:
        return ""
    # NFKC: fullwidth → ASCII, 호환 문자 통합. 한일 SAP exports 대응.
    s = unicodedata.normalize("NFKC", str(v)).strip().lower()
    # 자주 쓰이는 구분자 제거 — ASCII + em/en dash 포함. 숫자/문자는 유지.
    for sep in ("-", "_", ".", " ", "–", "—"):
        s = s.replace(sep, "")
    return s


def _levenshtein_le1(a: str, b: str) -> bool:
    """edit distance ≤ 1 인지 O(len) 체크.

    full Levenshtein DP 보다 훨씬 빠른 special-case. 한 글자 치환/삽입/삭제
    만 True 반환. 길이 차 > 1 이면 즉시 False.

    false positive 우려 때문에 distance > 1 은 지원 안 함 (plan Non-goal).
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        # 동일 길이 — 한 글자 치환 경우만 유효
        diff = 0
        for x, y in zip(a, b):
            if x != y:
                diff += 1
                if diff > 1:
                    return False
        return diff == 1
    # 길이 차 1 → 한 글자 삽입/삭제 케이스
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    found_diff = False
    while i < len(short) and j < len(long_):
        if short[i] != long_[j]:
            if found_diff:
                return False
            found_diff = True
            j += 1  # long 쪽에서만 advance (삽입/삭제 정렬)
        else:
            i += 1
            j += 1
    return True


def build_master_value_index(
    master_instance_uris: set[str],
    available_classes: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """master_instance_uris 를 target_class 별로 분해 + normalized 인덱스 구축.

    URI 형태: "http://.../Inst#EquipmentMaster_EQ001"
        → local name 에서 클래스명 기준 split
        → class="EquipmentMaster", suffix="EQ001"
        → normalized key "eq001"

    Args:
        master_instance_uris: master_data.ttl 에서 로드한 URI set.
        available_classes: 알려진 클래스명 집합. 제공되면 local name 이 어느
            클래스로 시작하는지 prefix 매칭으로 정확히 분리한다
            (`Equipment_Master` 처럼 클래스명에 언더스코어가 포함된 도메인
            지원). None 이면 첫 언더스코어 split 폴백 (CamelCase 클래스명
            가정 — 기존 동작 유지).

    Returns:
        {target_class: {normalized_suffix: original_uri}}

    비용: O(|master_instance_uris| × |available_classes|) in worst case.
    수천~수만 URI 라도 < 1초.

    **중복 key 는 결정적으로 해소한다.** ``EQ-001`` / ``EQ_001`` / ``eq001`` 은 모두
    ``eq001`` 로 정규화되므로 한 key 에 여러 URI 가 몰린다. 예전엔 ``set`` 순회
    순서대로 나중 항목이 덮어써서 **PYTHONHASHSEED 에 따라 승자가 바뀌었다**
    (2026-08-08 실측: 같은 입력으로 6개 시드에서 3가지 결과). 그러면 바이트 동일한
    CSV+T-Box 로 A-Box 를 두 번 만들었을 때 같은 FK 가 **서로 다른 master
    인스턴스** 를 가리키고, 그 FK 를 타는 모든 SPARQL 조인 결과가 달라진다
    (``fk_referential_integrity`` 가 PASS/FAIL 을 왕복하고 roundtrip 이 원인 없는
    drift 를 보고한다). URI 사전순 최소를 정본으로 고정해 재현성을 보장한다.
    """
    # 긴 이름 우선: `Equipment_Master` 가 `Equipment` 보다 먼저 매칭되도록 정렬.
    sorted_classes: list[str] | None = None
    if available_classes:
        sorted_classes = sorted(available_classes, key=len, reverse=True)

    index: dict[str, dict[str, str]] = {}
    # URI 를 정렬해 순회한다 — set 순회 순서는 프로세스마다 다르다.
    for uri in sorted(master_instance_uris):
        # fragment (#) 뒤의 local name 추출. fragment 없으면 path 마지막.
        if "#" in uri:
            local = uri.rsplit("#", 1)[-1]
        else:
            local = uri.rsplit("/", 1)[-1]

        cls: str | None = None
        suffix: str | None = None

        if sorted_classes:
            # 가장 긴 매칭 클래스명 찾기 (snake_case 클래스명 안전 처리).
            for candidate in sorted_classes:
                if local.startswith(candidate + "_"):
                    cls = candidate
                    suffix = local[len(candidate) + 1:]
                    break

        if cls is None:
            # Fallback: first underscore split (CamelCase 클래스명 가정).
            # available_classes 미제공 또는 매칭 실패 시의 backward compat 경로.
            parts = local.split("_", 1)
            if len(parts) != 2:
                continue
            cls, suffix = parts

        if cls and suffix:
            norm = _normalize_fk_value(suffix)
            if norm:
                # 사전순 최소를 정본으로 유지 (setdefault 로 첫 항목 고정).
                # 정렬 순회 + setdefault 조합이라 결과가 시드에 불변이다.
                index.setdefault(cls, {}).setdefault(norm, uri)
    return index


def resolve_fk_target(
    target_class: str,
    fk_value: str | None,
    candidate_uri: URIRef,
    master_instance_uris: set[str],
    master_value_index: dict[str, dict[str, str]],
    fuzzy_config: dict,
) -> tuple[URIRef, str]:
    """4단계 fallback 으로 실제 target URI 결정.

    단계:
        1. exact        — candidate_uri 가 master set 에 이미 있음
        2. normalized   — 양쪽 정규화 후 일치 (기본 ON)
        3. levenshtein  — edit distance ≤ 1 (opt-in)
        4. prefix       — 모호하지 않은 prefix 매칭 (opt-in)
        → 실패 시 candidate_uri 그대로 반환 (기존 동작 유지)

    Args:
        target_class: FK 타겟 클래스명 (e.g., "EquipmentMaster")
        fk_value: CSV 원본 FK 값 (e.g., "EQ-001"). None / 빈 문자열이면 즉시 unresolved.
        candidate_uri: `DOMAIN_INST_NS_OBJ[f"{target_class}_{fk_value}"]` 로 만든 후보
        master_instance_uris: master_data.ttl 에서 로드한 전체 URI set
        master_value_index: `build_master_value_index` 결과
        fuzzy_config: {"normalized": bool, "levenshtein": bool, "prefix": bool}

    Returns:
        (resolved_uri, match_stage)
        match_stage: "exact" / "normalized" / "levenshtein" / "prefix" / "unresolved"

    Side effects:
        `_fk_match_stage_counter[stage]` 를 bump (통계 수집).
    """
    # None / 빈 문자열 가드 — bogus `EquipmentMaster_None` URI 생성 방지.
    if fk_value is None or fk_value == "":
        _bump_stage("unresolved")
        return candidate_uri, "unresolved"

    # Stage 1: exact
    if str(candidate_uri) in master_instance_uris:
        _bump_stage("exact")
        return candidate_uri, "exact"

    cls_index = master_value_index.get(target_class, {})
    norm = _normalize_fk_value(fk_value) if cls_index else ""

    # Stage 2: normalized (기본 ON)
    if fuzzy_config.get("normalized", True) and norm and norm in cls_index:
        _bump_stage("normalized")
        return URIRef(cls_index[norm]), "normalized"

    # Stage 3: Levenshtein ≤ 1 (opt-in, O(N) iteration).
    # key 순으로 정렬해 순회한다 — dict 순서는 삽입 순서이고 그 삽입은 master URI
    # 집합 순회에서 왔으므로, 근사 후보가 2개 이상이면 어느 쪽이 이기는지가
    # 프로세스마다 달라졌다. 사전순 최소를 고정해 재현성을 보장한다 (2026-08-08).
    if fuzzy_config.get("levenshtein", False) and norm and cls_index:
        for key in sorted(cls_index):
            if _levenshtein_le1(norm, key):
                _bump_stage("levenshtein")
                return URIRef(cls_index[key]), "levenshtein"

    # Stage 4: prefix (opt-in, unambiguous only)
    # 방향 제한: source 가 master 의 '짧은 prefix' 인 경우만 허용 (반대 방향 제외).
    # 반대 방향 (`norm.startswith(k)`) 을 허용하면 신규 ID `EQ0013` 이 기존
    # master `EQ001` 로 silent misroute 된다. 최소 3글자 가드로 `EQ` 같은
    # 극단적 짧은 prefix 의 거짓 양성 추가 차단.
    if (
        fuzzy_config.get("prefix", False)
        and norm
        and len(norm) >= 3
        and cls_index
    ):
        matches = [uri for k, uri in cls_index.items() if k.startswith(norm)]
        if len(matches) == 1:
            _bump_stage("prefix")
            return URIRef(matches[0]), "prefix"

    _bump_stage("unresolved")
    return candidate_uri, "unresolved"
