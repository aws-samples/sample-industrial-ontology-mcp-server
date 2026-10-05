"""S2 토론 중 인프라 장애가 검토된 산출물을 파괴하지 않는다.

2026-08-09 규명: Jury 실패를 fail-closed 로 고친 뒤에도 (커밋 9b3b0a6) 바로 뒤에
오는 LLM 호출이 **보호되지 않아** 같은 throttling 으로 예외가 라운드를 탈출했다.
그러면 ``_finalize_and_save`` 가 아예 실행되지 않는데, ``generate_tbox`` 가 토론
시작 **전에** ``TBOX_PATH`` 를 무조건 덮어써 놓았기 때문에 디스크에는 **검토를
마친 이전 T-Box 대신 이번 실행의 미검토 초안** 이 남는다. ``t_box_baseline.ttl`` 도
두 writer 모두 ``if not exists`` 라 최초 실행에 동결돼 있어 롤백 경로가 아니다.
40~50분 토론 결과가 조용히 downgrade 되는 것이다.

무방비 호출은 세 곳이었다 (모두 fail-closed 직후 경로에 있다):
``_run_validator_and_sme`` / ``_architect_revise`` / ``_architect_compromise``.

이 파일이 주장하는 계약:

- POSITIVE: 인프라 장애면 지금까지의 TTL 을 **저장하고** 토론을 끝낸다.
- 정직성: 저장물에 ``debate_status=aborted_infra_error`` 가 각인되고
  ``consensus_reached`` 는 여전히 false 다 (fail-closed 유지).
- NEGATIVE: **코드 버그** (``KeyError`` 등) 와 ``MemoryError`` 는 전파된다 —
  "합의 실패로 저장됨" 으로 숨기면 영구히 안 보인다.
- FLOOR: 라운드 1의 리뷰조차 못 했으면 (``debate_log`` 비어 있음) 저장할 "검토된"
  산출물이 없으므로 전파한다.
- PRESERVATION: 정상 라운드와 Jury-만-실패 경로는 동작이 바뀌지 않는다.
"""
from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

import tools.multi_agent_tbox as mt

_THROTTLE = ClientError(
    {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}},
    "InvokeModel",
)
_REVIEW = {"issues": [], "approved": False}


def _base_ttl() -> str:
    return (
        f"@prefix steel: <{mt.DOMAIN_NS}> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "steel:A a owl:Class .\nsteel:B a owl:Class .\n"
        "steel:op a owl:ObjectProperty ; rdfs:domain steel:A ; rdfs:range steel:B .\n"
    )


def _state(*, reviewed: bool = True) -> dict:
    """토론 진행 중 state. ``reviewed=False`` 면 아직 리뷰 기록이 없는 상태."""
    return {
        "current_ttl": _base_ttl(),
        "architect_stats": {}, "csv_summary": "", "cqs": [], "extra_context": "",
        "debate_log": [{"round": 1, "validator": {}, "sme": {}}] if reviewed else [],
        "consensus_reached": False, "compromise_reason": None,
        "prev_validator_issues": [], "prev_sme_issues": [],
        "prev_round_ttl": _base_ttl(),
        "veto_lock_triggered": False, "veto_persistent_targets": [],
        "initial_draft_rounds": 1, "infra_abort": None,
    }


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """세션 산출물 보호 — 저장 경로를 tmp 로 격리한다.

    라운드별 품질 메트릭도 무력화한다: ``_compute_round_quality_metrics`` →
    ``evaluate_farber`` 는 인자 없이 호출되면 **실 A-Box(40MB)** 를 함께 파싱해
    한 라운드당 ~32초를 쓴다. 이 파일의 관심사는 예외 경로이므로 그 계산은
    무관하고, 세션 산출물을 읽지 않는 것이 격리 원칙에도 맞다.
    """
    tbox = tmp_path / "t_box.ttl"
    monkeypatch.setattr(mt, "TBOX_PATH", str(tbox))
    monkeypatch.setattr(mt, "TBOX_BASELINE_PATH", str(tmp_path / "t_box_baseline.ttl"))
    monkeypatch.setattr(mt, "_compute_round_quality_metrics", lambda *a, **k: {})
    return tbox


@pytest.fixture()
def stub_reviews(monkeypatch):
    monkeypatch.setattr(
        mt, "_run_validator_and_sme",
        lambda *a, **k: (dict(_REVIEW), dict(_REVIEW)),
    )


def _raise(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


# ── POSITIVE: 산출물이 저장된다 ───────────────────────────────────────


def test_architect_throttle_saves_the_reviewed_artifact(
    isolated_paths, stub_reviews, monkeypatch,
):
    """THE REGRESSION: architect 호출이 throttle 돼도 산출물이 저장된다."""
    monkeypatch.setattr(mt, "_architect_revise", _raise(_THROTTLE))
    state = _state()

    summary = mt._run_one_debate_round(state, 1, 3)

    assert summary["break_loop"] is True, "토론을 끝내지 않으면 같은 장애로 재진입한다"
    assert summary["consensus"] is False, "fail-closed 가 깨졌다"
    result = json.loads(mt._finalize_and_save(state, 0.0))
    assert isolated_paths.exists(), "산출물이 저장되지 않았다 (검토된 T-Box 유실)"
    assert "steel:op" in isolated_paths.read_text(encoding="utf-8"), (
        "직전 라운드의 TTL 이 보존되지 않았다"
    )
    assert result["partial"] is True
    assert result["infra_abort"]["phase"] == "architect_revise"
    assert result["debate"]["consensus_reached"] is False


def test_validator_throttle_saves_and_returns_a_complete_summary(
    isolated_paths, monkeypatch,
):
    """리뷰 호출 실패도 같은 계약. summary 는 **7개 키가 모두** 있어야 한다.

    호출부(``_generate_tbox_collaborative_async``)는 ``break_loop`` 를 검사하기
    **전에** v_issues_count / cq_coverage_pct 등을 읽어 하트비트 메시지를 만든다.
    부분 dict 를 돌려주면 그 지점에서 KeyError 가 나 같은 유실이 되살아난다.
    """
    monkeypatch.setattr(mt, "_run_validator_and_sme", _raise(_THROTTLE))
    state = _state()

    summary = mt._run_one_debate_round(state, 1, 3)

    for key in ("round_log", "break_loop", "consensus", "metrics_feedback",
                "v_issues_count", "s_issues_count", "v_approved", "s_approved",
                "cq_coverage_pct"):
        assert key in summary, f"summary 에 {key} 누락 — 호출부가 KeyError 를 낸다"
    assert state["infra_abort"]["phase"] == "validator_sme"
    mt._finalize_and_save(state, 0.0)
    assert isolated_paths.exists()


def test_abort_is_recorded_in_the_artifact_annotation(isolated_paths, stub_reviews,
                                                      monkeypatch):
    """인프라 중단 산출물은 정상 미합의 산출물과 구별돼야 한다.

    표기가 없으면 3라운드에서 끊긴 T-Box 와 5라운드를 다 소화하고 합의에 이르지
    못한 T-Box 가 바이트 단위로 동일해진다 — 신뢰도가 전혀 다른데도.
    """
    monkeypatch.setattr(mt, "_architect_revise", _raise(_THROTTLE))
    state = _state()
    mt._run_one_debate_round(state, 1, 3)
    mt._finalize_and_save(state, 0.0)

    saved = isolated_paths.read_text(encoding="utf-8")
    assert "debate_status=aborted_infra_error" in saved
    assert "aborted_phase=architect_revise" in saved
    assert "consensus_reached=false" in saved


# ── NEGATIVE: 삼켜서는 안 되는 실패 ──────────────────────────────────


@pytest.mark.parametrize("bug", [
    KeyError("veto_persistent_targets"),
    AttributeError("'NoneType' object has no attribute 'get'"),
    TypeError("unsupported operand"),
    MemoryError(),
])
def test_code_bugs_still_propagate(isolated_paths, stub_reviews, monkeypatch, bug):
    """계약 위반 버그와 MemoryError 는 전파된다.

    이것을 "인프라 장애" 로 묶어 삼키면 버그가 "합의 실패로 저장됨" 뒤에 영구히
    숨는다. ``MemoryError`` 는 ``Exception`` 하위라 특히 위험하다 — S2 는 peak RSS
    최대 단계이고, 반쯤 만들어진 그래프를 정상 산출물로 저장하면 안 된다.
    """
    monkeypatch.setattr(mt, "_architect_revise", _raise(bug))
    state = _state()

    with pytest.raises(type(bug)):
        mt._run_one_debate_round(state, 1, 3)
    assert not isolated_paths.exists(), "버그인데 산출물을 저장했다"


def test_round_one_abort_without_any_review_propagates(isolated_paths, monkeypatch):
    """FLOOR: 리뷰 기록이 0건이면 저장할 '검토된' 산출물이 없다.

    이때 저장하면 미검토 초안을 ``success`` 로 보고하게 되는데, 그것은 지금 고치는
    문제(검토된 산출물의 조용한 downgrade)보다 나쁘다.
    """
    monkeypatch.setattr(mt, "_run_validator_and_sme", _raise(_THROTTLE))
    state = _state(reviewed=False)

    with pytest.raises(ClientError):
        mt._run_one_debate_round(state, 0, 3)
    assert not isolated_paths.exists()


# ── PRESERVATION: 정상 경로는 그대로 ─────────────────────────────────


def test_healthy_round_is_unaffected(isolated_paths, stub_reviews, monkeypatch):
    """정상 라운드는 토론을 계속하고 abort 표기를 남기지 않는다."""
    monkeypatch.setattr(
        mt, "_architect_revise",
        lambda *a, **k: _base_ttl() + "steel:C a owl:Class .\n",
    )
    state = _state()

    summary = mt._run_one_debate_round(state, 1, 3)

    assert summary["break_loop"] is False, "정상 라운드를 중단시켰다"
    assert state["infra_abort"] is None
    assert summary["round_log"]["revision"]["valid"] is True
    result = json.loads(mt._finalize_and_save(state, 0.0))
    assert result["partial"] is False
    assert "debate_status=debate_unresolved_max_rounds" in (
        isolated_paths.read_text(encoding="utf-8")
    )


def test_jury_only_failure_keeps_its_existing_semantics(
    isolated_paths, stub_reviews, monkeypatch,
):
    """PRESERVATION: Jury 만 실패하는 경로는 커밋 9b3b0a6 의 계약을 유지한다.

    Jury fail-closed 자체는 아무것도 잃지 않는다 — 산출물이 저장되고
    ``consensus`` 는 false 다. 이번 수정이 그것을 "abort" 로 바꿔버리지 않아야
    한다 (모든 미합의를 인프라 오류로 표기하면 표기의 의미가 사라진다).
    """
    monkeypatch.setattr(mt, "_jury_decide", _raise(_THROTTLE))
    monkeypatch.setattr(
        mt, "_architect_revise",
        lambda *a, **k: _base_ttl() + "steel:D a owl:Class .\n",
    )
    monkeypatch.setattr(
        mt, "_run_validator_and_sme",
        lambda *a, **k: ({"issues": [], "approved": True},
                         {"issues": [], "approved": True}),
    )
    state = _state()

    summary = mt._run_one_debate_round(state, 2, 4)

    assert summary["consensus"] is False, "인프라 오류가 합의로 번역됐다"
    assert state["infra_abort"] is None, (
        "Jury 실패만으로 abort 표기를 남기면 안 된다 — 산출물 손실이 없는 경로다"
    )
    result = json.loads(mt._finalize_and_save(state, 0.0))
    assert result["partial"] is False
    assert isolated_paths.exists()


def test_compromise_failure_does_not_block_the_save_path(isolated_paths, monkeypatch):
    """최종 라운드에서 Jury·절충안이 **연속 실패** 해도 저장 경로가 살아있다.

    절충안 생성은 ``except`` 절 안에 있어, 거기서 raise 되면 그 뒤의
    ``break_loop=True`` 두 줄이 실행되지 않고 라운드를 탈출했다. 절충안은 감사
    기록일 뿐 TTL 을 바꾸지 않으므로 저장을 막을 이유가 없다.
    """
    monkeypatch.setattr(mt, "_jury_decide", _raise(_THROTTLE))
    monkeypatch.setattr(mt, "_architect_compromise", _raise(_THROTTLE))
    monkeypatch.setattr(
        mt, "_run_validator_and_sme",
        lambda *a, **k: (dict(_REVIEW), dict(_REVIEW)),
    )
    state = _state()

    summary = mt._run_one_debate_round(state, 3, 3)   # round_num == max_rounds

    assert summary["break_loop"] is True
    assert summary["consensus"] is False
    assert summary["round_log"].get("compromise_error"), (
        "절충안 실패가 기록되지 않았다"
    )
    mt._finalize_and_save(state, 0.0)
    assert isolated_paths.exists()
