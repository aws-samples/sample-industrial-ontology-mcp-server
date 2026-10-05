"""tools/cq_feedback.py 단위 테스트 — T3 CQ 실패 피드백 루프.

CAUTION: ``test_cq_feedback.py`` 는 별개 기능 (S4 CQ coverage check) 의
테스트이므로 혼동 방지를 위해 이 파일 이름은 ``test_cq_feedback_loop.py``
로 둔다.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def tmp_feedback_path(tmp_path, monkeypatch):
    """Isolate feedback file per test by monkeypatching CQ_FEEDBACK_PATH."""
    path = str(tmp_path / "cq_feedback.json")
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", path)
    return path


def test_append_creates_file(tmp_feedback_path):
    from tools.cq_feedback import _load_feedback, append_iteration

    append_iteration(
        pass_rate=60.0,
        total_cqs=10,
        passed=6,
        failed=4,
        suggestions=[
            {
                "cq_id": "CQ-01",
                "type": "missing_connection",
                "class_a": "A",
                "class_b": "B",
                "action": "...",
            }
        ],
    )
    assert os.path.exists(tmp_feedback_path)
    data = _load_feedback()
    assert len(data["iterations"]) == 1
    assert data["iterations"][0]["pass_rate"] == 60.0
    assert data["iterations"][0]["suggestions"][0]["age_iterations"] == 1


def test_age_iterations_increments(tmp_feedback_path):
    from tools.cq_feedback import _load_feedback, append_iteration

    sug = {
        "cq_id": "CQ-01",
        "type": "missing_connection",
        "class_a": "A",
        "class_b": "B",
        "action": "...",
    }
    append_iteration(pass_rate=50, total_cqs=10, passed=5, failed=5, suggestions=[dict(sug)])
    append_iteration(pass_rate=55, total_cqs=10, passed=5, failed=5, suggestions=[dict(sug)])
    append_iteration(pass_rate=60, total_cqs=10, passed=6, failed=4, suggestions=[dict(sug)])

    data = _load_feedback()
    ages = [it["suggestions"][0]["age_iterations"] for it in data["iterations"]]
    assert ages == [1, 2, 3]


def test_age_out_old_iterations(tmp_feedback_path):
    from tools.cq_feedback import _MAX_AGE_DAYS, _load_feedback, _save_feedback

    old_ts = (datetime.now() - timedelta(days=_MAX_AGE_DAYS + 1)).isoformat()
    new_ts = datetime.now().isoformat()
    data = {
        "version": 1,
        "iterations": [
            {"timestamp": old_ts, "suggestions": [{"cq_id": "OLD"}]},
            {"timestamp": new_ts, "suggestions": [{"cq_id": "NEW"}]},
        ],
    }
    _save_feedback(data)
    reloaded = _load_feedback()
    assert len(reloaded["iterations"]) == 1
    assert reloaded["iterations"][0]["suggestions"][0]["cq_id"] == "NEW"


def test_max_iterations_bound(tmp_feedback_path):
    from tools.cq_feedback import _MAX_ITERATIONS, _load_feedback, append_iteration

    for i in range(_MAX_ITERATIONS + 5):
        append_iteration(
            pass_rate=float(i * 5),
            total_cqs=10,
            passed=i,
            failed=10 - i,
            suggestions=[
                {
                    "cq_id": f"CQ-{i}",
                    "type": "missing_connection",
                    "class_a": "A",
                    "class_b": "B",
                    "action": "...",
                }
            ],
        )
    data = _load_feedback()
    assert len(data["iterations"]) == _MAX_ITERATIONS
    # Newest entries retained (pass_rate increases with i)
    assert data["iterations"][-1]["pass_rate"] == float((_MAX_ITERATIONS + 4) * 5)


def test_load_active_suggestions_dedupe(tmp_feedback_path):
    from tools.cq_feedback import append_iteration, load_active_suggestions

    append_iteration(
        pass_rate=60,
        total_cqs=10,
        passed=6,
        failed=4,
        suggestions=[
            {"cq_id": "CQ-01", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
            {"cq_id": "CQ-02", "type": "missing_connection",
             "class_a": "A", "class_b": "B", "action": "..."},
            {"cq_id": "CQ-03", "type": "missing_instances",
             "class": "C", "action": "..."},
        ],
    )
    active = load_active_suggestions()
    # (missing_connection, A, B) duplicate collapses to 1 → total 2
    assert len(active) == 2
    keys = {
        (s.get("type"), s.get("class_a", s.get("class", "")), s.get("class_b", ""))
        for s in active
    }
    assert keys == {
        ("missing_connection", "A", "B"),
        ("missing_instances", "C", ""),
    }


def test_empty_feedback_returns_empty_list(tmp_feedback_path):
    from tools.cq_feedback import load_active_suggestions

    assert load_active_suggestions() == []


def test_format_feedback_empty():
    from tools.cq_feedback import format_feedback_for_prompt

    assert format_feedback_for_prompt([]) == ""
    assert format_feedback_for_prompt([], pass_rate=None) == ""


def test_format_feedback_with_content():
    from tools.cq_feedback import format_feedback_for_prompt

    sugs = [
        {
            "cq_id": "CQ-01",
            "type": "missing_connection",
            "class_a": "A",
            "class_b": "B",
            "action": "...",
            "age_iterations": 1,
        },
        {
            "cq_id": "CQ-02",
            "type": "missing_instances",
            "class": "C",
            "action": "...",
            "age_iterations": 3,
        },
    ]
    out = format_feedback_for_prompt(sugs, pass_rate=60.0)
    assert "60.0%" in out
    assert "A ↔ B" in out
    assert "⚠️반복" in out  # age >= 3 badge
    # pass_rate=None skips the pass-rate line but still renders content
    out_no_rate = format_feedback_for_prompt(sugs, pass_rate=None)
    assert "통과율" not in out_no_rate
    assert "A ↔ B" in out_no_rate


def test_malformed_file_returns_default(tmp_feedback_path):
    """Corrupted JSON must yield empty default — never block the pipeline."""
    from tools.cq_feedback import _load_feedback

    os.makedirs(os.path.dirname(tmp_feedback_path), exist_ok=True)
    with open(tmp_feedback_path, "w", encoding="utf-8") as f:
        f.write("{ not valid json")
    data = _load_feedback()
    assert data == {"version": 1, "iterations": []}


def test_atomic_write_no_tmp_left_behind(tmp_feedback_path):
    """Successful save leaves no .tmp residue alongside the final file."""
    from tools.cq_feedback import append_iteration

    append_iteration(
        pass_rate=70.0,
        total_cqs=10,
        passed=7,
        failed=3,
        suggestions=[],
    )
    assert os.path.exists(tmp_feedback_path)
    assert not os.path.exists(tmp_feedback_path + ".tmp")


# ---------------------------------------------------------------------------
# Circuit breaker + stubborn drop (T3 리뷰 Important #1)
# ---------------------------------------------------------------------------

def test_circuit_breaker_no_improvement(tmp_feedback_path):
    """3 연속 iteration 에서 pass_rate 가 개선되지 않으면 빈 리스트."""
    from tools.cq_feedback import append_iteration, load_active_suggestions

    sug = {"cq_id": "CQ-01", "type": "missing_connection",
           "class_a": "A", "class_b": "B", "action": "..."}
    # pass_rate 60 → 55 → 60 (개선 없음, 최신이 min 과 같음)
    for rate in [60.0, 55.0, 60.0]:
        append_iteration(pass_rate=rate, total_cqs=10, passed=int(rate / 10),
                         failed=10 - int(rate / 10), suggestions=[dict(sug)])

    active = load_active_suggestions()
    assert active == [], "circuit breaker tripped"


def test_circuit_breaker_improvement_continues(tmp_feedback_path):
    """pass_rate 가 개선 추세면 suggestion 계속 주입."""
    from tools.cq_feedback import append_iteration, load_active_suggestions

    sug = {"cq_id": "CQ-01", "type": "missing_connection",
           "class_a": "A", "class_b": "B", "action": "..."}
    for rate in [60.0, 65.0, 70.0]:
        append_iteration(pass_rate=rate, total_cqs=10, passed=int(rate / 10),
                         failed=10 - int(rate / 10), suggestions=[dict(sug)])

    active = load_active_suggestions()
    assert len(active) == 1  # 계속 주입


def test_stubborn_suggestion_dropped(tmp_feedback_path):
    """age_iterations >= 5 인 suggestion 은 제외."""
    from tools.cq_feedback import append_iteration, load_active_suggestions

    sug_old = {"cq_id": "CQ-01", "type": "missing_connection",
               "class_a": "A", "class_b": "B", "action": "...old"}
    sug_new = {"cq_id": "CQ-02", "type": "missing_connection",
               "class_a": "X", "class_b": "Y", "action": "...new"}

    # 5회 연속 같은 suggestion 등록 — age_iterations 가 5 로 증가
    # pass_rate 는 개선 추세 (circuit breaker 회피)
    for i, rate in enumerate([50.0, 55.0, 60.0, 65.0, 70.0]):
        sugs = [dict(sug_old)]
        if i == 4:
            sugs.append(dict(sug_new))  # 마지막 iteration 에만 새 suggestion
        append_iteration(pass_rate=rate, total_cqs=10, passed=int(rate / 10),
                         failed=10 - int(rate / 10), suggestions=sugs)

    active = load_active_suggestions()
    # sug_old 는 age=5 라 drop, sug_new 만 남음
    active_ids = [a["cq_id"] for a in active]
    assert "CQ-02" in active_ids
    assert "CQ-01" not in active_ids
