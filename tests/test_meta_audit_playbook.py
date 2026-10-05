"""X3 compute_action_playbook 테스트.

meta_audit 결과를 구체 액션으로 변환하는 로직 검증. 자동 코드 편집 없음 —
사람이 검토할 제안만 생성하는지 확인.
"""
from tools.meta_audit import (
    _MUTATION_CATEGORY_EXPECTED_CATCHERS,
    compute_action_playbook,
)


def test_blind_spot_generates_high_priority_entry():
    """caught_by_any_check == 0 이면 high priority 로 분류."""
    blind_spots = [
        {"category": "M4", "mutants": 2, "caught_by_any_check": 0,
         "recommendation": "investigate_coverage_gap"},
    ]
    playbook = compute_action_playbook(blind_spots, [], [])
    assert len(playbook) == 1
    entry = playbook[0]
    assert entry["priority"] == "high"
    assert entry["type"] == "blind_spot"
    assert entry["category"] == "M4"
    # 기대 check 매핑이 반영됐는지 (M4 → HermiT 혹은 AllDisjoint display name)
    # 매핑 값은 실제 caught_by 에 나타날 수 있는 이름이어야 함 — cross-reference 검증은
    # tests/test_x3_action_playbook_integration.py::test_expected_checks_are_real_check_names
    assert "hermit" in entry["expected_checks"]
    assert "AllDisjointClasses 위반" in entry["expected_checks"]
    # 자동 코드 편집 필드 없음
    assert "auto_patch" not in entry["suggested_action"]
    assert "investigation_hint" in entry["suggested_action"]


def test_partial_blind_spot_medium_priority():
    """일부만 catch (caught > 0 이지만 < mutants) 면 medium priority."""
    blind_spots = [
        {"category": "M5", "mutants": 3, "caught_by_any_check": 1,
         "recommendation": "add_annotation_check_or_accept_blindspot"},
    ]
    playbook = compute_action_playbook(blind_spots, [], [])
    assert len(playbook) == 1
    assert playbook[0]["priority"] == "medium"


def test_dead_check_generates_low_priority():
    """dead_check 는 low priority + 제거/유지 판단 기준 제공."""
    dead_checks = [
        {"check": "fk_op_coverage", "history_fires": 0,
         "mutation_catches": 0, "recommendation": "consider_removal"},
    ]
    playbook = compute_action_playbook([], dead_checks, [])
    assert len(playbook) == 1
    entry = playbook[0]
    assert entry["priority"] == "low"
    assert entry["type"] == "dead_check"
    assert entry["check"] == "fk_op_coverage"
    # 제거/유지 판단 기준 제공
    assert "keep_if" in entry["suggested_action"]
    assert "remove_if" in entry["suggested_action"]
    assert entry["suggested_action"]["kind"] == "investigate_or_remove"


def test_correlation_gap_redundant_only():
    """redundant_candidate 만 playbook 에 포함, cooccurrence_candidate 는 skip."""
    pairs = [
        {"pair": ["check_a", "check_b"], "cooccurrence": 0.95,
         "sample_size": 10, "interpretation": "redundant_candidate"},
        {"pair": ["check_c", "check_d"], "cooccurrence": 0.92,
         "sample_size": 7, "interpretation": "cooccurrence_candidate"},
    ]
    playbook = compute_action_playbook([], [], pairs)
    # redundant 만 포함
    assert len(playbook) == 1
    assert playbook[0]["pair"] == ["check_a", "check_b"]
    assert playbook[0]["type"] == "correlation_gap"
    assert playbook[0]["priority"] == "low"
    assert playbook[0]["suggested_action"]["kind"] == "consolidate_or_keep"


def test_priority_sorting():
    """high 먼저, medium, low 순 정렬."""
    blind_spots = [
        {"category": "M1", "mutants": 1, "caught_by_any_check": 0,
         "recommendation": "investigate_coverage_gap"},  # high
        {"category": "M2", "mutants": 3, "caught_by_any_check": 1,
         "recommendation": "investigate_coverage_gap"},  # medium
    ]
    dead_checks = [
        {"check": "x", "history_fires": 0, "mutation_catches": 0,
         "recommendation": "consider_removal"},  # low
    ]
    playbook = compute_action_playbook(blind_spots, dead_checks, [])
    priorities = [p["priority"] for p in playbook]
    assert priorities == ["high", "medium", "low"]


def test_category_mapping_covers_all_seven():
    """7 카테고리 (M1..M7) 모두 매핑 존재하고 non-empty."""
    for cat in ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]:
        assert cat in _MUTATION_CATEGORY_EXPECTED_CATCHERS
        assert _MUTATION_CATEGORY_EXPECTED_CATCHERS[cat]
        assert isinstance(_MUTATION_CATEGORY_EXPECTED_CATCHERS[cat], list)


def test_empty_input_returns_empty_playbook():
    """모든 입력 빈 배열 → 빈 playbook (insufficient history case 포함)."""
    assert compute_action_playbook([], [], []) == []


def test_unknown_category_graceful():
    """매핑에 없는 카테고리도 처리 (빈 expected_checks + hint 없음)."""
    blind_spots = [
        {"category": "M99", "mutants": 1, "caught_by_any_check": 0,
         "recommendation": "investigate_coverage_gap"},
    ]
    playbook = compute_action_playbook(blind_spots, [], [])
    assert len(playbook) == 1
    entry = playbook[0]
    assert entry["expected_checks"] == []
    # Kind 는 여전히 extend_check
    assert entry["suggested_action"]["kind"] == "extend_check"
    # Important #3: expected_checks 가 비면 오해 소지가 있는 catalog_path /
    # investigation_hint 를 만들지 않는다 (존재하지 않는 디렉토리 안내 방지).
    assert "catalog_path" not in entry["suggested_action"]
    assert "investigation_hint" not in entry["suggested_action"]


def test_blind_spot_low_priority_when_mostly_caught():
    """Important #2: 50% 이상 catch 되면 low priority (시급하지 않음).

    3-tier priority:
      - 0 catch → high
      - 0 < catch_ratio < 0.5 → medium
      - 0.5 <= catch_ratio < 1.0 → low
    """
    blind_spots = [
        # 4/5 = 80% catch → low
        {"category": "M6", "mutants": 5, "caught_by_any_check": 4,
         "recommendation": "investigate_coverage_gap"},
    ]
    playbook = compute_action_playbook(blind_spots, [], [])
    assert len(playbook) == 1
    assert playbook[0]["priority"] == "low"
    assert playbook[0]["type"] == "blind_spot"


def test_blind_spot_priority_tri_tier_boundary():
    """3-tier 경계: caught=0 → high, 0<r<0.5 → medium, r>=0.5 → low.

    boundary case catch_ratio == 0.5 → low (>= 0.5 규칙).
    """
    # 0 / 4 → high
    # 1 / 4 = 0.25 → medium
    # 2 / 4 = 0.50 → low (경계값)
    blind_spots = [
        {"category": "M1", "mutants": 4, "caught_by_any_check": 0},
        {"category": "M2", "mutants": 4, "caught_by_any_check": 1},
        {"category": "M3", "mutants": 4, "caught_by_any_check": 2},
    ]
    playbook = compute_action_playbook(blind_spots, [], [])
    priorities = {p["category"]: p["priority"] for p in playbook}
    assert priorities["M1"] == "high"
    assert priorities["M2"] == "medium"
    assert priorities["M3"] == "low"


def test_suggested_action_no_auto_patch():
    """자동 코드 편집 필드 없음 — 사람 판단 필수."""
    blind_spots = [
        {"category": "M1", "mutants": 1, "caught_by_any_check": 0,
         "recommendation": "investigate_coverage_gap"},
    ]
    dead_checks = [
        {"check": "x", "history_fires": 0, "mutation_catches": 0,
         "recommendation": "consider_removal"},
    ]
    pairs = [
        {"pair": ["a", "b"], "cooccurrence": 0.95,
         "sample_size": 10, "interpretation": "redundant_candidate"},
    ]
    playbook = compute_action_playbook(blind_spots, dead_checks, pairs)
    forbidden_keys = ["auto_patch", "code_diff", "run_command"]
    for entry in playbook:
        action = entry["suggested_action"]
        for k in forbidden_keys:
            assert k not in action, f"Entry {entry['type']} has forbidden key {k}"


def test_fully_caught_category_excluded():
    """caught_by_any_check == mutants 인 경우 playbook 에 blind_spot 없음.

    compute_blind_spots 가 이미 필터링하지만 compute_action_playbook 도 방어적으로
    처리해야 함 (direct call 시).
    """
    blind_spots = [
        {"category": "M1", "mutants": 2, "caught_by_any_check": 2,
         "recommendation": "all_caught"},
    ]
    playbook = compute_action_playbook(blind_spots, [], [])
    # 전량 catch 된 경우 playbook 에서 제외
    assert playbook == []
