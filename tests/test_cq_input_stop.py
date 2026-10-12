"""S2 입력 오류를 수정 가능한 갭으로 오인하거나 합의로 처리하지 않는지 검증한다."""

import copy
import json
import time
from unittest.mock import Mock

import pytest
from rdflib import Graph, Literal

from domain.namespaces import DOMAIN_NS
from tools import competency_questions as cq
from tools import debate_log_store as logs
from tools import multi_agent_tbox as s2

_TTL = f"""
@prefix ex: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Machine a owl:Class .
ex:Part a owl:Class .
ex:Order a owl:Class .
ex:hasPart a owl:ObjectProperty ; rdfs:domain ex:Machine ; rdfs:range ex:Part .
"""
_GOOD = {"id": "good", "question_ko": "설비와 부품", "domains": ["Machine", "Part"]}
_BAD = {"id": "bad", "question_ko": "입력 정정 필요", "domains": False}
_REVIEW = {"approved": True, "issues": [], "summary": "검토 완료"}
_BLOCK = {"severity": "critical", "category": "logic", "target": "Order", "fix": "관계 방향 재검토"}


@pytest.fixture
def run_round(tmp_path, monkeypatch):
    """실제 CQ 평가·토론 제어·파일 저장을 사용하고 모델 호출만 대체한다."""
    path = tmp_path / "cq.json"
    monkeypatch.setattr(s2, "_CQ_PATH", str(path))
    monkeypatch.setattr(s2, "TBOX_PATH", str(tmp_path / "tbox.ttl"))
    monkeypatch.setattr(s2, "TBOX_BASELINE_PATH", str(tmp_path / "baseline.ttl"))
    monkeypatch.setattr(logs, "DEBATE_LOG_PATH", str(tmp_path / "debate.json"))
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", str(tmp_path / "feedback.json"))
    monkeypatch.setattr(cq, "_load_csv_summary", lambda: "")
    monkeypatch.setattr(cq, "_load_table_class_map", lambda: {})
    monkeypatch.setattr(s2, "_generate_tbox_skeleton", lambda: "")
    monkeypatch.setattr(s2, "_load_anomaly_hints", lambda: "")
    monkeypatch.setattr(s2, "_analyze_cq_required_ops", lambda *_a: "")
    monkeypatch.setattr(s2, "_compute_metrics_feedback", lambda *_a: "")
    monkeypatch.setattr(s2, "_compute_tbox_metrics", lambda *_a: {})
    monkeypatch.setattr(s2, "_compute_round_quality_metrics", lambda *_a: {})
    monkeypatch.setattr(s2, "_csv_fk_pairs", lambda: {("machine", "order")})
    monkeypatch.setattr(s2, "_record_s2_quality_history", lambda *_a: None)
    # veto 전이 자체는 기존 테스트가 맡고, 여기서는 활성 veto의 존중을 확인한다.
    monkeypatch.setattr(s2, "_detect_and_record_veto", lambda *_a: None)
    monkeypatch.setattr("tools.tbox_generation.generate_tbox", lambda *_a, **_k: json.dumps({
        "success": True, "ttl": _TTL,
        "statistics": {"classes": 3, "object_properties": 1, "data_properties": 0},
        "timing": {"total": 0},
    }))
    revise = Mock(return_value=_TTL)
    jury = Mock(return_value={"production_ready": True, "required_fixes": [], "decisions": []})
    monkeypatch.setattr(s2, "_architect_revise", revise)
    monkeypatch.setattr(s2, "_jury_decide", jury)
    monkeypatch.setattr("tools.jury_fixes.apply_jury_fixes", lambda ttl, *_a: {
        "ttl": ttl, "applied": [], "skipped": [], "failed": [], "noop": [],
    })

    def run(entries=None, *, validator=None, sme=None, round_num=1, max_rounds=3, veto=False):
        data = copy.deepcopy(entries if entries is not None else [_GOOD, _BAD])
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        state = s2._build_initial_draft("")
        state["max_rounds_planned"] = max_rounds
        state["veto_lock_triggered"] = veto
        state["veto_persistent_targets"] = ["Order"] if veto else []
        v = copy.deepcopy(_REVIEW if validator is None else validator)
        m = copy.deepcopy(_REVIEW if sme is None else sme)
        monkeypatch.setattr(s2, "_run_validator_and_sme", lambda *_a, **_k: (v, m))
        summary = s2._run_one_debate_round(state, round_num, max_rounds)
        assert path.read_bytes() == before
        return state, summary, revise, jury

    return run


@pytest.mark.parametrize("round_num", [1, 3])
@pytest.mark.parametrize("expressibility_stop", ["on", "off"])
def test_only_input_errors_stop_without_more_model_work(run_round, monkeypatch, round_num, expressibility_stop):
    """첫 라운드·최종 라운드 모두 입력 오류만 남으면 재수정과 Jury 없이 끝낸다."""
    monkeypatch.setenv("S2_EARLY_STOP", expressibility_stop)
    state, summary, revise, jury = run_round(round_num=round_num)
    assert summary["break_loop"] is True
    assert summary["consensus"] is False
    assert summary["stop_reason"] == state["early_stop_reason"] == "invalid_cq_input"
    log = summary["round_log"]
    assert log["cq_runtime"]["total"] == 2
    assert log["cq_runtime"]["coverage_pct"] == 50.0
    assert log["cq_block_input_error"][0]["source_index"] == 2
    assert not log.get("cq_block_fixable") and not log.get("cq_block_data_gap")
    assert log["early_stop"]["reason"] == "invalid_cq_input"
    assert state["current_ttl"] == _TTL
    assert not revise.called and not jury.called


def test_runtime_and_sme_gaps_are_merged_without_id_only_deduplication(run_round):
    """입력 오류와 ID가 같은 정상 갭 및 SME 단독 근거를 모두 유지한다."""
    data_gap = ["SME-DATA", "part → order 경로 없음 (2홉 내 OP 연결 부재)"]
    opaque = "SME 미확인 관계를 검토해야 함"
    entries = [
        _GOOD,
        {**_BAD, "id": "duplicate"},
        {"id": "duplicate", "question_ko": "설비와 주문", "domains": ["Machine", "Order"]},
    ]
    state, summary, _, _ = run_round(entries, sme={
        **_REVIEW, "unanswerable_cqs": [data_gap, opaque, data_gap],
    })
    log = summary["round_log"]
    assert summary["break_loop"] is False
    assert not state["consensus_reached"]
    assert log["cq_runtime"]["total"] == 3
    assert log["cq_runtime"]["coverage_pct"] == 33.3
    assert log["cq_block_input_error"][0]["cq_id"] == "duplicate"
    assert len(log["cq_block_fixable"]) == 2
    assert any(
        isinstance(gap, list | tuple) and gap[0] == "duplicate"
        and gap[1].startswith("machine → order 경로 없음")
        for gap in log["cq_block_fixable"]
    )
    assert opaque in log["cq_block_fixable"]
    assert log["cq_block_data_gap"] == [data_gap]
    assert state["prev_cq_data_gap"] == [data_gap]


def test_input_errors_do_not_hide_sme_only_blocker(run_round):
    """runtime에 입력 오류만 있어도 SME의 수정 가능한 근거가 분류에 남는다."""
    _, summary, revise, _ = run_round(sme={**_REVIEW, "unanswerable_cqs": ["SME unresolved CQ"]})
    assert summary["break_loop"] is False
    assert summary["round_log"]["cq_block_fixable"] == ["SME unresolved CQ"]
    assert revise.called


@pytest.mark.parametrize("review", [
    {"approved": False, "issues": [], "parse_error": True},
    {},
    {"approved": False, "issues": [_BLOCK]},
    {"approved": True, "issues": [_BLOCK]},
])
@pytest.mark.parametrize("role", ["validator", "sme"])
def test_uncertain_or_blocking_review_does_not_stop_as_input_only(run_round, review, role):
    """파싱 실패·불완전 리뷰·실제 차단 이슈를 입력 오류만의 종료로 오인하지 않는다."""
    state, summary, _, _ = run_round(**{role: review})
    assert summary["break_loop"] is False
    assert state["early_stop_reason"] != "invalid_cq_input"
    assert summary["consensus"] is False


def test_veto_prevents_input_only_early_stop(run_round):
    """기존 veto를 입력 오류 조기 종료가 우회하지 않는다."""
    state, summary, _, _ = run_round(veto=True)
    assert summary["break_loop"] is False
    assert state["veto_lock_triggered"]
    assert state["early_stop_reason"] != "invalid_cq_input"


def test_error_free_consensus_path_remains_available(run_round):
    """입력 오류가 없으면 기존 최소 라운드 이후 Jury 합의가 유지된다."""
    state, summary, revise, jury = run_round([_GOOD], round_num=2)
    assert summary["break_loop"] and summary["consensus"]
    assert state["consensus_reached"]
    assert not state.get("early_stop_reason")
    assert jury.called and not revise.called


@pytest.mark.parametrize("with_fixable_gap", [False, True])
def test_saved_result_marks_input_required_even_after_max_rounds(run_round, with_fixable_gap):
    """입력 오류가 있는 저장 결과는 정상 완료나 합의로 표시되지 않는다."""
    entries = [_GOOD, _BAD]
    if with_fixable_gap:
        entries.append({"id": "gap", "domains": ["Machine", "Order"], "question_ko": "설비와 주문"})
    state, _, _, _ = run_round(entries, round_num=3)
    result = json.loads(s2._finalize_and_save(state, time.monotonic()))
    assert result["success"] is False
    assert result["partial"] is True
    assert result["needs_input"] is True
    assert result["input_errors"][0]["source_index"] == 2
    assert result["error"] and result["hint"]
    assert result["debate"]["consensus_reached"] is False
    graph = Graph().parse(result["saved_to"], format="turtle")
    status = [str(obj) for obj in graph.objects() if isinstance(obj, Literal) and str(obj).startswith("multi_agent_debate:")]
    assert len(status) == 1
    assert "consensus_reached=false" in status[0]
    if not with_fixable_gap:
        assert "debate_status=debate_stopped_invalid_cq_input" in status[0]
        assert "debate_stopped_no_expressible_fix" not in status[0]
    persisted = logs.summarize_latest_run()
    assert persisted["rounds"][-1]["cq_block_input_error"] == 1
    if not with_fixable_gap:
        assert persisted["rounds"][-1]["early_stop_reason"] == "invalid_cq_input"
