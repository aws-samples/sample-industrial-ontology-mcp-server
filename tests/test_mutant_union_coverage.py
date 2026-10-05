"""meta_audit — distinct mutant 단위 **합집합** 커버리지를 내는가.

## 왜 (2026-09-05 실측)

``compute_blind_spots`` 는 mutant **인스턴스**(run × mutant)를 센다. 그런데 두 감사
단계는 서로 다른 검증기 집합을 돌린다::

    S4.5  syntax / quality / hermit / classify / shacl   (T-Box 5종)
    S9.5  validate_kg 25 check                           (KG)

그래서 한쪽만 잡는 결함이 양쪽 기록에 각각 세어져 비율이 절반쯤으로 희석된다. 실측
(meta_audit 15 runs): 6개 mutant 가 한쪽 단계에서만 잡히는데 카테고리 비율은 그것을
"coverage_gap" 으로 표시한다. 반대로 **어느 단계도 잡지 못하는** mutant 는 카테고리
비율에 묻힌다 — 그것이 이 감사가 답해야 하는 유일한 질문이다.

합집합 기준 실측: distinct 17개 중 covered 12 / uncovered 5.
"""
from __future__ import annotations

import glob
import json

import pytest

from tools.meta_audit import compute_action_playbook, compute_mutant_coverage


def _run(stage: str, mutants: list[dict]) -> dict:
    for m in mutants:
        m.setdefault("applied", True)
        m.setdefault("stage", stage)
        m.setdefault("category", m["mutant_id"].split("_")[0])
    return {"mutants": mutants}


# ── 합집합 판정 ────────────────────────────────────────────────────────


def test_caught_in_either_stage_is_covered():
    """THE REGRESSION: 한 단계만 잡아도 그 결함은 커버된 것이다."""
    runs = [
        _run("S4.5", [{"mutant_id": "M1_x/a", "caught_by": []}]),
        _run("S9.5", [{"mutant_id": "M1_x/a", "caught_by": ["체크X"]}]),
    ]
    coverage, uncovered = compute_mutant_coverage(runs)
    assert len(coverage) == 1
    entry = coverage[0]
    assert entry["covered"] is True
    assert entry["caught_by_stages"] == ["S9.5"]
    assert entry["seen_stages"] == ["S4.5", "S9.5"]
    assert entry["applied_runs"] == 2 and entry["caught_runs"] == 1
    assert uncovered == []


def test_never_caught_is_uncovered():
    runs = [
        _run("S4.5", [{"mutant_id": "M9_y/b", "caught_by": []}]),
        _run("S9.5", [{"mutant_id": "M9_y/b", "caught_by": []}]),
    ]
    _, uncovered = compute_mutant_coverage(runs)
    assert [u["mutant_id"] for u in uncovered] == ["M9_y/b"]
    assert uncovered[0]["seen_stages"] == ["S4.5", "S9.5"]


def test_unapplied_instances_are_ignored():
    """미주입은 miss 가 아니다 — 분모에 넣으면 미주입을 사각지대로 오독한다."""
    runs = [_run("S9.5", [
        {"mutant_id": "M1_x/a", "applied": False, "reason": "no_effect"},
    ])]
    coverage, uncovered = compute_mutant_coverage(runs)
    assert coverage == [] and uncovered == []


def test_blinded_checks_are_unioned():
    """가드를 눈멀게 한 기록은 합집합으로 모은다."""
    runs = [
        _run("S9.5", [{"mutant_id": "M7_r/s", "caught_by": [],
                       "blinded_checks": ["필수참여 공리 충족"]}]),
        _run("S9.5", [{"mutant_id": "M7_r/s", "caught_by": [], "blinded_checks": []}]),
    ]
    _, uncovered = compute_mutant_coverage(runs)
    assert uncovered[0]["blinded_checks"] == ["필수참여 공리 충족"]


def test_missing_blinded_field_is_tolerated():
    """옛 기록에는 blinded_checks 가 없다 — 그 때문에 깨지지 않아야 한다."""
    runs = [_run("S4.5", [{"mutant_id": "M1_x/a", "caught_by": []}])]
    _, uncovered = compute_mutant_coverage(runs)
    assert uncovered[0]["blinded_checks"] == []


# ── playbook 통합 ──────────────────────────────────────────────────────


def test_uncovered_mutants_lead_the_playbook():
    """미커버 mutant 는 high 우선순위로 playbook 에 실린다 (S13 이 렌더한다)."""
    uncovered = [{
        "mutant_id": "M7_restriction/some_to_all", "category": "M7",
        "applied_runs": 3, "seen_stages": ["S4.5"],
        "blinded_checks": ["필수참여 공리 충족"],
    }]
    pb = compute_action_playbook([], [], [], uncovered)
    assert pb, "playbook 이 비었다"
    first = pb[0]
    assert first["type"] == "uncovered_mutant"
    assert first["priority"] == "high"
    assert first["mutant_id"] == "M7_restriction/some_to_all"
    assert "필수참여 공리 충족" in first["issue"]
    assert first["suggested_action"]["blinded_checks"] == ["필수참여 공리 충족"]


def test_playbook_without_uncovered_is_unchanged():
    """PRESERVATION: 인자를 안 주면 기존 동작 그대로 (하위 호환)."""
    assert compute_action_playbook([], [], []) == []


# ── 표본 카테고리 ──────────────────────────────────────────────────────


def test_kg_sample_covers_checks_that_exist():
    """S9.5 표본이 validate_kg 에 축이 있는 카테고리를 모두 포함하는가.

    ``M7/some_to_all`` 은 ``필수참여 공리 충족`` 을 판정 불가로 만드는데, M7 이 표본에
    없으면 **KG 체크를 눈멀게 하는 변조가 KG 체크를 돌리는 단계에서 한 번도 적용되지
    않는다** (실측: seen_stages = ['S4.5'] 뿐).
    """
    from tools.mutation_runner import _KG_CATEGORIES

    for cat in ("M4", "M7"):
        assert cat in _KG_CATEGORIES, (
            f"{cat} 가 KG 표본에 없다 — 그 축의 KG 체크가 자극되지 않는다"
        )


def test_full_exposure_sample_size_is_documented():
    """전수 노출에 필요한 sample_size 를 소스가 밝히는가."""
    import inspect

    from tools import mutation_runner as mr

    src = inspect.getsource(mr)
    assert "sample_size=18" in src, (
        "카테고리를 늘렸으면 전수 노출 sample_size 를 문서에 남겨야 한다"
    )


# ── 산출물 확인 ────────────────────────────────────────────────────────


def test_real_records_show_complementary_coverage():
    """저장된 기록에서 상보성이 실제로 관측되는가.

    한쪽 단계만 잡는 mutant 가 존재해야 이 축을 만든 이유가 성립한다.
    """
    runs = []
    for path in sorted(glob.glob("data/generated/meta_audit/runs/*/*.json")):
        with open(path, encoding="utf-8") as handle:
            runs.append(json.load(handle))
    if not runs:
        pytest.skip("mutation 기록 없음")
    coverage, uncovered = compute_mutant_coverage(runs)
    if not coverage:
        pytest.skip("주입된 mutant 기록 없음")

    one_sided = [
        c for c in coverage
        if c["covered"] and set(c["caught_by_stages"]) != set(c["seen_stages"])
    ]
    assert one_sided, (
        "한쪽만 잡는 mutant 가 없다 — 두 감사가 같은 것을 본다면 이 축은 불필요하다"
    )
    # 합집합이 인스턴스 비율보다 낙관적이어야 한다 (희석이 실재함을 보인다).
    total_applied = sum(c["applied_runs"] for c in coverage)
    total_caught = sum(c["caught_runs"] for c in coverage)
    union_rate = (len(coverage) - len(uncovered)) / len(coverage)
    instance_rate = total_caught / total_applied
    assert union_rate > instance_rate, (
        f"합집합 {union_rate:.2f} 이 인스턴스 {instance_rate:.2f} 보다 크지 않다"
    )
