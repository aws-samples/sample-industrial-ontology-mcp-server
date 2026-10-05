"""S2 토론 수렴 조건 회귀 가드 — 합의가 구조적으로 불가능했던 세 원인.

2026-08-17 S2 실행(48분, 5라운드) 실측:
  - 5라운드 전부 ``consensus=false``, CQ 커버리지 50% 가 한 번도 안 움직임
  - R4 와 R5 의 revision triples 가 3222 로 **동일** (Architect 무변경).
    R5 Validator: "4라운드에서도 T-Box 변경이 없어 이전 critical 이슈가 잔존"
  - ``veto_lock_triggered: true``, persistent targets 2건이 3라운드 연속 잔존.
    그 2건은 **CSV 데이터 갭**(Air_Emission_Monitoring 에 Equipment_ID 컬럼 없음,
    Monitoring_Point_Master 에 Equipment_ID 없음)이라 T-Box 수정으로 해소 불가.

즉 48분 중 후반 절반이 "어떤 수정으로도 합의에 도달할 수 없는" 구간이었다.

## 세 원인

1. **합의 조건이 데이터 갭을 요구한다.** ``not cq_ops_missing`` 이 합의 조건인데,
   같은 리포의 다른 코드는 그 갭을 이미 "CSV FK 근거 없음 = T-Box 로 고칠 수 없음"
   으로 판정한다(``_generate_op_skeletons_from_cq_gaps`` 가 주입을 거부).
   Architect 가 무엇을 해도 이 조건은 안 풀린다.

2. **veto lock 이 단방향 래치다.** ``_detect_and_record_veto`` 는 True 로만
   세팅하고 False 로 되돌리는 코드가 소스 전체에 없었다. 합의 조건에
   ``not veto_lock_triggered`` 가 걸려 있으니, 어떤 이슈든 2라운드 연속 잔존한
   순간부터 남은 라운드가 전부 무의미해진다.

3. **무진전 감지가 없다.** ``round_log["revision"]`` 에 트리플 수를 기록하지만
   이전 라운드와 비교하지 않는다(grep: stagnat/no_progress 히트 0). 변경 0인
   라운드를 계속 태운다.
"""
from __future__ import annotations

import tools.multi_agent_tbox as mt

# ── 1. 데이터 갭 분류 ─────────────────────────────────────────────────────

def test_data_gap_cq_is_classified_separately(monkeypatch):
    """FK 근거가 없는 CQ 갭은 ``data_gap`` 으로 분류된다 (합의 차단 대상 아님)."""
    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {("alpha", "beta")})
    unanswerable = [
        ("CQ01", "alpha → beta 경로 없음 (3홉 내 OP 연결 부재)"),   # FK 근거 있음
        ("CQ02", "gamma → delta 경로 없음 (3홉 내 OP 연결 부재)"),   # FK 근거 없음
    ]

    fixable, data_gap = mt._split_cq_gaps_by_fk_evidence(unanswerable)

    assert [c for c, _ in fixable] == ["CQ01"], f"fixable 오분류: {fixable}"
    assert [c for c, _ in data_gap] == ["CQ02"], f"data_gap 오분류: {data_gap}"


def test_unparseable_fk_inventory_treats_all_as_fixable(monkeypatch):
    """CSV 를 읽을 수 없으면(None) 전부 fixable — 판정 불가를 '갭' 으로 읽지 않는다.

    ``_csv_fk_pairs`` 의 규약이 "None = 판정 불가, 게이트 적용 안 함" 이다.
    이를 어기면 CSV 접근 실패가 조용히 모든 CQ 를 면제해 게이트를 끈다.
    """
    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: None)
    unanswerable = [("CQ01", "a → b 경로 없음"), ("CQ02", "c → d 경로 없음")]

    fixable, data_gap = mt._split_cq_gaps_by_fk_evidence(unanswerable)

    assert len(fixable) == 2, "판정 불가인데 면제했다"
    assert data_gap == []


def test_unrecognised_reason_format_is_fixable(monkeypatch):
    """쌍을 못 뽑는 사유 문자열은 보수적으로 fixable 로 둔다 (면제는 위험하다)."""
    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {("alpha", "beta")})
    fixable, data_gap = mt._split_cq_gaps_by_fk_evidence(
        [("CQ01", "클래스 누락: alpha"), ("CQ02", "parse error: boom")],
    )
    assert len(fixable) == 2
    assert data_gap == []


def test_consensus_not_blocked_by_data_gap_only(monkeypatch):
    """데이터 갭만 남았으면 합의를 막지 않는다 (소스 레벨 배선 고정).

    분류 헬퍼만 있고 합의 조건이 여전히 전체 ``cq_ops_missing`` 을 보면
    동작이 바뀌지 않는다 — 배선이 사라지는 것을 별도로 막는다.
    """
    import inspect

    src = inspect.getsource(mt._run_one_debate_round)
    assert "_split_cq_gaps_by_fk_evidence" in src, (
        "라운드 루프가 데이터 갭 분류를 호출하지 않는다"
    )
    # 합의 조건은 fixable 만 봐야 한다.
    assert "cq_block_fixable" in src, (
        "합의 조건이 fixable 갭을 쓰지 않는다 — 데이터 갭이 여전히 합의를 막는다"
    )


# ── 2. veto lock 재계산 (단방향 래치 제거) ────────────────────────────────

def _state() -> dict:
    return {"veto_lock_triggered": False, "veto_persistent_targets": []}


def _issue(target: str, sev: str = "critical") -> dict:
    return {"target": target, "severity": sev, "description": f"{target} 문제"}


def test_veto_lock_releases_when_issue_resolved():
    """THE REGRESSION: 잔존 이슈가 사라지면 락이 풀린다.

    풀리지 않으면 R3 에 락이 걸린 순간 R4·R5 합의가 원천 차단된다 (실측).
    """
    state = _state()
    v = {"issues": [_issue("logic:opX")]}
    empty = {"issues": []}
    log1: dict = {}

    # Round 2: 같은 이슈가 2라운드 연속 → 락 발동
    mt._detect_and_record_veto(state, v, empty, [_issue("logic:opX")], [], 2, log1)
    assert state["veto_lock_triggered"] is True, "락이 발동하지 않았다"

    # Round 3: 이슈 해소 → 락 해제
    log2: dict = {}
    mt._detect_and_record_veto(state, empty, empty, [_issue("logic:opX")], [], 3, log2)
    assert state["veto_lock_triggered"] is False, (
        "이슈가 해소됐는데 락이 유지된다 — 남은 라운드가 전부 무의미해진다"
    )
    assert state["veto_persistent_targets"] == []


def test_veto_lock_persists_while_issue_persists():
    """NEGATIVE: 이슈가 계속 잔존하면 락도 유지된다 (게이트 보존).

    해제 로직이 무조건 풀어버리면 veto 가 아무것도 막지 않는다.
    """
    state = _state()
    v = {"issues": [_issue("logic:opX")]}
    mt._detect_and_record_veto(state, v, {"issues": []},
                              [_issue("logic:opX")], [], 2, {})
    assert state["veto_lock_triggered"] is True
    mt._detect_and_record_veto(state, v, {"issues": []},
                              [_issue("logic:opX")], [], 3, {})
    assert state["veto_lock_triggered"] is True, "잔존 중인데 락이 풀렸다"


def test_veto_release_is_recorded_in_round_log():
    """해제 사실이 라운드 로그에 남는다 — 조용한 상태 변화는 디버깅 불가."""
    state = _state()
    v = {"issues": [_issue("logic:opX")]}
    mt._detect_and_record_veto(state, v, {"issues": []},
                              [_issue("logic:opX")], [], 2, {})
    log: dict = {}
    mt._detect_and_record_veto(state, {"issues": []}, {"issues": []},
                              [_issue("logic:opX")], [], 3, log)
    assert log.get("veto_lock_released") is True, f"해제가 기록되지 않았다: {log}"


# ── 3. 무진전 감지 ────────────────────────────────────────────────────────

def test_no_progress_detected_on_identical_graph():
    """같은 트리플 집합이면 무진전으로 판정한다.

    실측: R4/R5 revision triples 가 3222 로 동일했는데 아무도 알아채지 못했다.
    """
    ttl = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class .\n"
    )
    assert mt._graph_fingerprint(ttl) == mt._graph_fingerprint(ttl)
    # 직렬화가 달라도 같은 트리플 집합이면 같은 지문이어야 한다 — 트리플 순서,
    # prefix 선언 순서, `a` vs `rdf:type` 표기는 내용 변경이 아니다.
    reordered = (
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "steel:B rdf:type owl:Class .\n"
        "steel:A rdf:type owl:Class .\n"
    )
    same_set = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class .\nsteel:B a owl:Class .\n"
    )
    assert mt._graph_fingerprint(reordered) == mt._graph_fingerprint(same_set), (
        "직렬화 차이를 내용 변경으로 오판한다"
    )


def test_progress_detected_on_changed_graph():
    """NEGATIVE: 내용이 바뀌면 다른 지문 — 무진전으로 오판하지 않는다.

    트리플 **수** 만 비교하면 add 1 / remove 1 이 상쇄돼 변경을 놓친다.
    """
    base = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "steel:A a owl:Class .\nsteel:B a owl:Class .\n")
    swapped = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
               "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
               "steel:A a owl:Class .\nsteel:C a owl:Class .\n")
    assert mt._graph_fingerprint(base) != mt._graph_fingerprint(swapped), (
        "트리플 수가 같고 내용이 다른데 같은 지문 — 수만 비교하고 있다"
    )


def test_no_progress_streak_is_wired_into_the_round_loop():
    """무진전 감지가 라운드 루프에 연결돼 있다 (소스 레벨 고정)."""
    import inspect

    src = inspect.getsource(mt._run_one_debate_round)
    assert "_graph_fingerprint" in src, "라운드 루프가 그래프 지문을 계산하지 않는다"
    assert "no_progress" in src, "무진전 상태를 기록하지 않는다"
