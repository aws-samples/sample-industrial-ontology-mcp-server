"""Validator/SME JSON 파싱 실패가 라운드를 망치는 경로 회귀 가드.

2026-08-17 S2 실행(48분, 5라운드) 실측:

| 라운드 | Validator | SME | revision triples |
|---|---|---|---|
| 2 | 13 이슈 (critical/high 7) | 8 (8) | 3202 |
| 3 | 19 (12) | 8 (7) | 3222 |
| 4 | **0 — "파싱 실패 — 재검토 필요"** | 8 (7) | 3222 |
| 5 | 13 (9) | 8 (7) | 3222 |

R5 Validator: "4라운드에서도 T-Box 변경이 없어 이전 라운드 critical 이슈가 미해결
상태로 잔존". R4·R5 트리플이 동일하다 — Architect 가 아무것도 바꾸지 않았다.

## 파급 경로 (메인이 코드로 확인)

``_parse_review_json`` 이 최종 실패 시 센티넬을 반환한다 (:2056-2061):
``{"issues": [], "approved": False, "summary": "파싱 실패 — 재검토 필요",
"parse_error": True}``. ``approved: False`` 는 fail-closed 로 맞지만
``issues: []`` 가 세 곳을 망친다:

1. **Architect 가 지적을 못 본다** — ``_architect_revise`` 가
   ``validator_review["issues"]`` 를 직접 읽는다 (:1509). 빈 리스트면 수정 지시가
   사라지고, 그것이 R4·R5 무변경의 직접 원인이다.
2. **persistence 판정이 리셋된다** — ``:3586-3587`` 이 ``parse_error`` 여부와
   무관하게 스냅샷을 덮어쓴다. 다음 라운드의 ``_detect_persistent_veto`` 는
   ``if not prev_issues: return []`` (:2098-2099) 로 빠져 veto 를 못 잡는다.
   이슈 수가 13→19→0→13 으로 **진동** 한 메커니즘이다.
3. **debate_log 가 거짓 기록이 된다** — ``issues_count: 0`` 으로 남아 나중에
   "이 라운드는 깨끗했다" 로 오독된다.

``parse_error`` 를 검사하는 곳은 Jury 경로 **한 곳뿐** 이었다 (:3249).

## 재시도가 왜 못 살리는가

모든 재시도가 1차보다 **적은** ``max_tokens`` 를 쓴다:

| 에이전트 | 1차 | 재시도 |
|---|---:|---:|
| Validator | 8,192 | **4,096** (-50%) |
| SME | 10,000 | 6,000 (-40%) |
| Jury | — | 4,096 |

1차가 길이 때문에 실패했다면 절반으로 줄인 재시도는 더 잘린다. ``_invoke_bedrock``
의 truncation 자동 확장(:149-156)은 **1회만** 하고, 확장분이 또 잘리면
(``_auto_extend=False``) 잘린 텍스트를 그대로 돌려준다.
"""
from __future__ import annotations

import pytest

import tools.multi_agent_tbox as mt

_PARSE_FAIL = {
    "issues": [],
    "approved": False,
    "summary": "파싱 실패 — 재검토 필요",
    "parse_error": True,
}


def _issue(target: str, sev: str = "critical") -> dict:
    return {"target": target, "severity": sev, "description": f"{target} 문제"}


# ── 1. 이전 이슈 보존 ─────────────────────────────────────────────────────

def test_parse_error_preserves_previous_issues():
    """THE REGRESSION: 파싱 실패 라운드가 이전 이슈를 지우지 않는다.

    지우면 (a) Architect 가 수정 지시를 못 받고 (b) 다음 라운드 persistence
    판정이 리셋된다.
    """
    prev = [_issue("logic:opX"), _issue("logic:opY")]
    kept = mt._merge_review_snapshot(prev, _PARSE_FAIL)
    assert kept == prev, f"파싱 실패인데 이전 이슈가 사라졌다: {kept}"


def test_successful_review_replaces_the_snapshot():
    """NEGATIVE: 정상 리뷰는 스냅샷을 교체한다 (해소된 이슈가 잔존하면 안 된다).

    이 방향을 주장하지 않으면 "항상 이전 것을 유지" 하는 변경이 통과하고,
    한 번 등록된 이슈가 영구히 남아 veto 가 절대 풀리지 않는다.
    """
    prev = [_issue("logic:opX")]
    fresh = {"issues": [_issue("logic:opZ")], "approved": False}
    assert mt._merge_review_snapshot(prev, fresh) == [_issue("logic:opZ")]


def test_empty_but_successful_review_clears_the_snapshot():
    """NEGATIVE: 정상적으로 "이슈 없음" 을 보고했으면 비운다.

    ``parse_error`` 가 아닌 빈 리스트는 **합의 신호** 다. 이것을 보존하면
    승인된 T-Box 가 영구히 미해결 이슈를 갖는다.
    """
    prev = [_issue("logic:opX")]
    clean = {"issues": [], "approved": True}
    assert mt._merge_review_snapshot(prev, clean) == []


def test_snapshot_merge_is_wired_into_the_round_loop():
    """라운드 루프가 병합 헬퍼를 쓴다 (소스 레벨 배선 고정).

    문자열 검사만으로는 **한쪽만 배선한 변경**을 놓친다 (실측: validator 쪽만
    되돌린 mutant 가 생존했다 — sme 쪽 호출이 남아 문자열이 발견됐다).
    그래서 아래 동작 테스트와 짝으로 둔다.
    """
    import inspect

    src = inspect.getsource(mt._run_one_debate_round)
    assert src.count("_merge_review_snapshot") >= 2, (
        "validator/sme 양쪽에 배선되지 않았다 — 한쪽은 여전히 이슈를 지운다"
    )


def test_both_reviewers_preserve_issues_in_the_round_loop(monkeypatch):
    """동작 검증: 라운드 루프를 실제로 태워 **양쪽** 스냅샷 보존을 확인한다.

    한쪽만 배선하면 그 리뷰어의 persistence 판정이 계속 리셋된다.
    """
    # 키 목록은 ``_init_debate_state`` 반환값과 맞춘다 (multi_agent_tbox 참조).
    state = {
        "current_ttl": (
            "@prefix steel: <http://example.com/steel-ontology#> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "steel:A a owl:Class .\n"
        ),
        "architect_stats": {},
        "csv_summary": "",
        "cqs": [],
        "extra_context": "",
        "debate_log": [],
        "consensus_reached": False,
        "compromise_reason": None,
        "prev_validator_issues": [_issue("logic:vPrev")],
        "prev_sme_issues": [_issue("mapping:sPrev")],
        "prev_round_ttl": None,
        "veto_lock_triggered": False,
        "veto_persistent_targets": [],
        "initial_draft_rounds": 1,
        "infra_abort": None,
        "no_progress_streak": 0,
    }
    monkeypatch.setattr(
        mt, "_run_validator_and_sme",
        lambda *a, **k: (dict(_PARSE_FAIL), dict(_PARSE_FAIL)),
    )
    monkeypatch.setattr(mt, "_compute_metrics_feedback", lambda *a, **k: "")
    monkeypatch.setattr(mt, "_compute_round_quality_metrics", lambda *a, **k: {})
    monkeypatch.setattr(mt, "_ontoqa_metrics_for_round", lambda *a, **k: {},
                        raising=False)
    monkeypatch.setattr(mt, "_cq_runtime_check",
                        lambda *a, **k: {"answerable": [], "unanswerable": [],
                                         "coverage_pct": 100.0})
    monkeypatch.setattr(mt, "_load_competency_questions", lambda *a, **k: [])
    monkeypatch.setattr(mt, "_architect_revise",
                        lambda ttl, *a, **k: state["current_ttl"])

    mt._run_one_debate_round(state, 2, 4, phase_setter=lambda *_a, **_k: None)

    assert state["prev_validator_issues"] == [_issue("logic:vPrev")], (
        f"validator 이슈가 지워졌다: {state['prev_validator_issues']}"
    )
    assert state["prev_sme_issues"] == [_issue("mapping:sPrev")], (
        f"sme 이슈가 지워졌다: {state['prev_sme_issues']}"
    )


# ── 2. Architect 가 이전 이슈를 본다 ─────────────────────────────────────

def test_architect_receives_preserved_issues_on_parse_error(monkeypatch):
    """파싱 실패 시 Architect 가 **이전 라운드 이슈**를 받는다.

    ``_architect_revise`` 는 ``validator_review["issues"]`` 를 그대로 읽으므로
    (:1509), 센티넬을 그대로 넘기면 지적이 사라진다.
    """
    prev = [_issue("logic:opX")]
    effective = mt._review_for_architect(_PARSE_FAIL, prev)
    assert effective.get("issues") == prev, (
        f"Architect 가 빈 이슈를 받는다: {effective.get('issues')}"
    )
    # 파싱 실패였다는 사실은 유지해야 한다 (approved 를 조작하면 fail-open 이 된다).
    assert effective.get("approved") is False
    assert effective.get("parse_error") is True


def test_review_for_architect_passes_through_successful_reviews():
    """NEGATIVE: 정상 리뷰는 그대로 넘긴다."""
    fresh = {"issues": [_issue("logic:opZ")], "approved": False}
    assert mt._review_for_architect(fresh, [_issue("logic:opX")]) is fresh


# ── 3. veto 락 상태를 건드리지 않는다 ────────────────────────────────────
#
# 스냅샷 보존만으로는 이 경로가 막히지 않는다. ``_detect_and_record_veto`` 는
# **이번 라운드 리뷰** 를 후보로 쓰므로, 센티넬의 빈 이슈를 "해소됐다" 로 읽고
# 락을 **거짓 해제** 한다 (실측 2026-08-18: sentinel 2개로 호출 →
# veto_lock_triggered=False, veto_lock_released=True).
#
# 반대로 보존된 스냅샷을 현재 이슈로 넘기면 ``_detect_persistent_veto(prev, prev)``
# 가 항상 persistent 를 반환해 1회 관측 이슈를 "2라운드 연속" 으로 조작한다 —
# 락을 **거짓 강화** 한다. 그래서 보류(hold)만 한다.

def _veto_state(locked: bool = True) -> dict:
    return {
        "veto_lock_triggered": locked,
        "veto_persistent_targets": ["logic:x"] if locked else [],
    }


def test_parse_error_does_not_release_the_veto_lock():
    """THE REGRESSION: 파싱 실패를 "이슈 해소" 로 읽어 락을 풀지 않는다."""
    state = _veto_state(locked=True)
    log: dict = {}
    mt._detect_and_record_veto(
        state, dict(_PARSE_FAIL), dict(_PARSE_FAIL),
        [_issue("logic:x")], [_issue("logic:x")], 3, log,
    )
    assert state["veto_lock_triggered"] is True, "판정 불가 라운드가 락을 풀었다"
    assert state["veto_persistent_targets"] == ["logic:x"]
    assert "veto_lock_released" not in log
    assert log.get("veto_lock_hold"), f"보류가 기록되지 않았다: {log}"


def test_parse_error_does_not_strengthen_the_veto_lock():
    """NEGATIVE: 락이 없던 상태를 파싱 실패로 걸지 않는다.

    보존된 스냅샷을 현재 이슈로 넘기는 구현이면 ``persistent`` 가 항상 참이 되어
    1회 관측 이슈로 락이 걸린다 (반증에서 실측된 위험).
    """
    state = _veto_state(locked=False)
    log: dict = {}
    mt._detect_and_record_veto(
        state, dict(_PARSE_FAIL), dict(_PARSE_FAIL),
        [_issue("logic:x")], [_issue("logic:x")], 3, log,
    )
    assert state["veto_lock_triggered"] is False, (
        f"파싱 실패로 락이 새로 걸렸다: targets={state['veto_persistent_targets']}"
    )


def test_normal_round_after_parse_error_still_releases():
    """NEGATIVE: 보류는 영구가 아니다 — 다음 정상 라운드가 실제 상태로 판정한다.

    이 방향을 주장하지 않으면 "판정 불가면 유지" 가 단방향 래치를 되살린다
    (커밋 acb4a6a 에서 고친 결함).
    """
    state = _veto_state(locked=True)
    # 파싱 실패 라운드 — 보류
    mt._detect_and_record_veto(
        state, dict(_PARSE_FAIL), dict(_PARSE_FAIL),
        [_issue("logic:x")], [_issue("logic:x")], 3, {},
    )
    assert state["veto_lock_triggered"] is True
    # 다음 라운드는 정상이고 이슈가 해소됐다 → 해제되어야 한다
    log: dict = {}
    mt._detect_and_record_veto(
        state, {"issues": [], "approved": True}, {"issues": [], "approved": True},
        [_issue("logic:x")], [_issue("logic:x")], 4, log,
    )
    assert state["veto_lock_triggered"] is False, "정상 라운드에서도 락이 유지된다"
    assert log.get("veto_lock_released") is True


# ── 4. 테스트가 배포 audit 파일을 오염시키지 않는다 ──────────────────────

def test_compromise_audit_path_is_isolated_in_tests():
    """``conftest`` autouse fixture 가 배포 경로를 가로챈다.

    실측 (2026-08-18): ``_architect_compromise`` 가 path 인자 없이
    ``append_compromise_audit(artifact)`` 를 호출해 ``COMPROMISE_AUDIT_PATH``
    로 폴백하고, patch 없는 테스트 2개가 **배포 파일에 썼다**.
    ``_MAX_ITERATIONS = 10`` 이라 실측 S2 레코드가 픽스처로 축출됐다 —
    확인 시점에 10건 전부 ``overall_rationale: "no issues"`` 였다.

    개별 테스트 patch 로는 새 테스트가 또 오염시키므로 autouse 로 막았다.
    이 테스트는 그 방어가 살아 있는지 확인한다.
    """
    import tools.compromise_audit as ca

    assert "compromise_audit.json" in str(ca.COMPROMISE_AUDIT_PATH)
    # tmp_path 로 리다이렉트돼 있어야 한다 — 배포 경로(data/generated)면 실패.
    assert "data/generated" not in str(ca.COMPROMISE_AUDIT_PATH), (
        f"테스트가 배포 audit 경로를 쓴다: {ca.COMPROMISE_AUDIT_PATH}"
    )


# ── 5. JSON 추출: 뒤에 해설이 붙은 무-fence 응답 ─────────────────────────
#
# 실측 (2026-08-18): branch 2 (``text.startswith("{")`` → ``return text``) 가
# 원문을 그대로 돌려줘, 뒤에 해설이 붙은 응답이 파싱 실패했다. **앞** 에 해설이
# 붙으면 branch 3 의 괄호 매칭이 살려내는데 **뒤** 에 붙으면 branch 2 가
# short-circuit 해서 못 살리는 비대칭이었다 — LLM 이 흔히 내는 형태다.

def test_trailing_prose_after_json_is_recovered():
    """THE REGRESSION: JSON 뒤에 해설이 붙어도 값만 떼어낸다."""
    import json as _json

    text = '{"issues": [], "approved": true}\n\n위와 같이 검토했습니다.'
    _json.loads(mt._extract_json_candidate(text))   # 예외 없어야 한다


def test_leading_prose_still_recovered():
    """POSITIVE 보존: 앞에 해설이 붙는 기존 경로는 계속 동작한다."""
    import json as _json

    text = '검토 결과입니다:\n{"issues": [], "approved": true}'
    assert _json.loads(mt._extract_json_candidate(text))["approved"] is True


def test_braces_inside_strings_do_not_truncate_the_value():
    """문자열 안의 중괄호에 속지 않는다 (탐욕 정규식의 함정).

    ``raw_decode`` 를 쓰는 이유다 — 정규식으로 첫 ``}`` 까지 자르면
    ``"a}b"`` 같은 값에서 JSON 이 깨진다.
    """
    import json as _json

    text = ('{"issues": [{"target": "a}b", "severity": "critical"}], '
            '"approved": false}\n설명')
    parsed = _json.loads(mt._extract_json_candidate(text))
    assert parsed["issues"][0]["target"] == "a}b"


def test_fenced_json_still_preferred():
    """POSITIVE 보존: fence 블록 경로가 우선 유지된다."""
    import json as _json

    text = '```json\n{"issues": [], "approved": true}\n```'
    assert _json.loads(mt._extract_json_candidate(text)) == {
        "issues": [], "approved": True,
    }


def test_truncated_json_still_falls_through():
    """NEGATIVE: 잘린 JSON 을 억지로 살리지 않는다 (재시도가 담당).

    부분 구제는 "이슈 절반만 반영" 이라는 조용한 부분 실패를 만든다. 잘림은
    재시도·토큰 예산으로 다뤄야 한다.
    """
    import json as _json

    text = '{"issues": [{"target": "a", "severity": "critical"}, {"target": "b"'
    with pytest.raises(_json.JSONDecodeError):
        _json.loads(mt._extract_json_candidate(text))


# ── 6. round_log 의 정직성 ───────────────────────────────────────────────

def test_round_log_marks_the_parse_failure():
    """``issues_count: 0`` 으로만 남으면 "깨끗한 라운드" 로 오독된다."""
    log = mt._build_round_log(_PARSE_FAIL, {"issues": [], "approved": False}, 3, True)
    v = log.get("validator") or {}
    assert v.get("parse_error") is True, (
        f"파싱 실패가 라운드 로그에 기록되지 않았다: {v}"
    )


def test_round_log_does_not_mark_healthy_rounds():
    """NEGATIVE: 정상 라운드에 실패 표시가 붙지 않는다."""
    log = mt._build_round_log(
        {"issues": [_issue("a")], "approved": False},
        {"issues": [], "approved": True}, 1, True,
    )
    assert not (log.get("validator") or {}).get("parse_error")


# ── 4. 재시도가 토큰을 줄이지 않는다 ─────────────────────────────────────

def test_retry_does_not_shrink_max_tokens():
    """재시도 ``max_tokens`` 가 1차보다 작으면 안 된다.

    1차가 길이 때문에 실패했다면 절반으로 줄인 재시도는 더 잘린다. 실측:
    Validator 8,192 → 4,096 (-50%), SME 10,000 → 6,000 (-40%).

    소스에서 각 에이전트의 1차/재시도 값을 추출해 비교한다 — 상수를 바꿔도
    테스트가 따라오도록.
    """
    import inspect
    import re

    src = inspect.getsource(mt)
    # ``def _retry_<agent>`` 직후의 max_tokens 와, 그 함수 **앞** 에 나오는
    # 가장 가까운 1차 호출의 max_tokens 를 비교한다.
    # **네 에이전트 전부** 검사한다. 한 곳만 보면 다른 곳에서 같은 결함이
    # 재발한다 — 실측: validator 8192→4096, sme 10000→6000, jury 8192→4096,
    # compromise 8192→4096 로 네 곳 모두 축소돼 있었다.
    for agent in ("validator", "sme", "jury", "compromise"):
        m = re.search(rf"def _retry_{agent}\b.*?max_tokens=(\d+)", src, re.S)
        assert m, f"_retry_{agent} 의 max_tokens 를 찾지 못했다"
        retry_tokens = int(m.group(1))
        head = src[: m.start()]
        firsts = re.findall(r"max_tokens=(\d+)", head)
        assert firsts, f"{agent} 1차 max_tokens 를 찾지 못했다"
        first_tokens = int(firsts[-1])
        assert retry_tokens >= first_tokens, (
            f"{agent} 재시도가 1차보다 작다: {first_tokens} → {retry_tokens}. "
            "1차가 길이로 실패했다면 더 잘린다"
        )
