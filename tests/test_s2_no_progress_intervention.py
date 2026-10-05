"""Regression: 무진전 라운드에서 Jury 개입이 실제로 정체를 뚫는가.

2026-08-19 실측 (58.5분 S2 실행): 이슈 수가 12→14→12→14 로 **전혀 수렴하지
않았고**, Architect 수정 트리플이 2833→3365→3365(정체)→null 이었다. SME 는 R2·R4
에서 "T-Box 가 변경되지 않아" 를 명시했고 지문 비교로 사실 확인됐다.

무진전 감지 장치는 이미 있었지만 **조기 종료에만** 쓰였다 — 정체를 해소하려는
시도가 없었다. ``jury_fixes_summary`` 가 R1~R3 전부 None 이고 마지막 라운드에서야
128건이 한꺼번에 적용됐다(94 반영). 그 개입이 정체 시점에 있었다면 남은 라운드가
바뀐 T-Box 를 리뷰하는 유효한 토론이 됐다.

**이 파일이 주장하는 것**: 카운터(applied>0)가 아니라 —
 (1) 정체 시 Jury 가 **실제로 호출** 되는가,
 (2) 그래프 **지문이 바뀌었는지** 로 진전을 판정하는가 (적용 보고를 믿지 않는다),
 (3) 개입이 실패해도 라운드를 죽이지 않는가,
 (4) 정상 라운드에서는 개입하지 않는가 (비용/지연 회귀 방지).
"""

from unittest.mock import patch

import tools.multi_agent_tbox as mat

_TTL_A = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix steel: <http://example.com/steel-ontology#> .
steel:A a owl:Class .
"""
_TTL_B = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix steel: <http://example.com/steel-ontology#> .
steel:A a owl:Class .
steel:B a owl:Class .
"""


def _state(ttl=_TTL_A):
    return {
        "current_ttl": ttl,
        "debate_log": [],
        "veto_persistent_targets": ["logic:dit"],
        "no_progress_streak": 1,
        "last_revision_fingerprint": mat._graph_fingerprint(ttl),
    }


def _reviews(approved=False):
    v = {"approved": approved, "issues": [{"severity": "critical", "target": "X"}]}
    s = {"approved": approved, "issues": []}
    return v, s


# ── 개입이 일어나고, 산출물로 판정하는가 ────────────────────────────────

def test_intervention_calls_jury_and_applies_fixes():
    """정체 시 Jury 를 호출하고 required_fixes 를 적용한다."""
    st = _state()
    v, s = _reviews()
    log: dict = {}
    fixes = [{"action": "add_class", "target": "B"}]

    def fake_jury(*a, **kw):
        return {"production_ready": False, "required_fixes": fixes}

    def fake_apply(state, required, round_log, origin="consensus"):
        state["current_ttl"] = _TTL_B      # 실제로 그래프를 바꾼다
        round_log["jury_fixes_summary"] = {"origin": origin, "applied": 1}
        return {"applied": 1, "origin": origin}

    with patch.object(mat, "_jury_decide", fake_jury), \
         patch.object(mat, "_apply_jury_required_fixes", fake_apply):
        changed = mat._intervene_on_no_progress(
            st, v, s, {}, {"coverage_pct": 50.0}, 2, log, streak=1,
        )

    assert changed is True, "그래프가 바뀌었는데 False 를 돌렸다"
    rec = log["no_progress_intervention"]
    assert rec["attempted"] is True
    assert rec["required_fixes"] == 1
    assert rec["changed"] is True
    assert log["jury_fixes_summary"]["origin"] == "no_progress", (
        "개입 경로가 origin 으로 구분되지 않는다 — 사후에 어느 경로가 T-Box 를 "
        "바꿨는지 알 수 없다"
    )


def test_verdict_is_graph_fingerprint_not_applied_counter():
    """적용 보고가 있어도 **그래프가 안 바뀌면** 진전이 아니다.

    이 리포에서 '적용 보고 vs 실제 그래프' 불일치 사고가 반복됐다 — 카운터를
    믿으면 정체가 해소된 것처럼 보이고 다음 라운드가 같은 결과를 낸다.
    """
    st = _state()
    v, s = _reviews()
    log: dict = {}

    def fake_jury(*a, **kw):
        return {"production_ready": False,
                "required_fixes": [{"action": "add_class", "target": "B"}]}

    def fake_apply(state, required, round_log, origin="consensus"):
        # applied=5 를 보고하지만 TTL 은 그대로 (전부 no-op 인 경우)
        round_log["jury_fixes_summary"] = {"applied": 5, "origin": origin}
        return {"applied": 5}

    with patch.object(mat, "_jury_decide", fake_jury), \
         patch.object(mat, "_apply_jury_required_fixes", fake_apply):
        changed = mat._intervene_on_no_progress(
            st, v, s, {}, {}, 2, log, streak=1,
        )

    assert changed is False, "applied=5 를 진전으로 오인했다 (지문은 그대로)"
    assert log["no_progress_intervention"]["changed"] is False


def test_fingerprint_baseline_updated_only_when_changed():
    """개입으로 바뀌면 기준 지문을 갱신한다 — 다음 라운드가 자기 진전으로 오인 방지."""
    st = _state()
    before = st["last_revision_fingerprint"]
    v, s = _reviews()

    def fake_jury(*a, **kw):
        return {"production_ready": False, "required_fixes": [{"action": "x"}]}

    def fake_apply(state, required, round_log, origin="consensus"):
        state["current_ttl"] = _TTL_B
        return {}

    with patch.object(mat, "_jury_decide", fake_jury), \
         patch.object(mat, "_apply_jury_required_fixes", fake_apply):
        mat._intervene_on_no_progress(st, v, s, {}, {}, 2, {}, streak=1)

    assert st["last_revision_fingerprint"] != before, "기준 지문이 갱신되지 않았다"
    assert st["last_revision_fingerprint"] == mat._graph_fingerprint(_TTL_B)


def test_baseline_untouched_when_no_change():
    """변경이 없으면 기준 지문을 건드리지 않는다 (거짓 진전 금지)."""
    st = _state()
    before = st["last_revision_fingerprint"]
    v, s = _reviews()
    with patch.object(mat, "_jury_decide",
                      lambda *a, **k: {"required_fixes": []}):
        mat._intervene_on_no_progress(st, v, s, {}, {}, 2, {}, streak=1)
    assert st["last_revision_fingerprint"] == before


# ── NEGATIVE: 실패해도 라운드를 죽이지 않는가 ───────────────────────────

def test_jury_failure_does_not_raise():
    """Jury 호출 실패는 라운드를 죽이지 않고 정체 상태를 유지한다.

    인프라 오류로 40분 진행분을 버리면 안 된다 (이 리포의 반복 사고 유형).
    """
    st = _state()
    v, s = _reviews()
    log: dict = {}
    with patch.object(mat, "_jury_decide",
                      side_effect=RuntimeError("throttling")):
        changed = mat._intervene_on_no_progress(st, v, s, {}, {}, 2, log, streak=1)
    assert changed is False
    assert "error" in log["no_progress_intervention"]
    assert st["current_ttl"] == _TTL_A, "실패 시 TTL 이 손상됐다"


def test_no_fixes_is_reported_not_silent():
    """Jury 가 고칠 것이 없다고 하면 — 리뷰어 미승인과의 불일치를 기록한다."""
    st = _state()
    v, s = _reviews(approved=False)
    log: dict = {}
    with patch.object(mat, "_jury_decide",
                      lambda *a, **k: {"production_ready": False,
                                       "required_fixes": []}):
        changed = mat._intervene_on_no_progress(st, v, s, {}, {}, 2, log, streak=1)
    assert changed is False
    rec = log["no_progress_intervention"]
    assert rec["required_fixes"] == 0
    assert rec["changed"] is False


def test_opt_out_env_disables_intervention():
    """환경변수로 끌 수 있다 (기존 동작으로 복귀 경로)."""
    st = _state()
    v, s = _reviews()
    log: dict = {}
    called = []
    with patch.dict("os.environ",
                    {"MULTI_AGENT_NO_PROGRESS_INTERVENE": "false"}), \
         patch.object(mat, "_jury_decide",
                      lambda *a, **k: called.append(1) or {}):
        changed = mat._intervene_on_no_progress(st, v, s, {}, {}, 2, log, streak=1)
    assert changed is False
    assert not called, "비활성인데 Jury 를 호출했다 (비용 발생)"
    assert "no_progress_intervention" not in log


# ── 배선: 정체 감지 지점에서 호출되는가 ─────────────────────────────────

def test_intervention_wired_into_no_progress_branch():
    """``no_progress`` 분기가 개입 함수를 호출한다.

    함수만 만들고 배선을 빠뜨리면 단위 테스트는 초록인데 산출물은 안 바뀐다
    (이 리포에서 반복된 실패 유형). 소스에서 호출 위치를 확인한다.
    """
    import inspect

    src = inspect.getsource(mat)
    assert "_intervene_on_no_progress(" in src
    # 정의 1회 + 호출 1회 이상
    assert src.count("_intervene_on_no_progress(") >= 2, (
        "정의만 있고 호출부가 없다 — 배선 누락"
    )
    # no_progress 분기 안에서 호출돼야 한다 (조기 종료 판정보다 **앞**).
    branch = src.split("if no_progress:", 1)[1].split("except Exception", 1)[0]
    assert "_intervene_on_no_progress(" in branch, (
        "no_progress 분기 밖에서 호출된다 — 정상 라운드에서도 Jury 를 태운다"
    )
    call_at = branch.index("_intervene_on_no_progress(")
    limit_at = branch.index("_NO_PROGRESS_STREAK_LIMIT")
    assert call_at < limit_at, (
        "조기 종료 판정 뒤에 개입한다 — 종료되는 라운드에서는 개입이 무의미하다"
    )


def test_apply_helper_extracted_and_shared():
    """fix 적용 로직이 함수로 추출돼 두 경로가 **같은** 코드를 쓴다.

    사본을 만들면 한쪽만 라우팅 가드를 타는 사고가 이 리포에 있었다
    (DSL 선행 pass 가 jury_fixes 가드를 우회한 건).
    """
    import inspect

    src = inspect.getsource(mat)
    assert src.count("_apply_jury_required_fixes(") >= 3, (
        "추출된 적용 함수가 두 경로에서 공유되지 않는다"
    )
    # 예비 합의 분기에 인라인 사본이 남아 있지 않아야 한다.
    consensus_src = inspect.getsource(mat._handle_potential_consensus)
    assert "from tools.jury_fixes import apply_jury_fixes" not in consensus_src, (
        "합의 분기에 인라인 적용 사본이 남았다 — 갈라질 수 있다"
    )
    assert "_apply_jury_required_fixes(" in consensus_src
