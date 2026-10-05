"""FK OP 선택의 결정성 회귀 가드 — 동의어 OP 중 어느 쪽을 고를지 고정.

배경 (2026-07-26 실측): T-Box 는 같은 (domain, range) 를 갖는 동의어 OP 를 여럿
선언한다.

    steel:isSlabOf                  domain=MaterialA  range=ProcessStepA
    steel:isSlabProducedByBofHeat   domain=MaterialA  range=ProcessStepA   ← 같은 의미

`_build_fk_skeleton` 은 specificity 점수로 후보를 정렬했는데, **동점일 때 2차 키가
없었다.** 그래서 선택 결과가 `obj_props` 입력 순서에 좌우됐고, 그 순서는 T-Box 를
다시 직렬화하면 흔들린다.

실제 사고: T-Box 에 무관한 DP 2개를 추가하고 A-Box 를 재생성했을 뿐인데 OP 8쌍의
이름이 통째로 뒤바뀌었다.

    isSlabOf                 약 1.3만 → 0
    isSlabProducedByBofHeat       0 → 약 1.3만
    isSlabRecordOf           약 1.6만 → 0
    materialAMaterialRecordHasSlab     0 → 약 1.6만

데이터는 동일하지만 **기존 SPARQL 질의가 전부 0건**이 된다. SME 문서 §3 "AI 가
지은 이름을 기준으로 연결해 두었다" 와 같은 계열의 이름 드리프트다.

해법: 동점이면 이름 오름차순으로 고정 → T-Box 재직렬화·DP 추가 같은 무관한 변경에
대해 선택이 불변.
"""
from __future__ import annotations

import random

from tools.abox_generation import _candidate_sort_key


def _names(cands):
    return [p.get("name") for _, p in sorted(cands, key=_candidate_sort_key)]


def test_higher_score_wins():
    """점수가 높은 후보가 항상 먼저 — 기존 우선순위 정책 유지."""
    cands = [(2, {"name": "aaaLowScore"}), (12, {"name": "zzzHighScore"})]
    assert _names(cands)[0] == "zzzHighScore"


def test_tie_broken_by_name_ascending():
    """동점이면 이름 오름차순 — 사전순으로 결정."""
    cands = [(2, {"name": "isSlabProducedByBofHeat"}), (2, {"name": "isSlabOf"})]
    assert _names(cands) == ["isSlabOf", "isSlabProducedByBofHeat"]


def test_selection_invariant_to_input_order():
    """THE REGRESSION: 입력 순서가 바뀌어도 선택이 같아야 한다."""
    base = [(2, {"name": "isSlabProducedByBofHeat"}),
            (2, {"name": "isSlabOf"}),
            (2, {"name": "hasSlabRelation"})]
    expected = _names(base)
    rng = random.Random(20260726)          # 고정 시드 — 재현 가능
    for _ in range(50):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert _names(shuffled) == expected


def test_restriction_priority_still_beats_name_order():
    """restriction 가산점(+10)은 이름 순서보다 강해야 한다.

    이름만으로 고정하면 Jury 가 minCardinality 로 강제한 OP 가 알파벳 뒤라고
    밀려나 카디널리티 제약이 깨진다.
    """
    # 'aaa...' 가 이름순 앞이지만 점수가 낮다.
    cands = [(2, {"name": "aaaGenericOp"}), (12, {"name": "zzzRestrictedOp"})]
    assert _names(cands)[0] == "zzzRestrictedOp"


def test_missing_name_does_not_crash():
    """name 이 없는(비정상) 후보도 정렬 가능해야 한다 — 파이프라인 중단 방지."""
    cands = [(2, {}), (2, {"name": "realOp"}), (2, {"name": None})]
    out = _names(cands)
    assert len(out) == 3
    assert "realOp" in out


def test_sort_key_shape():
    """키는 (음수점수, 이름) — 점수 내림차순 + 이름 오름차순."""
    assert _candidate_sort_key((5, {"name": "x"})) == (-5, "x")
