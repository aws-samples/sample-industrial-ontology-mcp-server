"""dead-check 검출기는 침묵이 아니라 **오발화 대기** 상태였다.

2026-08-25 실측. 나는 처음에 "history 가 1건뿐이라 `min_history=5` 게이트를 넘지
못해 검출기가 발화하지 못한다" 로 진단했다. **그 진단은 방향이 반대였다.**

## 사실 1 — 게이트가 열리는 순간 핵심 게이트를 제거 후보로 인쇄한다

파이프라인 한 바퀴(save_step 5건 + validate_kg 2건)만 돌면 `len(history) >= 5` 가
되어 게이트가 열린다. 그 상태로 판정하면 26개 중 **23개** 가 dead 로 지목되고,
목록에 이런 것들이 들어 있었다::

    AllDisjointClasses 위반 / FK 참조 무결성 / domain/range 타입 정합성 /
    카디널리티 제약 위반

그 권고(`consider_removal`)는 S13 HTML 보고서에 "Dead-Check Candidates" 로 자동
렌더된다. 오탐률 ≈ 95% 다 — 이번 세션에 21개가 실측으로 살아 있음이 증명됐다.

## 사실 2 — KG 가 건강할수록 더 나빠진다

`history_fires` 는 "그 check 가 **실패한** 횟수" 다. 완벽하게 건강한 이력
(23/23 × 10회, `failed_checks` 전부 빈 배열) 을 넣으면 26개 중 **24개** 가 dead 로
지목된다. 목표가 건강한 KG 인데 건강해질수록 검증 제거를 권고하는 것은 **원리적
결함** 이다. 그래서 history 를 보조로 내리고 노출량·baseline 축을 함께 본다.

## 사실 3 — 분모가 "신호 없는 엔트리" 를 센다

`quality_history.json` 에는 스키마가 다른 두 writer 가 쓴다. `pipeline_state` 의
`{step, metrics, duration_seconds}` 에는 `failed_checks` 가 **없다**. `_load_history`
는 스키마를 검사하지 않으므로 그 엔트리도 `min_history` 분모에 들어간다 — 즉
`save_step` 만으로 게이트를 열어 표본 부족 오탐을 "근거 있는 판정" 처럼 출력할 수
있다.

## 판정을 네 갈래로 나눈다

    insufficient_exposure      노출 < 8회 → 판정 불가, 제거 권고 안 함
    structurally_undetectable  baseline 이 FAIL → 등급 상승 불가. 고칠 것은 baseline
    no_probe_available         그 축의 mutant 가 카탈로그에 없음 (syntax)
    consider_removal           충분히 노출 + baseline 건강 + 그래도 0 catch

현재 데이터에서 `consider_removal` 은 **0건** 이다 (23 → 0).
"""
from __future__ import annotations

import collections

import pytest

from tools.meta_audit import (
    _MIN_MUTATION_EXPOSURE,
    _load_mutation_runs,
    compute_action_playbook,
    compute_dead_checks,
)

_RUNS = "data/generated/meta_audit/runs"


def _mutant(
    checks: dict, caught: list | None = None, applied: bool = True,
    *, strengthens: bool = True, category: str = "M1",
) -> dict:
    """mutant 레코드.

    ``strengthens=True`` 는 트리플을 **추가** 하는 mutant 다. OWL 단조성 때문에
    삭제 mutant 는 일관성 계열 check 를 자극할 수 없으므로 노출로 세지 않는다.
    """
    return {
        "applied": applied,
        "baseline_summary": checks,
        "mutant_summary": checks,
        "caught_by": caught or [],
        "triples_added": 1 if strengthens else 0,
        "triples_removed": 0 if strengthens else 1,
        # 카테고리는 노출 귀속에 쓰인다 — 기본 M1 은 hermit/shacl/domain·range 를
        # 자극하는 축이다. 픽스처의 check 이름 "A" 는 어느 카테고리에도 없으므로
        # 관련성 표를 우회해야 하는 테스트는 category 를 명시한다.
        "category": category,
    }


def _kg_entry(failed: list) -> dict:
    return {"timestamp": "t", "score": "1/2", "passed": False, "failed_checks": failed}


def _step_entry() -> dict:
    """``pipeline_state`` writer 스키마 — ``failed_checks`` 가 없다."""
    return {"timestamp": "t", "step": "S2_TBOX", "metrics": {}, "duration_seconds": 1}


# ──────────────────────────────────────────────────────────────────
# 1. 노출 부족을 사형 선고로 바꾸지 않는다 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

def test_low_exposure_is_not_removal_candidate():
    """노출 2회로는 어떤 결론도 낼 수 없다."""
    runs = [{"mutants": [_mutant({"hermit": "PASS"}), _mutant({"hermit": "PASS"})]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert len(dead) == 1
    assert dead[0]["recommendation"] == "insufficient_exposure", dead
    assert dead[0]["mutation_exposure"] == 2


def test_sufficient_exposure_with_no_catch_is_removal_candidate():
    """충분히 노출됐고 baseline 도 건강한데 0 catch 면 진짜 제거 후보다."""
    runs = [{"mutants": [_mutant({"hermit": "PASS"}) for _ in range(_MIN_MUTATION_EXPOSURE)]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["recommendation"] == "consider_removal", dead


def test_unapplied_mutants_do_not_count_as_exposure():
    """심어지지 않은 결함은 노출이 아니다.

    ``no_effect`` mutant 를 노출로 세면 "충분히 시험했다" 는 착각이 생긴다.
    """
    runs = [{"mutants": [
        _mutant({"hermit": "PASS"}, applied=False) for _ in range(20)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["mutation_exposure"] == 0
    assert dead[0]["recommendation"] == "insufficient_exposure"


# ──────────────────────────────────────────────────────────────────
# 2. baseline FAIL 은 check 잘못이 아니다
# ──────────────────────────────────────────────────────────────────

def test_baseline_failing_check_is_structurally_undetectable():
    """baseline 이 FAIL 이면 등급 상승이 불가능해 어떤 mutant 도 못 잡는다.

    ``_caught_by`` 는 PASS→WARN/FAIL 강등만 센다. 이 상태의 check 를 제거하면
    baseline 을 고칠 기회까지 사라진다.
    """
    runs = [{"mutants": [
        _mutant({"hermit": "FAIL"}) for _ in range(_MIN_MUTATION_EXPOSURE + 4)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["recommendation"] == "structurally_undetectable", dead
    assert dead[0]["baseline_failing_runs"] == _MIN_MUTATION_EXPOSURE + 4


def test_partially_failing_baseline_is_not_structural():
    """일부 run 에서만 FAIL 이면 구조적 불가가 아니다 (과잉 관용 방지)."""
    runs = [{"mutants": (
        [_mutant({"hermit": "FAIL"})] * 3 + [_mutant({"hermit": "PASS"})] * 9
    )}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["recommendation"] == "consider_removal", dead


# ──────────────────────────────────────────────────────────────────
# 3. 자극 mutant 가 없는 축
# ──────────────────────────────────────────────────────────────────

def test_unstimulable_check_is_not_removal_candidate():
    """``syntax`` 는 SPARQL UPDATE 산출물이 항상 유효 Turtle 이라 발화할 수 없다."""
    runs = [{"mutants": [
        _mutant({"syntax": "PASS"}) for _ in range(_MIN_MUTATION_EXPOSURE + 20)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"syntax"})
    assert dead[0]["recommendation"] == "no_probe_available", dead


# ──────────────────────────────────────────────────────────────────
# 4. 발화한 check 는 dead 가 아니다 (주 방향)
# ──────────────────────────────────────────────────────────────────

def test_check_that_caught_a_mutant_is_alive():
    runs = [{"mutants": [_mutant({"hermit": "PASS"}, caught=["hermit"])]}]
    assert compute_dead_checks(runs, [_kg_entry([])], {"hermit"}) == []


def test_check_that_failed_in_history_is_alive():
    runs = [{"mutants": [_mutant({"hermit": "PASS"}) for _ in range(20)]}]
    assert compute_dead_checks(runs, [_kg_entry(["hermit"])], {"hermit"}) == []


# ──────────────────────────────────────────────────────────────────
# 5. 분모는 신호를 담은 엔트리만 센다
# ──────────────────────────────────────────────────────────────────

def test_step_schema_entries_do_not_contribute_signal():
    """``failed_checks`` 없는 엔트리는 신호 0 이다 — 분모를 부풀리지 않는다."""
    runs = [{"mutants": [_mutant({"hermit": "PASS"}) for _ in range(20)]}]
    dead = compute_dead_checks(runs, [_step_entry() for _ in range(10)], {"hermit"})
    assert dead[0]["history_signal_entries"] == 0, (
        "step 스키마 엔트리를 신호로 셌다 — save_step 만으로 게이트가 열린다"
    )


def test_signal_entries_are_counted_separately():
    runs = [{"mutants": [_mutant({"hermit": "PASS"}) for _ in range(20)]}]
    history = [_step_entry(), _kg_entry([]), _step_entry(), _kg_entry([])]
    dead = compute_dead_checks(runs, history, {"hermit"})
    assert dead[0]["history_signal_entries"] == 2


# ──────────────────────────────────────────────────────────────────
# 6. playbook 이 판정별로 다른 액션을 낸다
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("verdict", "kind"), [
    ("insufficient_exposure", "increase_mutation_coverage"),
    ("structurally_undetectable", "fix_baseline_first"),
    ("no_probe_available", "add_mutation_category"),
    ("consider_removal", "investigate_or_remove"),
])
def test_playbook_action_matches_verdict(verdict, kind):
    """판정에 맞는 액션이 나온다.

    전부 ``investigate_or_remove`` 로 두면 표본 부족과 진짜 죽음이 같은 권고를
    받고, 그것이 S13 보고서에 "제거 후보" 로 인쇄된다.
    """
    dead = [{"check": "X", "recommendation": verdict, "mutation_exposure": 3}]
    pb = compute_action_playbook([], dead, [])
    entries = [e for e in pb if e.get("type") == "dead_check"]
    assert entries, pb
    assert entries[0]["suggested_action"]["kind"] == kind
    assert entries[0]["verdict"] == verdict


def test_only_true_removal_verdict_says_remove():
    """제거를 말하는 액션은 ``consider_removal`` 하나뿐이다."""
    dead = [
        {"check": "A", "recommendation": "insufficient_exposure", "mutation_exposure": 1},
        {"check": "B", "recommendation": "structurally_undetectable", "mutation_exposure": 9},
        {"check": "C", "recommendation": "no_probe_available", "mutation_exposure": 9},
    ]
    pb = compute_action_playbook([], dead, [])
    kinds = {e["suggested_action"]["kind"] for e in pb if e.get("type") == "dead_check"}
    assert "investigate_or_remove" not in kinds, (
        f"제거를 권고해서는 안 되는 판정에 제거 액션이 붙었다: {kinds}"
    )


# ──────────────────────────────────────────────────────────────────
# 7. 배포 산출물 회귀 — 핵심 게이트가 제거 후보로 인쇄되지 않는다
# ──────────────────────────────────────────────────────────────────

_CORE_GATES = (
    "FK 참조 무결성", "domain/range 타입 정합성",
    "카디널리티 제약 위반", "AllDisjointClasses 위반",
    # S4.5 T-Box 축. 실측으로 여기서 샜다 — 삭제 mutant 27회를 노출로 세는 바람에
    # 임계 8 을 넘어 consider_removal 이 됐다. hermit 은 이 리포에서 가장 하중이
    # 큰 게이트다 (그 침묵이 unsat 4건을 덮은 이력이 있다).
    "hermit", "classify",
)


def _deployed_runs():
    import os
    if not os.path.isdir(_RUNS):
        pytest.skip("mutation run 산출물 없음")
    runs = _load_mutation_runs(_RUNS)
    if not runs:
        pytest.skip("mutation run 0건")
    seen: set[str] = set()
    for r in runs:
        for m in r.get("mutants", []):
            if m.get("applied"):
                seen.update(m.get("baseline_summary") or {})
    return runs, seen


def test_core_gates_are_never_removal_candidates_on_deployed_data():
    """THE REGRESSION: 실측 데이터로 핵심 게이트가 제거 후보가 되지 않는다.

    게이트가 열린 상태(history 신호 있음)를 가정해도 그렇다.
    """
    runs, seen = _deployed_runs()
    history = [_kg_entry(["추론 sanity check"]) for _ in range(6)]
    dead = compute_dead_checks(runs, history, seen)
    removal = {d["check"] for d in dead if d["recommendation"] == "consider_removal"}
    leaked = removal & set(_CORE_GATES)
    assert not leaked, f"핵심 게이트가 제거 후보로 지목됐다: {sorted(leaked)}"


def test_healthy_history_does_not_mass_flag_removal():
    """건강한 KG 가 대량 제거 권고를 만들지 않는다.

    실측: 예전 판정식은 23/23 × 10회 이력에서 26개 중 24개를 dead 로 지목했다.
    """
    runs, seen = _deployed_runs()
    healthy = [
        {"timestamp": f"t{i}", "score": "23/23", "passed": True, "failed_checks": []}
        for i in range(10)
    ]
    dead = compute_dead_checks(runs, healthy, seen)
    removal = [d for d in dead if d["recommendation"] == "consider_removal"]
    assert len(removal) <= 3, (
        f"건강한 이력에서 제거 후보 {len(removal)}개 — 건강해질수록 나빠진다: "
        f"{[d['check'] for d in removal][:8]}"
    )


def test_deployed_verdict_distribution_is_dominated_by_exposure():
    """현 데이터의 대부분은 "판정 불가" 여야 한다 (노출이 부족하므로)."""
    runs, seen = _deployed_runs()
    dead = compute_dead_checks(runs, [_kg_entry([])], seen)
    dist = collections.Counter(d["recommendation"] for d in dead)
    assert dist["insufficient_exposure"] >= dist["consider_removal"], dist


# ──────────────────────────────────────────────────────────────────
# 8. 배포 이력이 테스트로 오염되지 않는다 (격리 자체를 지킨다)
# ──────────────────────────────────────────────────────────────────

def test_conftest_isolates_both_history_writers():
    """``quality_history.json`` writer 가 **둘** 이고 둘 다 격리돼야 한다.

    2026-08-25 실측 — history 가 1건에 머문 진짜 원인:

      - ``tools/pipeline_state.py``           → ``dirname(_STATE_PATH)``
      - ``tools/validation_support/…``        → ``dirname(GENERATED_ABOX_DIR)``

    conftest 가 ``_STATE_PATH`` 만 돌려놨어서 ``validate_kg`` 경로는 그대로 배포
    파일에 썼다. 그 테스트들은 ``_load_graph`` 를 빈 그래프로 mock 하므로
    ``triples: 0`` 짜리 mock 엔트리가 **실측 항목을 교체** 했다. 테스트 1건만
    돌려도 배포 파일 md5 가 바뀌는 것을 확인했다.

    이 테스트는 격리가 실제로 걸려 있는지 **런타임에** 확인한다 — 두 상수가 모두
    리포 밖(tmp)을 가리켜야 한다. 소스 문자열 검사가 아니라 값 검사여야 fixture 가
    조용히 빠지는 것을 잡는다.
    """
    import os

    import tools.kg_validation as kgv
    import tools.pipeline_state as ps

    repo_data = os.path.abspath("data/generated")
    for label, value in (
        ("kg_validation.GENERATED_ABOX_DIR", os.path.abspath(kgv.GENERATED_ABOX_DIR)),
        ("pipeline_state._STATE_PATH", os.path.abspath(ps._STATE_PATH)),
    ):
        assert not value.startswith(repo_data), (
            f"{label} 이 배포 경로를 가리킨다 ({value}) — "
            "테스트가 quality_history.json 을 오염시킨다"
        )


def test_validate_kg_does_not_touch_deployed_history():
    """``validate_kg`` 를 불러도 배포 이력 파일이 변하지 않는다 (산출물 검증).

    상수 검사만으로는 부족하다 — 경로 파생이 바뀌면 상수는 tmp 인데 쓰기는 배포로
    갈 수 있다. 실제 파일 해시를 대조한다.
    """
    import hashlib
    import json
    import os

    from tools.validation_support.quality_history import append_quality_history

    deployed = "data/generated/quality_history.json"
    if not os.path.exists(deployed):
        pytest.skip("배포 이력 파일 없음")
    with open(deployed, "rb") as fh:
        before = hashlib.md5(fh.read(), usedforsecurity=False).hexdigest()

    # kg writer 를 직접 호출한다 (autouse fixture 의 격리가 걸려 있어야 한다).
    append_quality_history({
        "score": "1/23", "passed": False, "triples": 0,
        "source": "test", "checks": [],
    })

    with open(deployed, "rb") as fh:
        after = hashlib.md5(fh.read(), usedforsecurity=False).hexdigest()
    assert before == after, (
        "테스트가 배포 quality_history.json 을 변경했다 — "
        "conftest 격리가 kg writer 를 막지 못한다"
    )
    # 그리고 tmp 쪽에는 실제로 쓰였는지 확인 (격리가 no-op 이 아님)
    import tools.kg_validation as kgv
    tmp_hist = os.path.join(
        os.path.dirname(kgv.GENERATED_ABOX_DIR), "quality_history.json",
    )
    assert os.path.exists(tmp_hist), "격리 경로에도 쓰이지 않았다 — writer 가 죽었다"
    with open(tmp_hist, encoding="utf-8") as fh:
        assert json.load(fh), "격리 경로 이력이 비어 있다"


# ──────────────────────────────────────────────────────────────────
# 9. 노출은 **강화(추가)** mutant 만 센다 (OWL 단조성)
# ──────────────────────────────────────────────────────────────────

def test_deletion_mutants_do_not_count_as_exposure():
    """삭제 mutant 는 일관성 계열 check 를 자극할 수 없다.

    OWL 은 단조 논리다 — 공리를 지우면 함의가 **약해질 뿐** 이라 HermiT 가
    unsatisfiable 을 보고할 수 없다. 실측 (2026-08-25): 적용 mutant 는 삭제 27 /
    추가 2 로 삭제가 압도하는데, 삭제를 노출로 세면 ``hermit``/``classify`` 가
    27회 노출로 임계를 넘어 ``consider_removal`` 로 샌다. 둘 다 오탐이다.
    """
    runs = [{"mutants": [
        _mutant({"hermit": "PASS"}, strengthens=False) for _ in range(30)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["mutation_exposure"] == 0, (
        f"삭제 mutant 를 노출로 셌다: {dead[0]}"
    )
    assert dead[0]["recommendation"] == "insufficient_exposure"


def test_addition_mutants_still_reach_removal_verdict():
    """과잉 교정 방지 — 강화 mutant 로 충분히 노출되면 제거 후보가 된다.

    이 방향이 없으면 "노출을 아무것도 안 센다" 로 게이트를 꺼서 오탐 0 을 만들 수
    있다 (지표 매수).
    """
    runs = [{"mutants": [
        _mutant({"hermit": "PASS"}, strengthens=True)
        for _ in range(_MIN_MUTATION_EXPOSURE)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["recommendation"] == "consider_removal", dead
    assert dead[0]["mutation_exposure"] == _MIN_MUTATION_EXPOSURE


def test_deployed_consistency_gates_are_not_removal_candidates():
    """실측 회귀: ``hermit``/``classify`` 가 제거 후보로 지목되지 않는다."""
    runs, seen = _deployed_runs()
    history = [_kg_entry(["추론 sanity check"]) for _ in range(6)]
    dead = compute_dead_checks(runs, history, seen)
    removal = {d["check"] for d in dead if d["recommendation"] == "consider_removal"}
    leaked = removal & {"hermit", "classify"}
    assert not leaked, (
        f"일관성 게이트가 제거 후보로 지목됐다: {sorted(leaked)} — "
        "삭제 mutant 를 노출로 세고 있다"
    )


# ──────────────────────────────────────────────────────────────────
# 10. 노출은 **관련 카테고리** 만 센다
# ──────────────────────────────────────────────────────────────────

def test_irrelevant_category_does_not_count_as_exposure():
    """M5(label/comment) 를 30회 돌려도 ``hermit`` 을 시험한 것이 아니다.

    노출이 카테고리 관련성을 무시하면 "30회 시험했는데 못 잡았다" 로 집계돼
    무관한 mutant 만 많이 돌린 run 이 핵심 게이트를 제거 후보로 만든다.
    ``_MUTATION_CATEGORY_EXPECTED_CATCHERS`` 가 그 관련성 표다.
    """
    runs = [{"mutants": [
        _mutant({"hermit": "PASS"}, category="M5") for _ in range(30)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["mutation_exposure"] == 0, (
        f"무관 카테고리를 노출로 셌다: {dead[0]}"
    )
    assert dead[0]["recommendation"] == "insufficient_exposure"


def test_relevant_category_counts_as_exposure():
    """관련 카테고리는 세야 한다 — 과잉 교정 방지 (주 방향).

    이 방향이 없으면 "아무 카테고리도 관련 없다" 로 노출을 0 으로 만들어
    제거 후보를 영구히 없앨 수 있다 (지표 매수).
    """
    runs = [{"mutants": [
        _mutant({"hermit": "PASS"}, category="M1")
        for _ in range(_MIN_MUTATION_EXPOSURE)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["mutation_exposure"] == _MIN_MUTATION_EXPOSURE
    assert dead[0]["recommendation"] == "consider_removal", dead


def test_unknown_category_is_counted_conservatively():
    """표에 없는 카테고리는 세지 않는다 — 미측정이 오탐보다 안전하다."""
    runs = [{"mutants": [
        _mutant({"hermit": "PASS"}, category="M99") for _ in range(30)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {"hermit"})
    assert dead[0]["mutation_exposure"] == 0
    assert dead[0]["recommendation"] == "insufficient_exposure"


def test_kg_display_names_are_attributed_by_category():
    """KG 표시 이름도 관련성 표로 귀속된다 (별칭 전용이 아니다).

    표적 이름은 **관련성 표에서 읽는다.** 예전에는 ``FK 참조 무결성`` 을 박아 뒀는데,
    M6 mutator 3종이 전부 T-Box ``rdfs:range``/``domain`` 만 변조해 A-Box FK 축을
    자극할 수 없다는 것이 실측으로 드러나 M6 목록에서 빠졌다 (2026-09-05). 이름을 박으면
    표를 고칠 때마다 이 테스트가 무관하게 깨진다 — 주장은 "KG 표시 이름이 귀속되는가" 다.
    """
    from tests.helpers_check_names import kg_check_display_names
    from tools.meta_audit import _MUTATION_CATEGORY_EXPECTED_CATCHERS

    kg_names = kg_check_display_names()
    target = next(
        (c for c in _MUTATION_CATEGORY_EXPECTED_CATCHERS["M6"] if c in kg_names),
        None,
    )
    assert target, (
        "M6 관련성 표에 KG 표시 이름이 하나도 없다 — 이 축을 검증할 수 없다"
    )
    runs = [{"mutants": [
        _mutant({target: "PASS"}, category="M6")
        for _ in range(_MIN_MUTATION_EXPOSURE)
    ]}]
    dead = compute_dead_checks(runs, [_kg_entry([])], {target})
    assert dead[0]["mutation_exposure"] == _MIN_MUTATION_EXPOSURE, dead
