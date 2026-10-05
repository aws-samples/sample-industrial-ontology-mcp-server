"""T3 통합 테스트 — S12 → cq_feedback.json → S2 Architect 프롬프트 라운드트립.

단위 테스트 (test_cq_feedback_loop.py) 가 개별 함수 동작을 검증한다면,
이 파일은 실제 파이프라인 흐름대로 end-to-end 시나리오를 엮어서
테스트한다:

    append_iteration (S12 모사)
        → load_active_suggestions (S2 Architect 모사)
        → format_feedback_for_prompt (Architect 프롬프트 빌드)
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def isolated_feedback(tmp_path, monkeypatch):
    """Point CQ_FEEDBACK_PATH to a tmp file so each test is independent."""
    path = str(tmp_path / "cq_feedback.json")
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", path)
    return path


def test_feedback_roundtrip(isolated_feedback):
    """S12 → append → S2 load → 프롬프트 생성까지 한 번에."""
    from tools import cq_feedback

    # 1. S12 모사: iteration 1 기록
    cq_feedback.append_iteration(
        pass_rate=65.0,
        total_cqs=20,
        passed=13,
        failed=7,
        suggestions=[
            {
                "cq_id": "CQ-07",
                "type": "missing_connection",
                "class_a": "EquipmentMaster",
                "class_b": "AlarmEvents",
                "action": "...",
            },
            {
                "cq_id": "CQ-12",
                "type": "missing_instances",
                "class": "MaintenanceHistory",
                "action": "...",
            },
        ],
    )

    # 2. S2 모사: 다음 iteration Architect 가 active suggestions 로드
    active = cq_feedback.load_active_suggestions()
    assert len(active) == 2
    # age_iterations 높은 순 정렬 — 이 경우 둘 다 1 이므로 순서 무관, 내용만 확인
    classes_a = {s.get("class_a", s.get("class")) for s in active}
    assert "EquipmentMaster" in classes_a
    assert "MaintenanceHistory" in classes_a

    # 3. 프롬프트 포맷
    prev_rate = cq_feedback._load_feedback()["iterations"][-1]["pass_rate"]
    prompt = cq_feedback.format_feedback_for_prompt(active, pass_rate=prev_rate)
    assert "EquipmentMaster" in prompt
    assert "AlarmEvents" in prompt
    assert "MaintenanceHistory" in prompt
    assert "65.0%" in prompt


def test_iteration_sequence_tracks_age(isolated_feedback):
    """같은 suggestion 이 연속 3회 나오면 age_iterations=3 + ⚠️반복 표시."""
    from tools import cq_feedback

    sug = {
        "cq_id": "CQ-07",
        "type": "missing_connection",
        "class_a": "EquipmentMaster",
        "class_b": "AlarmEvents",
        "action": "...",
    }
    for pr in (50.0, 55.0, 60.0):
        cq_feedback.append_iteration(
            pass_rate=pr, total_cqs=20, passed=int(pr / 5),
            failed=20 - int(pr / 5), suggestions=[dict(sug)],
        )

    active = cq_feedback.load_active_suggestions()
    assert len(active) == 1
    assert active[0]["age_iterations"] == 3

    prompt = cq_feedback.format_feedback_for_prompt(active, pass_rate=60.0)
    assert "⚠️반복" in prompt


def test_resolved_cq_ids_tracked(isolated_feedback):
    """이전 fail 이었던 CQ 가 다음 iteration 에서 PASS 되면
    resolved_from_previous 에 기록되는지."""
    from tools import cq_feedback

    # iteration 1: CQ-07 fail
    cq_feedback.append_iteration(
        pass_rate=50.0, total_cqs=10, passed=5, failed=5,
        suggestions=[
            {"cq_id": "CQ-07", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
        ],
    )

    # iteration 2: CQ-07 이 이번엔 PASS → suggestions 에서 빠짐
    cq_feedback.append_iteration(
        pass_rate=80.0, total_cqs=10, passed=8, failed=2,
        suggestions=[],
        resolved_cq_ids=["CQ-07"],
    )

    data = cq_feedback._load_feedback()
    assert data["iterations"][-1]["resolved_from_previous"] == ["CQ-07"]


def test_backward_compat_no_file(isolated_feedback):
    """피드백 파일이 없을 때 load_active_suggestions / format 모두
    빈 출력 → 기존 파이프라인 영향 없음."""
    from tools import cq_feedback

    # 파일 없는 상태 유지 (isolated_feedback 픽스처는 경로만 지정,
    # 실제 파일은 append_iteration 호출 시 생성됨)
    assert not os.path.exists(isolated_feedback)
    assert cq_feedback.load_active_suggestions() == []
    assert cq_feedback.format_feedback_for_prompt([]) == ""

    # _load_previous_pass_rate 도 None 반환
    from tools.multi_agent_tbox import _load_previous_pass_rate
    assert _load_previous_pass_rate() is None


def test_resolved_cq_ids_includes_all_deduped(isolated_feedback):
    """같은 (type, class_a, class_b) 공유하는 여러 CQ 가 resolved 에 모두 포함.

    리뷰 Important #2: query_test.py 가 load_active_suggestions() (dedupe 적용)
    를 사용하면 같은 구조 결함을 공유하는 여러 CQ id 중 1개만 살아남음 —
    raw suggestions 를 직접 읽어 모든 cq_id 를 수집해야 함.
    """
    from tools.cq_feedback import _load_feedback, append_iteration

    # 이전 iteration: 3 CQ 가 같은 dedupe 키 공유
    append_iteration(
        pass_rate=50.0, total_cqs=10, passed=5, failed=5,
        suggestions=[
            {"cq_id": "CQ-01", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
            {"cq_id": "CQ-02", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
            {"cq_id": "CQ-03", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
        ],
    )

    data = _load_feedback()
    prev_sugs = data["iterations"][-1]["suggestions"]
    prev_ids = {s["cq_id"] for s in prev_sugs}
    # dedupe 없이 모든 3 CQ id 포함되는지 확인
    assert prev_ids == {"CQ-01", "CQ-02", "CQ-03"}

    # load_active_suggestions 는 1개만 (dedupe)
    from tools.cq_feedback import load_active_suggestions
    active = load_active_suggestions()
    assert len(active) == 1  # dedupe 작동 확인


def test_mixed_suggestion_types_all_rendered(isolated_feedback):
    """세 가지 type 이 섞여 있어도 프롬프트에 모두 렌더."""
    from tools import cq_feedback

    cq_feedback.append_iteration(
        pass_rate=40.0, total_cqs=10, passed=4, failed=6,
        suggestions=[
            {"cq_id": "CQ-01", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
            {"cq_id": "CQ-02", "type": "missing_instances",
             "class": "C", "action": "..."},
            {"cq_id": "CQ-03", "type": "missing_dp_values",
             "class": "D", "action": "..."},
        ],
    )
    active = cq_feedback.load_active_suggestions()
    prompt = cq_feedback.format_feedback_for_prompt(active, pass_rate=40.0)
    # 세 유형 헤더 모두
    assert "### 누락된 OP 연결" in prompt
    assert "### 인스턴스 누락" in prompt
    assert "### DP 값 없음" in prompt
    # 각 대상 클래스 포함
    assert "A ↔ B" in prompt
    assert "- C " in prompt
    assert "- D " in prompt
