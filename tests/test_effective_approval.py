"""S3 담당 항목만으로는 합의를 막지 못한다 — 승인 재판정 회귀 가드.

2026-08-26 실측. `data/generated/tbox/debate_log.json` 의 배포 실행 3회
(8/22 48.1분 · 8/23 44.4분 · 8/25 45.7분) 에서 ``consensus_reached`` 가 **3/3
False** 였고, ``approved=True`` 는 11라운드 전부에서 **한 번도** 나오지 않았다.

합의 분기는 5개 AND 다:

    v_approved and s_approved and round_num >= _MIN_DEBATE_ROUNDS
        and not cq_block_fixable and not veto_lock_triggered

**첫 두 개가 항상 False 였으므로 나머지 세 조건은 평가된 적이 없다.** 데이터 갭
분리·veto 래치 해제·MIN_ROUNDS 는 실측상 도달 불가 코드였고, 최종 관문인 Jury
(``_handle_potential_consensus``) 도 호출되지 않았다.

## 리뷰어가 든 거부 사유는 S2 가 할 수 없는 일이었다

실측으로 S2 초안 → S3 완료를 대조하면:

    dcterms:source 근거율   0/100 (0%)  →  100/119 (84%)   step_15d
    클래스 수                    47      →       67         step_14/24 등
    (EquipmentManagement · AtmosphericMonitoring · EnergyConsumption ·
     EquipmentAsset 등 리뷰어가 요구한 추상 클래스 4개가 여기서 생긴다)

리뷰어는 이것들을 critical/high 로 반복 지적했다. LLM 자신도 ``deferred_to_s3=true``
를 25건에 표시했지만 severity 는 critical/high 로 남겨 승인을 막았다 — 프롬프트
``s3_deferred_block`` 지시 3("이 항목만으로 approved:false 를 내지 마라") 을 지키지
않은 것이다.

## 왜 프롬프트가 아니라 코드로 강제하는가

이 리포에서 네 번 반복된 실패 유형이다 — 프롬프트로 지시하고 산출물로 검증하지
않으면 지켜지는지 알 수 없다.
severity 는 신뢰하지 않고 표시(``deferred_to_s3`` / ``category``) 를 판정축으로
쓴다: 실측 25건 중 8건이 지시를 어기고 critical/high 로 왔지만 표시 자체는 정확했다.

## 한 방향으로만 뒤집는다

거부 → 승인만 한다. 승인 → 거부는 하지 않는다 — 리뷰어가 승인했는데 코드가 막으면
그것이 다시 "승인 0건" 을 만든다. 이 함수의 목적은 **Jury 에 도달시키는 것**이고,
Jury 는 여전히 독립적으로 ``production_ready`` 를 판정한다.

## 이 테스트의 방향

완화 변경이므로 "승인이 늘었다" 만 주장하면 게이트를 통째로 없애도 통과한다.
두 축을 함께 고정한다:

* POSITIVE — S3 담당/데이터 갭만 남은 거부는 승인으로 재판정된다
* NEGATIVE — **진짜 결함(disjoint 축 혼재, 방향 오류 등)은 여전히 거부를 유지한다**
"""
from __future__ import annotations

import pytest

from tools.multi_agent_tbox import _blocking_issues, _effective_approval


def _issue(**kw) -> dict:
    base = {
        "severity": "critical",
        "category": "logic",
        "target": "steel:Foo",
        "symptom": "...",
    }
    base.update(kw)
    return base


# ── POSITIVE: S3 담당 항목은 합의를 막지 못한다 ────────────────────────


def test_deferred_flag_does_not_block():
    """THE REGRESSION: ``deferred_to_s3`` 표시가 있으면 severity 와 무관하게 비차단.

    실측 사례: "ObjectProperty dcterms:source 근거율 0%" 가 critical +
    deferred_to_s3=true 로 왔다. S3 step_15d 가 84% 로 채우는 항목이다.
    """
    review = {
        "approved": False,
        "issues": [_issue(severity="critical", deferred_to_s3=True,
                          target="dcterms:source 근거율 0%")],
    }
    approved, blocking = _effective_approval(review)
    assert approved is True, "S3 담당 항목만으로 승인이 막혔다"
    assert blocking == []


@pytest.mark.parametrize("category", ["metric", "data_gap", "deferred"])
def test_non_blocking_category_does_not_block(category):
    """``metric`` / ``data_gap`` / ``deferred`` category 는 비차단."""
    review = {"approved": False, "issues": [_issue(category=category)]}
    approved, blocking = _effective_approval(review)
    assert approved is True, f"category={category} 가 승인을 막았다"
    assert blocking == []


def test_category_matching_is_case_insensitive():
    """LLM 이 ``Data_Gap`` / ``METRIC`` 으로 보내도 인식한다.

    이 리포는 키 대소문자·표기 변형으로 지시가 버려진 사고를 반복했다.
    """
    for raw in ("Data_Gap", "METRIC", " deferred "):
        review = {"approved": False, "issues": [_issue(category=raw)]}
        approved, _ = _effective_approval(review)
        assert approved is True, f"category={raw!r} 를 인식하지 못했다"


def test_medium_and_low_never_block():
    """medium/low 는 애초에 차단 대상이 아니다 — 기존 동작 보존."""
    review = {
        "approved": False,
        "issues": [_issue(severity="medium"), _issue(severity="low")],
    }
    approved, blocking = _effective_approval(review)
    assert approved is True
    assert blocking == []


def test_mixed_issues_only_real_ones_block():
    """S3 항목과 진짜 결함이 섞이면 진짜 결함만 남는다."""
    review = {
        "approved": False,
        "issues": [
            _issue(category="metric", target="RR 미달"),
            _issue(deferred_to_s3=True, target="inverseOf 불완전"),
            _issue(category="disjoint", target="AllDisjointClasses 축 혼재"),
        ],
    }
    approved, blocking = _effective_approval(review)
    assert approved is False, "진짜 결함이 있는데 승인됐다"
    assert len(blocking) == 1
    assert "AllDisjointClasses" in blocking[0]["target"]


# ── NEGATIVE: 정당한 거부는 유지된다 ────────────────────────────────────


@pytest.mark.parametrize(
    "category",
    ["logic", "inference", "disjoint", "cardinality", "standard",
     "mapping", "relationship", "competency", "process"],
)
def test_real_defect_categories_still_block(category):
    """S2 가 고칠 수 있는 category 는 그대로 승인을 막는다.

    이 주장이 없으면 ``_blocking_issues`` 가 항상 ``[]`` 를 반환해도 위 테스트가
    전부 통과한다 — 완화가 게이트 제거로 번지는 것을 막는 축이다.
    """
    review = {"approved": False, "issues": [_issue(category=category)]}
    approved, blocking = _effective_approval(review)
    assert approved is False, f"category={category} 가 차단력을 잃었다"
    assert len(blocking) == 1


def test_missing_category_blocks():
    """category 가 없으면 보수적으로 차단한다 — 판정 불가는 면제가 아니다."""
    review = {"approved": False, "issues": [{"severity": "critical", "target": "X"}]}
    approved, blocking = _effective_approval(review)
    assert approved is False
    assert len(blocking) == 1


def test_llm_approval_is_never_revoked():
    """``approved=True`` 는 코드가 뒤집지 않는다 — 한 방향 규칙."""
    review = {
        "approved": True,
        "issues": [_issue(category="disjoint", severity="critical")],
    }
    approved, blocking = _effective_approval(review)
    assert approved is True, "리뷰어 승인을 코드가 취소했다"
    assert blocking == []


def test_issues_are_not_mutated():
    """판정은 원본 이슈를 지우거나 바꾸지 않는다 — 기록은 남아야 한다.

    S3 가 실패했을 때 이 기록이 유일한 단서다 (프롬프트 지시 1).
    """
    issues = [_issue(category="metric"), _issue(category="logic")]
    review = {"approved": False, "issues": issues}
    _effective_approval(review)
    assert len(review["issues"]) == 2
    assert review["issues"][0]["category"] == "metric"
    assert review["issues"][0]["severity"] == "critical"


def test_malformed_issues_do_not_crash():
    """LLM 이 dict 아닌 항목을 섞어도 죽지 않는다."""
    review = {"approved": False, "issues": ["문자열 이슈", None, 42,
                                            _issue(category="logic")]}
    approved, blocking = _effective_approval(review)
    assert approved is False
    assert len(blocking) == 1


def test_empty_review_is_approved():
    """이슈가 아예 없으면 차단 근거가 없다."""
    approved, blocking = _effective_approval({"approved": False, "issues": []})
    assert approved is True
    assert blocking == []


def test_blocking_issues_returns_the_issue_dicts():
    """``_blocking_issues`` 는 카운트가 아니라 이슈 자체를 준다 — 로그에 실어야 한다."""
    issue = _issue(category="logic", target="steel:Bar")
    got = _blocking_issues({"issues": [issue]})
    assert got == [issue]


# ── 배선: 함수가 실제로 합의 분기에 연결됐는가 ──────────────────────────


def test_debate_round_is_wired_to_effective_approval():
    """``_run_one_debate_round`` 가 재판정 결과로 합의를 판정하는가.

    함수 단위 테스트만으로는 이 축을 볼 수 없다 — 이 리포에서 단위 테스트 14건이
    초록인데 산출물이 두 번 안 바뀐 사고가 있었고, 원인이 배선이었다. 소스를 AST 로
    읽어 확인한다.
    """
    import ast
    import pathlib

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_one_debate_round"
    )
    body = ast.unparse(fn)

    assert "_effective_approval(validator_result)" in body, "Validator 재판정 미배선"
    assert "_effective_approval(sme_result)" in body, "SME 재판정 미배선"
    assert "v_approved and s_approved" in body, "합의 분기가 재판정 결과를 쓰지 않는다"
    # raw approved 를 직접 읽어 합의를 판정하면 재판정이 우회된다.
    assert "validator_result.get('approved', False)" not in body, (
        "합의 판정이 LLM approved 를 직접 읽는다 — 재판정이 우회됐다"
    )
    assert "approval_recheck" in body, (
        "재판정 발화 여부가 round_log 에 안 남는다 — 산출물로 확인할 수 없다"
    )


def test_data_gap_block_is_wired_into_reviewer_prompt():
    """직전 라운드 데이터 갭이 리뷰어 preamble 로 주입되고 다음 라운드로 보관되는가."""
    import ast
    import pathlib

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_run_one_debate_round"
    )
    body = ast.unparse(fn)
    # import 문만 남고 **호출이 사라지는** 형태를 잡아야 한다. 문자열 포함 검사는
    # `from ... import cq_data_gap_block` 에 걸려 통과한다 (뮤테이션 생존 실측).
    assert "cq_data_gap_block(state.get('prev_cq_data_gap'))" in body, (
        "데이터 갭 블록이 직전 라운드 상태로 호출되지 않는다"
    )
    # **대입문 자체**를 검사한다. 앞뒤 slice 검사는 `if ... or data_gap_block:`
    # 조건문에 걸려, 실제 합침(대입)에서 빠져도 통과한다 (뮤테이션 생존 실측).
    assert "review_preamble = metrics_feedback + extra + data_gap_block" in body, (
        "빌드된 블록이 review_preamble 대입에 합쳐지지 않는다"
    )
    assert "state['prev_cq_data_gap'] = cq_block_data_gap" in body, (
        "판정 결과가 다음 라운드로 보관되지 않는다"
    )


def test_data_gap_block_is_empty_without_gaps():
    """갭이 없으면 빈 문자열 — 1라운드에 불필요한 블록을 넣지 않는다."""
    from tools.multi_agent_prompts import cq_data_gap_block
    assert cq_data_gap_block(None) == ""
    assert cq_data_gap_block([]) == ""


def test_data_gap_block_names_the_cqs():
    """블록이 실제 CQ id 와 사유를 담는가 — 리뷰어가 대조할 수 있어야 한다."""
    from tools.multi_agent_prompts import cq_data_gap_block
    out = cq_data_gap_block([("CQ02", "processblastfurnace → alarmevents 경로 없음")])
    assert "CQ02" in out
    assert "processblastfurnace" in out
    # 지시가 함께 있어야 한다 — 목록만 주면 리뷰어가 어떻게 다룰지 모른다.
    assert "approved" in out
    assert "data_gap" in out


def test_issue_trace_records_category():
    """``issue_trace`` 가 category 를 저장하는가 — 재판정 효과 측정의 전제.

    2026-08-26 감사에서 실제로 막혔다: category 가 없어 그 축의 효과를 배포 로그로
    재구성할 수 없었고 deferred_to_s3 만으로 추정해야 했다.
    """
    from tools.debate_log_store import _issue_trace

    # 문자열 검사는 쓸 수 없다 — 같은 파일 다른 함수에 `"category"` 가 이미 있어
    # 저장 코드를 지워도 통과한다(뮤테이션 생존 실측). 실제로 trace 를 만들어
    # 필드가 나오는지 본다.
    rounds = [{
        "round": 2,
        "validator": {"issues": [
            {"severity": "critical", "category": "disjoint",
             "target": "steel:Foo", "symptom": "축 혼재"},
        ]},
    }]
    trace = _issue_trace(rounds)
    assert trace, "issue_trace 가 비었다"
    assert "category" in trace[0], "issue_trace 레코드에 category 가 없다"
    assert trace[0]["category"] == "disjoint"
