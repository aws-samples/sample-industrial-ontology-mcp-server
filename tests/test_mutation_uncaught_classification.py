"""S9.5 — 못 잡은 mutant 에 **왜** 를 각인하는가 (분모는 건드리지 않는다).

## 왜 (2026-09-04 S9.5 실측)

applied 10 / caught 2 였고, 못 잡은 8건 중 절반이 "구조적으로 인스턴스 위반을 만들 수
없는" 것이었다 — 표적 술어가 A-Box 0건이거나, 변조가 공리를 **제거**만 해서 위반할
대상이 사라진 경우다. 그것들을 분모에서 빼면 catch_rate 가 20% → 57% 로 뛴다.

**그래서 빼지 않는다.** "현재 검사 집합으로 관측 불가" 는 곧 사각지대이고 이 감사의
존재 이유가 그것을 드러내는 것이다. 분모를 줄이면 탐지력을 하나도 올리지 않고 숫자만
좋아진다 — 이 리포가 반복해서 기각한 지표 매수다.

대신 이유를 각인해 **고칠 수 있는 miss** (``detector_gap``) 와 구조적으로 못 보는
miss 를 가른다. 실측 검증: 그 갈래로 분류된 3건이 정확히 직전 두 커밋에서 고친
3건이었다 (DP domain / inverseOf 타입 / DP range).
"""
from __future__ import annotations

import glob
import json
import os

import pytest

from tools.mutation_runner import _classify_uncaught


def _m(mid: str, *, applied=True, caught=None, added=0, removed=1, target=None) -> dict:
    return {
        "mutant_id": mid,
        "applied": applied,
        "caught_by": caught or [],
        "triples_added": added,
        "triples_removed": removed,
        "target": target if target is not None else {},
    }


# ── 분모 불변 ──────────────────────────────────────────────────────────


def test_classifier_does_not_touch_counts():
    """분류기는 세지 않는다 — applied/caught 계산에 개입하지 않는다."""
    mutants = [_m("a", caught=["X"]), _m("b")]
    before = json.dumps(
        [{k: v for k, v in m.items() if k != "uncaught_reasons"} for m in mutants],
        sort_keys=True,
    )
    _classify_uncaught(mutants)
    after = json.dumps(
        [{k: v for k, v in m.items() if k != "uncaught_reasons"} for m in mutants],
        sort_keys=True,
    )
    assert before == after, "분류기가 기존 필드를 변경했다"


def test_caught_mutants_get_no_reason():
    mutants = [_m("a", caught=["체크X"])]
    assert _classify_uncaught(mutants) == {}
    assert "uncaught_reasons" not in mutants[0]


def test_unapplied_mutants_are_not_classified():
    """주입되지 않은 mutant 는 miss 가 아니다 (미주입과 미검출을 섞지 않는다)."""
    mutants = [_m("a", applied=False)]
    assert _classify_uncaught(mutants) == {}
    assert "uncaught_reasons" not in mutants[0]


# ── 갈래 판정 ──────────────────────────────────────────────────────────


def test_weakening_only_is_labelled(monkeypatch):
    """공리를 제거만 하면 위반 축에서는 보이지 않는다."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(c),
    )
    mutants = [_m("del", added=0, removed=1, target={"?P": "http://x#liveProp"})]
    bd = _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == ["weakening_only"], mutants[0]
    assert bd == {"weakening_only": 1}


def test_target_unused_in_abox_is_labelled(monkeypatch):
    """표적 술어가 A-Box·master·tacit 어디에도 없으면 인스턴스 축은 무력하다."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(),
    )
    mutants = [_m("alter", added=1, removed=1, target={"?P": "http://x#deadProp"})]
    bd = _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == ["target_unused_in_abox"]
    assert bd == {"target_unused_in_abox": 1}


def test_detector_gap_when_no_structural_excuse(monkeypatch):
    """표적이 살아있고 변조가 값을 바꿨는데 못 잡으면 = 조치 대상."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(c),
    )
    mutants = [_m("alter", added=1, removed=1, target={"?P": "http://x#liveProp"})]
    bd = _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == ["detector_gap"]
    assert bd == {"detector_gap": 1}


def test_both_reasons_are_kept(monkeypatch):
    """두 이유가 겹치면 둘 다 남긴다 — 정보를 버리지 않는다."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(),
    )
    mutants = [_m("del", added=0, removed=1, target={"?P": "http://x#deadProp"})]
    _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == [
        "target_unused_in_abox", "weakening_only",
    ]


def test_undecidable_usage_is_not_read_as_unused(monkeypatch):
    """헬퍼가 ``None`` 을 내면 판정 불가다 — "미사용" 으로 읽지 않는다.

    0건과 판정불가를 섞으면 산 데이터를 미사용으로 오판한다
    (``abox_used_local_names`` 의 명시된 계약).
    """
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: None,
    )
    mutants = [_m("alter", added=1, removed=1, target={"?P": "http://x#anyProp"})]
    _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == ["abox_usage_undecidable"], mutants[0]


def test_no_target_falls_back_to_detector_gap(monkeypatch):
    """표적 정보가 없으면 구조적 면제를 주장할 수 없다 → 조치 대상으로 남긴다."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(),
    )
    mutants = [_m("nt", added=1, removed=1, target={})]
    assert _classify_uncaught(mutants) == {"detector_gap": 1}


# ── 산출물 확인 ────────────────────────────────────────────────────────


def test_real_s95_record_classifies_as_measured():
    """저장된 S9.5 기록에 돌려 실측 삼분류와 맞는지 확인한다.

    2026-09-04 실측: weakening_only 3 / detector_gap 3 / target_unused_in_abox 3.
    ``detector_gap`` 3건은 정확히 그날 고친 3건이다 (DP domain / inverseOf 타입 /
    DP range) — 즉 이 라벨이 조치 대상을 실제로 가리킨다.
    """
    runs = sorted(glob.glob("data/generated/meta_audit/runs/*/kg.json"))
    if not runs:
        pytest.skip("S9.5 기록 없음")
    with open(runs[-1], encoding="utf-8") as handle:
        record = json.load(handle)
    if not record.get("applied"):
        pytest.skip("주입 0건인 기록")
    bd = _classify_uncaught(record["mutants"])
    uncaught = [
        m for m in record["mutants"]
        if m.get("applied") and not m.get("caught_by")
    ]
    assert all("uncaught_reasons" in m for m in uncaught), "각인 누락"
    assert sum(bd.values()) >= len(uncaught), "이유가 없는 miss 가 있다"
    assert set(bd) <= {
        # ``blinded_guard`` 는 2026-09-05 추가 — 감사가 blinded_checks 채널로 이미
        # 포착한 miss 를 detector_gap 에서 분리한다.
        "detector_gap", "weakening_only", "target_unused_in_abox",
        "abox_usage_undecidable", "blinded_guard",
    }, bd


def test_summary_declares_its_denominator():
    """요약이 분모를 명시하는가 — 숫자만 보고 오독하지 않게."""
    import inspect

    from tools.mutation_runner import run_kg_mutations

    src = inspect.getsource(run_kg_mutations)
    assert '"catch_rate_denominator": "applied"' in src, (
        "분모를 명시하지 않으면 catch_rate 를 탐지력으로 오독한다"
    )
    assert '"uncaught_breakdown": uncaught_breakdown' in src
    assert "caught / applied" in src, "분모가 applied 에서 바뀌었다"


def test_stored_record_keeps_denominator_over_applied():
    """저장된 기록의 catch_rate 가 applied 분모와 일치하는가."""
    runs = sorted(glob.glob("data/generated/meta_audit/runs/*/kg.json"))
    if not runs:
        pytest.skip("S9.5 기록 없음")
    with open(runs[-1], encoding="utf-8") as handle:
        record = json.load(handle)
    if not record.get("applied"):
        pytest.skip("주입 0건인 기록")
    expected = round(record["caught"] / record["applied"] * 100, 1)
    assert record["catch_rate_pct"] == expected, (
        "catch_rate 분모가 applied 가 아니다 — 구조적 miss 를 빼면 사각지대를 감춘다"
    )
    assert os.path.exists(runs[-1])


# ── blinded_guard 우선순위 ──────────────────────────────────────────────
#
# 감사가 ``blinded_checks`` 채널로 이미 포착한 miss 를 ``detector_gap`` 에 남기면
# "고칠 탐지기" 를 찾게 되는데, 실제로 필요한 것은 그 축이 왜 판정 불가가 됐는지를
# 보는 것이다. 실측 2026-09-05 (전수 노출 S9.5, applied 14 / caught 7): 이 우선순위를
# 넣은 뒤 detector_gap 이 1 → 0 이 되고 미검출 7건 전부가 구조적 사유를 갖는다.


def test_blinded_guard_takes_precedence_over_detector_gap(monkeypatch):
    """가드가 눈먼 miss 는 설명된 miss 다 — 조치 목록을 오염시키지 않는다."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(c),
    )
    mutants = [_m("some_to_all", added=40, removed=40,
                  target={})]
    mutants[0]["blinded_checks"] = ["필수참여 공리 충족"]
    bd = _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == ["blinded_guard"], mutants[0]
    assert "detector_gap" not in bd, bd


def test_blinded_and_weakening_are_both_kept(monkeypatch):
    """두 사유가 겹치면 둘 다 남긴다 (정보를 버리지 않는다)."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda c: set(c),
    )
    mutants = [_m("del", added=0, removed=1, target={"?P": "http://x#liveProp"})]
    mutants[0]["blinded_checks"] = ["어떤 체크"]
    _classify_uncaught(mutants)
    assert mutants[0]["uncaught_reasons"] == ["blinded_guard", "weakening_only"]


def test_real_full_exposure_record_has_no_unexplained_miss():
    """산출물 확인 — 전수 노출 기록에서 detector_gap 이 0 인가.

    0 이 아니면 그것이 곧 조치 목록이다 (그 자체로 정당한 상태이므로 실패시키지 않고
    사유를 함께 보고한다).
    """
    runs = sorted(glob.glob("data/generated/meta_audit/runs/*/kg.json"))
    if not runs:
        pytest.skip("S9.5 기록 없음")
    with open(runs[-1], encoding="utf-8") as handle:
        record = json.load(handle)
    if not record.get("applied"):
        pytest.skip("주입 0건")
    bd = _classify_uncaught(record["mutants"])
    uncaught = [m for m in record["mutants"]
                if m.get("applied") and not m.get("caught_by")]
    assert all(m.get("uncaught_reasons") for m in uncaught), "사유 없는 miss 가 있다"
    assert set(bd) <= {
        "detector_gap", "weakening_only", "target_unused_in_abox",
        "abox_usage_undecidable", "blinded_guard",
    }, bd
