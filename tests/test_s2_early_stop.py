"""S2 조기 종료 — 고칠 수 있는 것이 없으면 라운드를 태우지 않는다 (Fix 5).

2026-08-30 실측. S2 는 8런 23,851초(6.63시간) 중 **88.4%를 토론에 소비**했고
30라운드 전체에서 ``approved=true`` 가 0건이었다. 라운드당 평균 711초.

## 왜 "고정 2라운드 상한" 이 아닌가

기각했다. run8 은 CQ 커버리지가 3라운드째 75.0 → 83.3 으로 올랐고 run2/run3 의
상승도 후반 라운드였다 — 라운드의 이득은 0이 아니라 **비결정적**이다. 그래서
이득이 **구조적으로 불가능한** 라운드만 자른다.

## 이 게이트는 보수적이다 (의도)

``actionable == 0`` 즉 실행 가능한 수정도 없고 번역 가능한 자연어 지적도 없는
상태에서만 멈춘다. 배포 debate_log 8런 30라운드에 이 조건을 적용하면 **0회**
발동한다 — 자연어 지적(``unspecified``)이 항상 남아 있었기 때문이다.

그것을 "게이트가 쓸모없다" 로 읽지 말 것. 두 가지 이유로 유지한다:

1. Fix 4 로 리뷰어가 SME 소유 이슈를 critical 로 올리지 않게 됐으므로 앞으로의
   런에서는 차단 이슈 구성이 달라진다 (실측 분포 기준 34%가 빠진다).
2. ``unspecified`` 를 actionable 로 세는 것이 **안전 쪽**이다. 그것을 "고칠 수
   없음" 으로 세면 자연어 지적만 남은 라운드를 잘라버려 진짜 이득을 잃는다.

느슨하게 만들어 발동 횟수를 늘리는 것은 **지표 매수**다. 그래서 합성 픽스처로
논리를 고정하고, 실측 분포는 별도로 기록한다.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tools.multi_agent_tbox import (
    _MIN_ROUNDS_BEFORE_EARLY_STOP,
    _build_debate_status_literal,
    _measure_expressibility,
    _should_stop_no_expressible,
)

DEBATE_LOG = pathlib.Path("data/generated/tbox/debate_log.json")


def _sme_issue(n: int = 1) -> list[dict]:
    return [
        {"severity": "critical", "category": "logic",
         "target": f"AllDisjointClasses 축 혼재 {i}", "fix": "그룹을 분리하라"}
        for i in range(n)
    ]


def _executable_issue(n: int = 1) -> list[dict]:
    return [
        {"severity": "critical", "category": "logic", "target": f"OP {i}",
         "fix": "add_object_property 로 추가하라"}
        for i in range(n)
    ]


def _verify_issue(n: int = 1) -> list[dict]:
    return [
        {"severity": "high", "category": "mapping", "target": f"FK {i}",
         "fix": "verify_fk_mapping 로 확인하라"}
        for i in range(n)
    ]


# ── 계측 ────────────────────────────────────────────────────────────────


def test_measure_splits_blocking_by_expressibility():
    result = _measure_expressibility(_sme_issue(3) + _executable_issue(2), [])

    assert result["measured"] is True
    assert result["blocking_total"] == 5
    assert result["sme_owned"] == 3
    assert result["executable"] == 2
    assert result["actionable"] == 2


def test_unspecified_counts_as_actionable():
    """자연어 지적은 Architect 가 번역할 수 있다 — 보수적으로 actionable 이다.

    이것을 '고칠 수 없음' 으로 세면 조기 종료가 과감해져 진짜 이득을 잃는다.
    """
    natural = [{"severity": "critical", "target": "X", "fix": "관계 방향을 재검토"}]

    result = _measure_expressibility(natural, [])

    assert result["unspecified"] == 1
    assert result["actionable"] == 1


def test_measure_survives_per_issue_classification_failure(monkeypatch):
    """분류기가 이슈 하나에서 터져도 계측이 예외를 내지 않는다.

    그리고 실패한 이슈는 ``unspecified`` = actionable 로 세야 한다 — "분류 못 했다"
    를 "고칠 수 없다" 로 읽으면 라운드를 잘못 자른다 (안전 방향).
    """
    import tools.action_registry as areg

    def _boom(_issue):
        raise RuntimeError("boom")

    monkeypatch.setattr(areg, "classify_requirement", _boom)

    result = _measure_expressibility(_sme_issue(2), [])

    assert result["blocking_total"] == 2
    assert result["unspecified"] == 2
    assert result["actionable"] == 2
    # 그 상태에서는 조기 종료가 발동하지 않는다.
    assert _should_stop_no_expressible({}, result, 3, {}) is False


# ── 발동: 고칠 수 있는 것이 없을 때 ─────────────────────────────────────


def test_stops_when_all_blocking_is_sme_owned():
    """THE REGRESSION: SME 소유 이슈만 남으면 멈춘다."""
    expressibility = _measure_expressibility(_sme_issue(3), [])
    state: dict = {}
    round_log: dict = {}

    stopped = _should_stop_no_expressible(state, expressibility, 3, round_log)

    assert stopped is True
    assert state["early_stop_reason"] == "no_expressible_blocking_fix"
    assert round_log["early_stop"]["reason"] == "no_expressible_blocking_fix"


def test_stops_when_only_verify_requests_remain():
    """``verify_*`` 는 편집이 아니다 — 그것만 남으면 Architect 가 할 일이 없다."""
    expressibility = _measure_expressibility(_verify_issue(2), [])

    assert _should_stop_no_expressible({}, expressibility, 2, {}) is True


# ── 비발동 (NEGATIVE 방향 — 이쪽이 더 중요하다) ─────────────────────────


def test_does_not_stop_when_any_fix_is_executable():
    """실행 가능한 수정이 1건이라도 있으면 계속 돈다."""
    expressibility = _measure_expressibility(
        _sme_issue(10) + _executable_issue(1), [],
    )

    assert _should_stop_no_expressible({}, expressibility, 3, {}) is False


def test_does_not_stop_when_only_natural_language_remains():
    """자연어 지적이 남아 있으면 번역 가능성이 있으므로 계속 돈다."""
    natural = [{"severity": "critical", "target": "X", "fix": "방향을 재검토"}]
    expressibility = _measure_expressibility(natural, [])

    assert _should_stop_no_expressible({}, expressibility, 3, {}) is False


def test_does_not_stop_before_minimum_rounds():
    """첫 라운드 리뷰는 TTL 발췌 기반이라 판정 근거가 약하다."""
    expressibility = _measure_expressibility(_sme_issue(3), [])

    assert _should_stop_no_expressible(
        {}, expressibility, _MIN_ROUNDS_BEFORE_EARLY_STOP - 1, {},
    ) is False


def test_does_not_stop_when_no_blocking_issues():
    """차단 이슈가 없으면 합의 분기가 판정한다 — 여기서 가로채지 않는다."""
    expressibility = _measure_expressibility([], [])

    assert _should_stop_no_expressible({}, expressibility, 3, {}) is False


def test_can_be_disabled_by_env(monkeypatch):
    """A/B 비교용 스위치 — 끄면 절대 발동하지 않는다."""
    monkeypatch.setenv("S2_EARLY_STOP", "off")
    expressibility = _measure_expressibility(_sme_issue(3), [])

    assert _should_stop_no_expressible({}, expressibility, 3, {}) is False


# ── 산출물 각인 ────────────────────────────────────────────────────────


def test_early_stop_is_recorded_in_debate_status():
    """조기 종료 산출물과 max_rounds 소진 산출물이 구분되는가.

    구분되지 않으면 "고칠 수 없어 2라운드에서 끝난" 것과 "5라운드 돌고도
    미합의인" 것이 바이트 단위로 같아진다 — 신뢰도가 전혀 다르다.
    """
    stopped = str(_build_debate_status_literal(
        consensus_reached=False, veto_lock_triggered=False, veto_targets=[],
        total_rounds=3, early_stop_reason="no_expressible_blocking_fix",
    ))
    exhausted = str(_build_debate_status_literal(
        consensus_reached=False, veto_lock_triggered=False, veto_targets=[],
        total_rounds=5,
    ))

    assert "debate_stopped_no_expressible_fix" in stopped
    assert "early_stop_reason=no_expressible_blocking_fix" in stopped
    assert "debate_unresolved_max_rounds" in exhausted
    assert stopped != exhausted


def test_infra_abort_outranks_early_stop():
    """인프라 장애가 조기 종료보다 우선한다 — 그쪽이 신뢰도에 더 중요하다."""
    literal = str(_build_debate_status_literal(
        consensus_reached=False, veto_lock_triggered=False, veto_targets=[],
        total_rounds=2, infra_abort={"phase": "architect", "round": 2},
        early_stop_reason="no_expressible_blocking_fix",
    ))

    assert "debate_status=aborted_infra_error" in literal


def test_consensus_outranks_early_stop():
    literal = str(_build_debate_status_literal(
        consensus_reached=True, veto_lock_triggered=False, veto_targets=[],
        total_rounds=3, early_stop_reason="no_expressible_blocking_fix",
    ))

    assert "debate_status=consensus_reached" in literal


# ── 실측 분포 기록 ─────────────────────────────────────────────────────


def test_recorded_runs_distribution_is_documented():
    """배포 debate_log 에서 이 게이트가 몇 번 발동하는지 **기록**한다.

    현재 0회다 (모듈 docstring 참조). 이 검사는 발동을 요구하지 않는다 — 요구하면
    게이트를 느슨하게 만들려는 압력이 생기고 그것이 지표 매수다. 대신 계측이
    실제 데이터에서 **동작하는지** 확인한다.
    """
    if not DEBATE_LOG.exists():
        pytest.skip("debate_log 없음")

    from tools.multi_agent_tbox import _blocking_issues

    log = json.loads(DEBATE_LOG.read_text(encoding="utf-8"))
    rounds_with_blocking = 0
    measured_rounds = 0
    for run in log.get("runs", []):
        for round_log in run.get("rounds", []):
            expressibility = _measure_expressibility(
                _blocking_issues(round_log.get("validator") or {}),
                _blocking_issues(round_log.get("sme") or {}),
            )
            if not expressibility["blocking_total"]:
                continue
            rounds_with_blocking += 1
            if expressibility["measured"]:
                measured_rounds += 1

    # 2026-09-05: 예전 고정값 ``>= 20`` 은 **로그 보관량에 묶인 핀** 이었다. S2 재실행
    # 후 로그가 4 run × 4 라운드 = 16 이 되어 실패했는데, 계측률은 16/16 = 100% 였다
    # (분류기는 정상). 이 검사의 의도는 "계측이 실제 데이터에서 동작하는가" 이므로
    # 총량이 아니라 **비율** 로 주장한다 — 총량은 debate_log 가 몇 run 을 남기느냐에
    # 따라 달라진다.
    if rounds_with_blocking == 0:
        pytest.skip("배포 debate_log 에 차단 이슈가 있는 라운드가 없다")
    assert measured_rounds == rounds_with_blocking, (
        f"차단 이슈가 있는 라운드 {rounds_with_blocking}개 중 {measured_rounds}개만 "
        f"계측됐다 — 분류기가 실제 이슈 형태를 못 읽고 있다"
    )
    # 빈/한 줄 로그로 공허하게 통과하지 않도록 최소 표본만 요구한다 (한 run 분).
    assert rounds_with_blocking >= 4, (
        f"표본이 {rounds_with_blocking} 라운드뿐 — 계측 동작을 판정할 근거가 얇다"
    )
