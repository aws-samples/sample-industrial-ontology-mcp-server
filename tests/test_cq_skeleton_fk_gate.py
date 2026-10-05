"""CQ 스켈레톤 주입이 CSV FK 근거를 요구하는지 고정.

## 왜

`_generate_op_skeletons_from_cq_gaps` 는 CQ 경로 갭을 보면 **무조건** OP 를 만들었다.
A-Box 생성기는 FK 컬럼에서만 관계 트리플을 만들므로 FK 없는 쌍에 OP 를 선언하면
영구히 값 0건이고, 그 관계로 질의하면 0건이 오류 없이 "정답처럼" 반환된다.

실측 (2026-08-13 S2 재실행): 이 경로가 주입한 OP **28개 중 A-Box 값을 가진 것은
1개** 였다. 더 나쁜 것은 **SME 프롬프트 수정을 우회했다는 점** 이다 — SME 는 같은
라운드에 "CSV FK 부재 — 데이터 수집 또는 tacit 지식 필요" 라고 정확히 보고했는데
이 코드가 그 판단을 무시해, 새 프롬프트로 얻은 R1 초안의 OP 87개가 R5 에서
135개로 되돌아갔다 (스켈레톤 54개 주입, `inverseOf` 51 → 103).

FK 가 없는 갭은 **데이터 갭** 이다. 빈 관계로 가리면 CQ 커버리지 지표만 오르고
질의는 계속 0건이므로, 주입하지 않고 로그로 드러낸다.
"""
from __future__ import annotations

import pytest

from tools.multi_agent_tbox import (
    _csv_fk_pairs,
    _generate_op_skeletons_from_cq_gaps,
)


def _cqs(*pairs: tuple[str, str]) -> list[dict]:
    """``domains`` 만 채운 최소 CQ 목록."""
    return [
        {"id": f"CQ{i:02d}", "question": "q", "domains": list(p)}
        for i, p in enumerate(pairs, 1)
    ]


def _gap(cq_id: str, left: str, right: str) -> dict:
    """``_cq_runtime_check`` 가 내는 형식 (소문자·밑줄 제거)."""
    norm = lambda x: x.replace("_", "").lower()   # noqa: E731
    return {"unanswerable": [(cq_id, f"{norm(left)} → {norm(right)} 경로 없음 (2-hop OP 부재)")]}


def _ops(skeletons: list[dict]) -> list[str]:
    return [a["property"] for a in skeletons if a["action"] == "add_object_property"]


# ── THE REGRESSION: FK 없는 갭에 OP 를 만들지 않는다 ──────────────────


def test_gap_without_csv_fk_injects_nothing(monkeypatch):
    """핵심 회귀: FK 근거가 없으면 주입하지 않는다."""
    monkeypatch.setattr(
        "tools.multi_agent_tbox._csv_fk_pairs", lambda: {("alpha", "beta")},
    )
    skeletons = _generate_op_skeletons_from_cq_gaps(
        _gap("CQ01", "Gamma", "Delta"), _cqs(("Gamma", "Delta")),
    )
    assert _ops(skeletons) == [], f"FK 없는 쌍에 OP 를 만들었다: {_ops(skeletons)}"


def test_ungrounded_gap_is_logged_not_silently_dropped(monkeypatch, caplog):
    """조용히 버리지 않는다 — 데이터 갭임을 로그로 드러낸다.

    로그가 없으면 "CQ 커버리지가 안 오르는데 이유를 모르겠다" 가 된다.
    """
    monkeypatch.setattr(
        "tools.multi_agent_tbox._csv_fk_pairs", lambda: {("alpha", "beta")},
    )
    with caplog.at_level("INFO", logger="tools.multi_agent_tbox"):
        _generate_op_skeletons_from_cq_gaps(
            _gap("CQ01", "Gamma", "Delta"), _cqs(("Gamma", "Delta")),
        )
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "CSV FK 근거가 없어" in messages, f"데이터 갭 로그가 없다: {messages}"
    assert "CQ01" in messages


# ── PRESERVATION: FK 있는 쌍은 계속 주입한다 ─────────────────────────


def test_gap_with_csv_fk_still_injects_both_legs(monkeypatch):
    """PRESERVATION: FK 가 뒷받침하면 정방향+역방향을 만든다.

    이 스텝의 목적(CQ 답변 가능하게 만들기)을 잃으면 안 된다.
    """
    monkeypatch.setattr(
        "tools.multi_agent_tbox._csv_fk_pairs", lambda: {("gamma", "delta")},
    )
    skeletons = _generate_op_skeletons_from_cq_gaps(
        _gap("CQ01", "Gamma", "Delta"), _cqs(("Gamma", "Delta")),
    )
    ops = _ops(skeletons)
    assert len(ops) == 2, f"양쪽 다리를 만들지 않았다: {ops}"
    assert any(a["action"] == "add_inverse_property" for a in skeletons), (
        "inverseOf 선언이 빠졌다 — 한쪽만 만들면 dangling 이 된다"
    )


def test_fk_direction_does_not_matter(monkeypatch):
    """FK 는 한쪽 테이블에만 컬럼으로 존재하므로 방향을 따지면 정당한 쌍을 놓친다."""
    monkeypatch.setattr(
        "tools.multi_agent_tbox._csv_fk_pairs", lambda: {("delta", "gamma")},
    )
    skeletons = _generate_op_skeletons_from_cq_gaps(
        _gap("CQ01", "Gamma", "Delta"), _cqs(("Gamma", "Delta")),
    )
    assert len(_ops(skeletons)) == 2, (
        "역방향 FK 를 근거로 인정하지 않아 정당한 쌍을 막았다"
    )


# ── 판정 불가는 게이트를 적용하지 않는다 ─────────────────────────────


def test_unknown_fk_pairs_does_not_block_injection(monkeypatch):
    """``None``(CSV 읽기 불가)이면 게이트를 적용하지 않는다.

    0건과 판정 불가를 혼동하면 정당한 주입을 **전부** 막는다 — 이 리포에서
    반복된 오독 유형이다.
    """
    monkeypatch.setattr("tools.multi_agent_tbox._csv_fk_pairs", lambda: None)
    skeletons = _generate_op_skeletons_from_cq_gaps(
        _gap("CQ01", "Gamma", "Delta"), _cqs(("Gamma", "Delta")),
    )
    assert len(_ops(skeletons)) == 2, (
        "판정 불가를 '근거 없음' 으로 읽어 주입을 막았다"
    )


def test_empty_fk_set_blocks_everything(monkeypatch):
    """빈 집합(FK 가 정말 하나도 없음)은 전부 차단한다 — ``None`` 과 다르다."""
    monkeypatch.setattr("tools.multi_agent_tbox._csv_fk_pairs", lambda: set())
    skeletons = _generate_op_skeletons_from_cq_gaps(
        _gap("CQ01", "Gamma", "Delta"), _cqs(("Gamma", "Delta")),
    )
    assert _ops(skeletons) == []


def test_no_gaps_is_a_clean_no_op():
    """갭이 없으면 조용히 빈 리스트."""
    assert _generate_op_skeletons_from_cq_gaps({"unanswerable": []}, _cqs()) == []


# ── 실측 고정 ─────────────────────────────────────────────────────────


def test_real_csv_fk_pairs_are_discovered():
    """실제 CSV 에서 FK 쌍을 찾는다 — ``None`` 이나 빈 집합이면 게이트가 무의미하다."""
    pairs = _csv_fk_pairs()
    if pairs is None:
        pytest.skip("CSV 디렉터리 없음")
    assert len(pairs) > 0, "FK 쌍을 하나도 못 찾았다 (fk_patterns.json 확인)"
    # 정규화 계약: 밑줄 제거 + 소문자
    for a, b in pairs:
        assert a == a.lower() and "_" not in a, f"정규화 안 된 키: {a}"
        assert b == b.lower() and "_" not in b, f"정규화 안 된 키: {b}"


def test_this_runs_actual_gaps_are_all_ungrounded():
    """실측 고정: 2026-08-13 S2 실행이 보고한 갭 6건은 전부 FK 근거가 없다.

    그 6건에 주입된 스켈레톤 54개가 R1→R5 의 OP 증가(87→135)를 만들었다.
    이 테스트가 깨지면 CSV 나 fk_patterns 가 바뀐 것이므로 재측정이 필요하다.
    """
    import json
    import os

    cq_path = os.path.join("data", "source", "query_tests",
                           "competency_questions.json")
    if not os.path.exists(cq_path):
        pytest.skip("CQ 파일 없음")
    with open(cq_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    items = raw if isinstance(raw, list) else (
        raw.get("competency_questions") or raw.get("questions") or []
    )
    gaps = {"unanswerable": [
        ("CQ02", "processblastfurnace → alarmevents 경로 없음 (2-hop OP 부재)"),
        ("CQ06", "ghgemission → energysourcemaster 경로 없음 (2-hop OP 부재)"),
        ("CQ08", "inventorystatus → suppliermaster 경로 없음 (2-hop OP 부재)"),
        ("CQ09", "processsteelmakingfurnace → realtimedata 경로 없음 (2-hop OP 부재)"),
        ("CQ10", "noisevibrationmonitoring → gasenergy 경로 없음 (2-hop OP 부재)"),
        ("CQ11", "productionplan → inventorytransaction 경로 없음 (2-hop OP 부재)"),
    ]}
    skeletons = _generate_op_skeletons_from_cq_gaps(gaps, items)
    assert _ops(skeletons) == [], (
        f"FK 근거 없는 갭에 OP 를 주입했다: {_ops(skeletons)}"
    )


# ── _csv_fk_pairs 의 판정 불가 계약 ──────────────────────────────────


def test_missing_csv_dir_returns_none_not_empty(monkeypatch, tmp_path):
    """CSV 디렉터리가 없으면 ``None``(판정 불가) — 빈 집합이 아니다.

    빈 집합을 돌려주면 호출부가 "FK 가 하나도 없다" 로 읽어 **모든** 주입을
    막는다. 판정 불가와 0건은 다른 상태다.
    """
    import config

    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(tmp_path / "nope"))
    assert _csv_fk_pairs() is None


def test_empty_csv_dir_returns_none_not_empty(monkeypatch, tmp_path):
    """디렉터리는 있지만 CSV 가 0개면 역시 판정 불가다."""
    import config

    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(tmp_path))
    assert _csv_fk_pairs() is None


def test_csv_with_no_fk_columns_returns_empty_set(monkeypatch, tmp_path):
    """CSV 는 있고 FK 컬럼만 없으면 **빈 집합** 이다 — 이때는 차단이 정답이다."""
    import config

    (tmp_path / "Plain_Table.csv").write_text("Name,Note\na,b\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(tmp_path))
    result = _csv_fk_pairs()
    assert result == set(), f"FK 없는 CSV 인데 {result} 를 돌려줬다"


def test_fk_pairs_are_normalized(monkeypatch, tmp_path):
    """밑줄 제거 + 소문자 정규화 — CQ domains 표기와 맞춰야 비교가 성립한다.

    실측: CQ 는 ``Equipment_Master``, CSV 클래스는 ``EquipmentMaster`` 로 표기가
    달라 정규화 없이는 교집합이 0 이 된다.
    """
    import config

    (tmp_path / "Order_Head.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    (tmp_path / "Item_Master.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(tmp_path))
    pairs = _csv_fk_pairs()
    assert pairs is not None
    assert ("orderhead", "itemmaster") in pairs, f"정규화 실패: {pairs}"
