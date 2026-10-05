"""S2 토론 기록이 디스크에 남고, ``age_rounds`` 가 실제로 증가한다.

2026-08-22 감사 실측 — 두 결함이 한 뿌리다:

1. ``state["debate_log"]`` 가 도구 응답 JSON 에만 실려 **인메모리**
   ``JobRegistry(max_finished=8)`` 가 evict 하면 5라운드 59분치 궤적이 소멸했다
   (``multi_agent_tbox.py`` 의 파일 쓰기는 TTL 2개 + heartbeat 1개뿐).
2. ``_build_round_log`` 이 ``issues_count`` 만 남기고 **본문을 버려서**
   ``_compute_issue_persistence`` 가 읽는 ``prev["validator"]["issues"]`` 가 항상
   빈 리스트였다 → ``age_rounds`` 가 **입력과 무관하게 항상 1**.
   그래서 ``_compute_issue_priority`` 의 age 가중치(라운드당 +15, 최대 +45)가
   한 번도 발화하지 않았다 — "3라운드 연속 잔존" 이라는 가장 강한 우선순위 신호가
   죽어 있었다.

## 이 파일이 고정하는 계약

- **지문 보존**: 슬림화가 ``_issue_fingerprint`` 를 바꾸지 않는다. 지문 입력 키를
  하나라도 빠뜨리면 persistence 판정이 조용히 깨지므로 이것이 1순위다.
- **age_rounds 증가**: 같은 이슈가 반복되면 2, 3, … 으로 오른다 (역회귀).
- **응답 크기 보호**: 이슈 상한/절단이 있다 (S8 25MB 응답이 stdio 를 끊은 이력).
- **fail-open**: 기록 실패가 예외로 전파되지 않는다 (T-Box 저장이 우선).
- **테스트 격리**: conftest autouse fixture 가 배포 파일을 가로챈다.
"""
from __future__ import annotations

import json

import pytest

from tools.debate_log_store import (
    DEBATE_LOG_PATH,
    _issue_trace,
    _load,
    _save,
    append_debate_run,
)
from tools.multi_agent_tbox import (
    _ROUND_LOG_MAX_ISSUES,
    _build_round_log,
    _compute_issue_persistence,
    _compute_issue_priority,
    _issue_fingerprint,
    _slim_issue,
)

_PERSISTENT = {
    "severity": "high", "category": "logic", "target": "EquipmentMaster",
    "symptom": "domain 누락", "fix": "rdfs:domain 추가",
}
_TRANSIENT = {
    "severity": "medium", "category": "style", "target": "TagMaster",
    "symptom": "라벨 표기 불일치",
}


def _rounds(count: int, *, issue: dict = None, only_round: int = None) -> list[dict]:
    """count 라운드 분량의 round_log 목록. issue 를 매 라운드(또는 특정 라운드) 포함."""
    issue = issue or _PERSISTENT
    out = []
    for r in range(count):
        include = issue if (only_round is None or r == only_round) else None
        issues = [include] if include else []
        out.append(_build_round_log(
            {"issues": issues, "approved": False, "summary": ""},
            {"issues": [], "approved": False, "summary": ""},
            r, True,
        ))
    return out


# ──────────────────────────────────────────────────────────────────
# 1. 지문 보존 (가장 위험한 방향 — 깨지면 조용하다)
# ──────────────────────────────────────────────────────────────────

def test_slimming_preserves_fingerprint():
    """슬림화 후에도 지문이 같다 — 다르면 age_rounds 가 리셋된다."""
    assert _issue_fingerprint(_slim_issue(_PERSISTENT)) == _issue_fingerprint(_PERSISTENT)


def test_slimming_preserves_fingerprint_for_long_text():
    """200자 절단이 지문을 바꾸지 않는다 (지문도 앞 200자만 해시한다)."""
    long_issue = {**_PERSISTENT, "symptom": "가" * 500}
    assert _issue_fingerprint(_slim_issue(long_issue)) == _issue_fingerprint(long_issue)


@pytest.mark.parametrize(
    "key", ["symptom", "text", "issue", "severity", "target",
            "affected_class", "class"],
)
def test_every_fingerprint_input_key_survives_slimming(key):
    """지문 입력 키가 전부 보존된다 — 하나라도 빠지면 판정이 갈린다."""
    issue = {key: "value-for-" + key, "severity": "high"}
    slim = _slim_issue(issue)
    assert key in slim, f"지문 입력 키 {key} 가 슬림화에서 사라졌다"
    assert _issue_fingerprint(slim) == _issue_fingerprint(issue)


# ──────────────────────────────────────────────────────────────────
# 2. age_rounds 가 실제로 증가한다 (원래 버그의 역회귀)
# ──────────────────────────────────────────────────────────────────

def test_round_log_stores_issue_bodies():
    """``issues`` 본문이 저장된다 — 이것이 없으면 persistence 가 죽는다."""
    log = _build_round_log(
        {"issues": [_PERSISTENT], "approved": False, "summary": ""},
        {"issues": [_TRANSIENT], "approved": False, "summary": ""}, 0, True,
    )
    assert log["validator"]["issues"], "validator 이슈 본문이 없다"
    assert log["sme"]["issues"], "sme 이슈 본문이 없다"


def test_age_rounds_increases_across_rounds():
    """같은 이슈가 4라운드 반복되면 age_rounds 가 5 다 (예전엔 항상 1)."""
    enriched = _compute_issue_persistence([_PERSISTENT], _rounds(4))
    assert enriched[0]["age_rounds"] == 5, (
        f"age_rounds={enriched[0]['age_rounds']} — 본문 미저장 버그가 되살아났다"
    )


def test_age_rounds_resets_when_continuity_breaks():
    """연속성이 끊기면 리셋된다 — 무조건 증가가 아니다."""
    # 이슈가 라운드 0 에만 있었다 → 최근 라운드에는 없으므로 age 는 1.
    enriched = _compute_issue_persistence([_PERSISTENT],
                                         _rounds(3, only_round=0))
    assert enriched[0]["age_rounds"] == 1


def test_priority_age_weight_actually_fires():
    """age 가중치가 발화한다 — 죽은 신호였던 것이 살아났는지 확인."""
    fresh = _compute_issue_priority({**_PERSISTENT, "age_rounds": 1}, 91.7)
    aged = _compute_issue_priority({**_PERSISTENT, "age_rounds": 5}, 91.7)
    assert aged > fresh, "age_rounds 가 우선순위에 반영되지 않는다"


# ──────────────────────────────────────────────────────────────────
# 3. 응답 크기 보호
# ──────────────────────────────────────────────────────────────────

def test_issue_count_is_capped():
    """이슈가 폭주해도 상한까지만 남는다 (응답 크기 → stdio 보호)."""
    many = [{**_PERSISTENT, "symptom": f"issue-{i}"}
            for i in range(_ROUND_LOG_MAX_ISSUES + 25)]
    log = _build_round_log({"issues": many, "approved": False, "summary": ""},
                           {"issues": [], "approved": False, "summary": ""}, 0, True)
    assert len(log["validator"]["issues"]) == _ROUND_LOG_MAX_ISSUES
    # 카운터는 **전체** 를 세야 한다 (상한이 통계를 왜곡하면 안 된다).
    assert log["validator"]["issues_count"] == len(many)


def test_long_strings_are_truncated():
    slim = _slim_issue({**_PERSISTENT, "symptom": "가" * 900})
    assert len(slim["symptom"]) == 200


def test_unknown_keys_are_dropped():
    """계약에 없는 키는 버린다 — LLM 이 장문 필드를 붙여도 커지지 않는다."""
    slim = _slim_issue({**_PERSISTENT, "verbose_explanation": "x" * 5000})
    assert "verbose_explanation" not in slim


# ──────────────────────────────────────────────────────────────────
# 4. 영속화 동작
# ──────────────────────────────────────────────────────────────────

def test_append_writes_and_reloads(tmp_path):
    p = str(tmp_path / "d.json")
    res = append_debate_run(rounds=_rounds(3), consensus_reached=False,
                            veto_lock_triggered=True, path=p)
    assert res["saved"] is True
    data = _load(p)
    assert len(data["runs"]) == 1
    assert data["runs"][0]["total_rounds"] == 3


def test_issue_trace_counts_repeated_rounds(tmp_path):
    """"같은 지적이 몇 라운드 반복됐나" 표가 정확한가 (핵심 산출물)."""
    rounds = _rounds(5)
    rounds[2]["sme"]["issues"] = [_slim_issue(_TRANSIENT)]
    trace = _issue_trace(rounds)
    persistent = [t for t in trace if t["target"] == "EquipmentMaster"][0]
    transient = [t for t in trace if t["target"] == "TagMaster"][0]
    assert persistent["rounds_seen"] == 5
    assert persistent["rounds"] == [1, 2, 3, 4, 5]
    assert transient["rounds_seen"] == 1


def test_runs_are_size_bounded(tmp_path):
    p = str(tmp_path / "d.json")
    for _ in range(15):
        append_debate_run(rounds=_rounds(1), consensus_reached=False,
                          veto_lock_triggered=False, path=p)
    assert len(_load(p)["runs"]) <= 10


def test_corrupt_file_degrades_gracefully(tmp_path):
    p = tmp_path / "d.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert _load(str(p)) == {"version": 1, "runs": []}
    # 손상된 파일이 있어도 append 가 성공해야 한다.
    assert append_debate_run(rounds=_rounds(1), consensus_reached=True,
                             veto_lock_triggered=False, path=str(p))["saved"]


def test_append_is_fail_open_on_bad_path():
    """저장 실패가 예외로 전파되지 않는다 — T-Box 저장이 우선이다."""
    res = append_debate_run(rounds=_rounds(1), consensus_reached=False,
                            veto_lock_triggered=False,
                            path="/proc/nonexistent-dir/d.json")
    assert res["saved"] is False
    assert "error" in res


def test_malformed_rounds_do_not_crash(tmp_path):
    """라운드 구조가 예상과 달라도 기록은 진행된다."""
    res = append_debate_run(rounds=[{"round": 1, "validator": None},
                                    {"round": 2}, "not-a-dict"],
                            consensus_reached=False, veto_lock_triggered=False,
                            path=str(tmp_path / "d.json"))
    assert res["saved"] is True


def test_age_out_drops_old_runs(tmp_path):
    p = str(tmp_path / "d.json")
    _save({"version": 1, "runs": [
        {"timestamp": "2020-01-01T00:00:00", "total_rounds": 1},
        {"timestamp": "2099-01-01T00:00:00", "total_rounds": 2},
    ]}, p)
    kept = _load(p)["runs"]
    assert [r["total_rounds"] for r in kept] == [2], "오래된 run 이 남았다"


def test_unparseable_timestamp_is_kept(tmp_path):
    """포맷 드리프트로 사용자 데이터를 조용히 버리지 않는다 (fail-open)."""
    p = str(tmp_path / "d.json")
    _save({"version": 1, "runs": [{"timestamp": "not-a-date", "total_rounds": 7}]}, p)
    assert _load(p)["runs"][0]["total_rounds"] == 7


# ──────────────────────────────────────────────────────────────────
# 5. 배선 + 테스트 격리
# ──────────────────────────────────────────────────────────────────

def test_finalize_actually_invokes_the_store(monkeypatch, tmp_path):
    """``_finalize_and_save`` 가 저장 함수를 **실제로 호출** 하는가.

    소스 문자열 grep 만으로는 부족하다 — mutation test 에서 호출을 no-op 람다로
    바꾼 mutant 가 문자열 검사를 통과해 **생존했다**. 그래서 호출을 spy 로 가로채
    실행 여부를 확인한다 (이 리포의 "산출물로 확인하라" 원칙).
    """
    import tools.debate_log_store as dls
    import tools.multi_agent_tbox as mat

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return {"saved": True, "path": "spy", "runs": 1, "persistent_issues": 0}

    monkeypatch.setattr(dls, "append_debate_run", _spy)

    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "steel:A a owl:Class .\n"
    )
    monkeypatch.setattr(mat, "TBOX_PATH", str(tmp_path / "t_box.ttl"))
    monkeypatch.setattr(mat, "TBOX_BASELINE_PATH", str(tmp_path / "base.ttl"))

    state = {
        "current_ttl": ttl, "consensus_reached": False,
        "veto_lock_triggered": False, "veto_persistent_targets": [],
        "debate_log": _rounds(2), "architect_stats": {"classes": 1},
        "initial_draft_rounds": 1, "compromise_reason": None, "cqs": [],
    }
    payload = json.loads(mat._finalize_and_save(state, 0.0))

    assert calls, "append_debate_run 이 호출되지 않았다 — 배선이 끊겼다"
    assert calls[0]["rounds"] == state["debate_log"], "라운드가 전달되지 않았다"
    assert payload["debate_log_saved"]["saved"] is True, (
        "응답에 기록 결과가 실리지 않는다"
    )


def test_conftest_isolates_production_path():
    """autouse fixture 가 배포 경로를 가로챈다 — 오염 사고 재발 방지."""
    import tools.debate_log_store as dls

    assert dls.DEBATE_LOG_PATH != DEBATE_LOG_PATH or "tmp" in dls.DEBATE_LOG_PATH, (
        "테스트가 배포 debate_log.json 에 쓸 수 있다 — conftest fixture 확인"
    )
    assert "data/generated/tbox/debate_log.json" not in dls.DEBATE_LOG_PATH


def test_default_path_lands_in_tbox_dir():
    """모듈 상수는 다른 T-Box 산출물과 같은 디렉터리를 가리킨다."""
    assert DEBATE_LOG_PATH.endswith("debate_log.json")
    assert "generated" in DEBATE_LOG_PATH and "tbox" in DEBATE_LOG_PATH


def test_saved_json_is_readable_utf8(tmp_path):
    """한국어가 이스케이프 없이 저장돼 사람이 읽을 수 있다."""
    p = tmp_path / "d.json"
    append_debate_run(rounds=_rounds(1), consensus_reached=False,
                      veto_lock_triggered=False, path=str(p))
    text = p.read_text(encoding="utf-8")
    assert "domain 누락" in text
    json.loads(text)
