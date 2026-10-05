"""FK 퍼지 매칭 단위 테스트 (A1 plan 의 7 케이스).

4단계 fallback (exact → normalized → levenshtein → prefix) 이 의도대로 동작하는지,
그리고 opt-in 설정 (levenshtein / prefix) 이 끄면 꺼지는지 확인한다.
"""

from rdflib import URIRef

from tools.fk_matching import (
    _levenshtein_le1,
    _normalize_fk_value,
    build_master_value_index,
    resolve_fk_target,
)


def test_normalize_removes_separators():
    # 하이픈 / 언더스코어 / 도트 / 공백 제거 + lowercase
    assert _normalize_fk_value("EQ-001") == "eq001"
    assert _normalize_fk_value("eq_001") == "eq001"
    assert _normalize_fk_value("  EQ001 ") == "eq001"
    assert _normalize_fk_value("EQ.001") == "eq001"
    assert _normalize_fk_value("") == ""


def test_levenshtein_le1_cases():
    # 동일 문자열
    assert _levenshtein_le1("abc", "abc") is True
    # 1 치환
    assert _levenshtein_le1("abc", "abd") is True
    # 1 삽입 (길이 차 1)
    assert _levenshtein_le1("abc", "abcd") is True
    assert _levenshtein_le1("abcd", "abc") is True
    # 1 삭제
    assert _levenshtein_le1("abc", "ac") is True
    # 2 치환 → False
    assert _levenshtein_le1("abc", "xyz") is False
    # 길이 차 2 → False (한 글자 이상 삽입/삭제 필요)
    assert _levenshtein_le1("abc", "abcde") is False


def test_build_master_value_index():
    uris = {
        "http://ex/Inst#EquipmentMaster_EQ001",
        "http://ex/Inst#EquipmentMaster_EQ002",
        "http://ex/Inst#TagMaster_T-001",
    }
    idx = build_master_value_index(uris)
    assert "EquipmentMaster" in idx
    assert "eq001" in idx["EquipmentMaster"]
    assert idx["EquipmentMaster"]["eq001"] == "http://ex/Inst#EquipmentMaster_EQ001"
    # hyphen normalized
    assert "TagMaster" in idx
    assert "t001" in idx["TagMaster"]


def test_resolve_stage_exact():
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_EQ001")
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ001", candidate, uris, idx, {"normalized": True},
    )
    assert stage == "exact"
    assert resolved == candidate


def test_resolve_stage_normalized_hyphen():
    """CSV 에 EQ-001 인데 master 에는 EQ001 → normalized 단계에서 매칭."""
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_EQ-001")  # 존재 안 함
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ-001", candidate, uris, idx, {"normalized": True},
    )
    assert stage == "normalized"
    assert str(resolved) == "http://ex/Inst#EquipmentMaster_EQ001"


def test_resolve_stage_levenshtein_optin():
    """Levenshtein 은 opt-in. default off 면 unresolved."""
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_EQ01")

    # OFF — 기본 설정
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ01", candidate, uris, idx, {"normalized": True},
    )
    assert stage == "unresolved"

    # ON — 명시적 활성화
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ01", candidate, uris, idx,
        {"normalized": True, "levenshtein": True},
    )
    assert stage == "levenshtein"
    assert str(resolved) == "http://ex/Inst#EquipmentMaster_EQ001"


def test_resolve_all_fail_returns_candidate():
    """모든 단계 실패 → 원본 candidate 반환 + stage=unresolved."""
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_UNKNOWN")
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "UNKNOWN", candidate, uris, idx, {"normalized": True},
    )
    assert stage == "unresolved"
    assert resolved == candidate


def test_prefix_does_not_misroute_longer_source_to_shorter_master():
    """Critical #1: source ID 가 master 보다 '길' 때 prefix 매칭 금지.

    새 ID `EQ0013` 이 기존 master `EQ001` 에 잘못 매칭되면
    신규 엔티티가 조용히 구 엔티티로 병합된다 (silent misroute).
    source 가 master 의 prefix 인 방향만 허용해야 한다.
    """
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_EQ0013")
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ0013", candidate, uris, idx,
        {"normalized": True, "prefix": True},
    )
    # EQ0013 은 master 에 없는 '신규' ID 이므로 unresolved 여야 한다.
    assert stage == "unresolved", f"신규 ID EQ0013 이 EQ001 로 오염 매칭됨: stage={stage}"


def test_prefix_matches_when_source_is_shorter_prefix_of_master():
    """Critical #1 (허용 방향): source 가 master 의 짧은 prefix 면 매칭 허용.

    사용자가 축약형 `EQ001` 을 타이핑했고 master 에 더 긴 `EQ0012` 한 건만 있을 때
    모호하지 않으므로 매칭 가능. (단일 매칭만 prefix 단계에서 반환)
    """
    uris = {"http://ex/Inst#EquipmentMaster_EQ0012"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_EQ001")
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ001", candidate, uris, idx,
        {"normalized": True, "prefix": True},
    )
    assert stage == "prefix"
    assert str(resolved) == "http://ex/Inst#EquipmentMaster_EQ0012"


def test_prefix_skips_when_source_shorter_than_min_length():
    """Critical #1: 최소 3글자 가드 — `EQ` 같은 2글자는 prefix 매칭 스킵."""
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_EQ")
    resolved, stage = resolve_fk_target(
        "EquipmentMaster", "EQ", candidate, uris, idx,
        {"normalized": True, "prefix": True},
    )
    assert stage == "unresolved", "2글자 source 로 prefix 매칭되면 안 됨"


def test_index_handles_snake_case_classnames():
    """Critical #2: snake_case 클래스명이 first-underscore split 으로 깨지지 않는다.

    `Equipment_Master` 같은 snake_case 클래스명을 가진 도메인에서
    URI local name `Equipment_Master_EQ001` 이 `cls=Equipment`,
    `suffix=Master_EQ001` 로 잘못 파싱되던 문제 수정.
    """
    uris = {"http://ex/Inst#Equipment_Master_EQ001"}

    # available_classes 없음 → fallback (first underscore split) 사용.
    # 이 경우는 '잘못된 파싱' 이지만 비정상 종료하지 않아야 한다.
    idx = build_master_value_index(uris)
    assert "Equipment" in idx  # legacy fallback behavior

    # available_classes 제공 → 올바른 split
    idx = build_master_value_index(uris, available_classes={"Equipment_Master"})
    assert "Equipment_Master" in idx
    assert "eq001" in idx["Equipment_Master"]
    assert idx["Equipment_Master"]["eq001"] == "http://ex/Inst#Equipment_Master_EQ001"


def test_normalize_handles_unicode():
    """Important #3: NFKC 정규화로 fullwidth / em dash / en dash 흡수.

    한국/일본 SAP/ERP exports 가 흔히 보내는 fullwidth 영숫자 (ＥＱ－００１)
    와 em/en dash 를 ASCII 로 정규화.
    """
    # Fullwidth → ASCII via NFKC
    assert _normalize_fk_value("ＥＱ－００１") == "eq001"
    # Em dash
    assert _normalize_fk_value("EQ—001") == "eq001"
    # En dash
    assert _normalize_fk_value("EQ–001") == "eq001"


def test_resolve_handles_none_and_empty():
    """Important #4: fk_value 가 None / 빈 문자열이면 즉시 unresolved.

    이전에는 `str(None)` → `"None"` 으로 변환돼 bogus URI 가 만들어지고
    조용히 unresolved 처리되던 문제. None 가드로 명시적 처리.
    """
    uris = {"http://ex/Inst#EquipmentMaster_EQ001"}
    idx = build_master_value_index(uris)
    candidate = URIRef("http://ex/Inst#EquipmentMaster_None")

    for val in [None, ""]:
        resolved, stage = resolve_fk_target(
            "EquipmentMaster", val, candidate, uris, idx, {"normalized": True},
        )
        assert stage == "unresolved", f"fk_value={val!r} 에서 stage={stage}"
        assert resolved == candidate


def test_index_prefers_longest_classname_when_ambiguous():
    """Critical #2: `Equipment` 와 `Equipment_Master` 둘 다 있을 때 가장 긴 이름 우선.

    URI `Equipment_Master_EQ001` 는 `Equipment_Master` 에 우선 귀속돼야 한다
    (짧은 이름 `Equipment` 로 잘못 나누면 suffix=`Master_EQ001` 가 됨).
    """
    uris = {"http://ex/Inst#Equipment_Master_EQ001"}
    idx = build_master_value_index(
        uris, available_classes={"Equipment", "Equipment_Master"},
    )
    assert "Equipment_Master" in idx
    assert "eq001" in idx["Equipment_Master"]
    # `Equipment` 로 빠져들어간 잘못된 suffix 가 없어야 한다
    assert "Equipment" not in idx or "master_eq001" not in idx.get("Equipment", {})
