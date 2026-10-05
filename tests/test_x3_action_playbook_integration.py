"""X3 action_playbook 통합 테스트 — run_meta_audit_impl 결과에 playbook 포함.

단위 테스트 (test_meta_audit_playbook.py) 가 compute_action_playbook 을
고립 검증한다면, 이 파일은 run_meta_audit_impl 엔드-투-엔드로
action_playbook 필드가 artifact JSON 에 실제로 쓰이는지 + mock mutation 데이터로
M4 blind spot / all-green 경로를 검증한다.
"""
import json


def test_expected_checks_are_real_check_names():
    """_MUTATION_CATEGORY_EXPECTED_CATCHERS 값이 실제 caught_by 에 나타날 수 있는 이름인지 검증.

    실행 근거:
    - T-Box mutation (S4.5) 의 caught_by 는 `_run_tbox_validators` 가 반환하는
      짧은 별칭 (syntax/quality/hermit/classify/shacl) 으로 채워짐
      (tools/mutation_runner.py:207-217).
    - KG mutation (S9.5) 의 caught_by 는 `validate_kg` 가 반환하는 check 의
      한국어 display name (tools/validation_support/checks/*.py 내 {"name": ...})
      으로 채워짐 (tools/mutation_runner.py:456).

    따라서 매핑 값은 둘 중 하나에 속해야 하며, 그 외는 실제로 `caught_by` 에
    나타날 수 없어 sensitivity_matrix 와 cross-reference 되지 않는 fake 이름이다.
    """
    # 유효 식별자는 **소스에서 유도한다**. 예전에는 여기 목록을 손으로 적어
    # 뒀는데 22번째 check(``스키마 참조 무결성``)가 빠져 있어서, 실재하는 이름을
    # 매핑에 넣자 "fake 이름" 이라며 실패했다 (2026-09-05).
    from tests.helpers_check_names import (
        TBOX_VALIDATOR_ALIASES as VALID_TBOX_ALIASES,
    )
    from tests.helpers_check_names import (
        kg_check_display_names,
    )
    from tools.meta_audit import _MUTATION_CATEGORY_EXPECTED_CATCHERS

    VALID_KG_DISPLAY_NAMES = kg_check_display_names()
    ALL_VALID = VALID_TBOX_ALIASES | VALID_KG_DISPLAY_NAMES

    for cat, catchers in _MUTATION_CATEGORY_EXPECTED_CATCHERS.items():
        for c in catchers:
            assert c in ALL_VALID, (
                f"'{c}' in {cat} mapping is not a real check identifier. "
                f"Must be a T-Box short alias {VALID_TBOX_ALIASES} or "
                f"a KG check display name from validation_support/checks/*.py."
            )


def test_run_meta_audit_includes_action_playbook(tmp_path):
    """run_meta_audit_impl 결과 JSON 에 action_playbook 키 포함.

    M4 category 에 applied=True 지만 caught_by=[] 인 mutant 2개 → high priority
    blind_spot playbook entry 생성돼야 함.
    """
    from tools.meta_audit import run_meta_audit_impl

    runs_dir = tmp_path / "runs" / "20260101T000000Z"
    runs_dir.mkdir(parents=True)
    (runs_dir / "tbox.json").write_text(json.dumps({
        "timestamp": "2026-01-01T00:00:00Z",
        "mutants": [
            {"mutant_id": "M4_disjoint/delete_1", "applied": True,
             "category": "M4", "caught_by": [],
             "baseline_summary": {"disjoint": "PASS",
                                  "fk_ref": "PASS"},
             "mutant_summary": {"disjoint": "PASS",
                                "fk_ref": "PASS"},
             "duration_s": 0.5},
            {"mutant_id": "M4_disjoint/delete_2", "applied": True,
             "category": "M4", "caught_by": [],
             "baseline_summary": {"disjoint": "PASS",
                                  "fk_ref": "PASS"},
             "mutant_summary": {"disjoint": "PASS",
                                "fk_ref": "PASS"},
             "duration_s": 0.5},
        ],
    }))

    history_path = tmp_path / "history.json"
    history_path.write_text("[]")

    out_path = tmp_path / "audit.json"

    artifact = run_meta_audit_impl(
        runs_root=str(tmp_path / "runs"),
        history_path=str(history_path),
        out_path=str(out_path),
        window_commits=30,
    )

    # action_playbook 키 존재
    assert "action_playbook" in artifact
    playbook = artifact["action_playbook"]
    # 기존 blind_spots/dead_checks 필드도 보존 (backward compat)
    assert "blind_spots" in artifact
    assert "dead_checks" in artifact

    # M4 blind_spot → high priority entry
    m4_entries = [p for p in playbook
                  if p.get("type") == "blind_spot" and p.get("category") == "M4"]
    assert len(m4_entries) == 1
    m4 = m4_entries[0]
    assert m4["priority"] == "high"
    # M4 매핑은 실제 caught_by 에 나타날 수 있는 이름 (T-Box 별칭 hermit/shacl 또는
    # KG display name "AllDisjointClasses 위반") 이어야 함.
    assert "hermit" in m4["expected_checks"]
    assert "AllDisjointClasses 위반" in m4["expected_checks"]
    # 자동 편집 필드 없음 (사람 검토 필수)
    assert "auto_patch" not in m4["suggested_action"]

    # Disk 에 저장된 JSON 에도 action_playbook 포함
    written = json.loads(out_path.read_text())
    assert "action_playbook" in written
    assert any(p.get("category") == "M4"
               for p in written["action_playbook"])


def test_run_meta_audit_empty_playbook_when_all_green(tmp_path):
    """모든 mutant 가 catch 된 경우 playbook 에 blind_spot entry 없음."""
    from tools.meta_audit import run_meta_audit_impl

    runs_dir = tmp_path / "runs" / "20260102T000000Z"
    runs_dir.mkdir(parents=True)
    (runs_dir / "tbox.json").write_text(json.dumps({
        "mutants": [
            {"mutant_id": "M1_a", "applied": True, "category": "M1",
             "caught_by": ["domain_range_conformance"],
             "baseline_summary": {"domain_range_conformance": "PASS"},
             "mutant_summary": {"domain_range_conformance": "FAIL"},
             "duration_s": 0.3},
        ],
    }))

    history_path = tmp_path / "history.json"
    history_path.write_text("[]")

    artifact = run_meta_audit_impl(
        runs_root=str(tmp_path / "runs"),
        history_path=str(history_path),
        out_path=str(tmp_path / "out.json"),
    )

    # M1 catch 됐으므로 blind_spot entry 없음
    blind_spot_entries = [p for p in artifact["action_playbook"]
                          if p.get("type") == "blind_spot"]
    assert blind_spot_entries == []


def test_run_meta_audit_insufficient_history_empty_playbook(tmp_path):
    """history 부족 (insufficient=True) 이고 모든 mutant catch 된 경우
    dead_checks / pairwise_correlation 이 빈 배열 이므로 playbook 도 빈 배열."""
    from tools.meta_audit import run_meta_audit_impl

    runs_dir = tmp_path / "runs" / "20260103T000000Z"
    runs_dir.mkdir(parents=True)
    (runs_dir / "tbox.json").write_text(json.dumps({
        "mutants": [
            {"mutant_id": "M1_a", "applied": True, "category": "M1",
             "caught_by": ["domain_range"],
             "baseline_summary": {"domain_range": "PASS"},
             "mutant_summary": {"domain_range": "FAIL"},
             "duration_s": 0.3},
        ],
    }))

    history_path = tmp_path / "history.json"
    history_path.write_text("[]")  # 0 entries < min_history=5

    artifact = run_meta_audit_impl(
        runs_root=str(tmp_path / "runs"),
        history_path=str(history_path),
        out_path=str(tmp_path / "out.json"),
    )

    assert artifact["history_insufficient"] is True
    assert artifact["dead_checks"] == []
    assert artifact["pairwise_correlation"] == []
    # M1 전량 catch + insufficient → playbook 완전히 빈 배열
    assert artifact["action_playbook"] == []
