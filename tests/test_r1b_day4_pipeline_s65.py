"""기능 단계 — S6_5_DICT_V1 파이프라인 상태머신 등록 검증.

S6.5 가 _STEP_DEPS 에 존재하고, S7_ABOX 가 dict_v1_mtime 에 의존하며,
_current_input_state 가 dict_v1_mtime 을 제공하는지 확인.
"""
from __future__ import annotations

import json
from unittest.mock import patch


def test_s6_5_dict_v1_registered():
    """_STEP_DEPS 에 S6_5_DICT_V1 존재."""
    from tools.pipeline_state import _STEP_DEPS
    assert "S6_5_DICT_V1" in _STEP_DEPS, "S6_5_DICT_V1 이 상태머신에 없음"


def test_s6_5_dict_v1_depends_on_tbox_only():
    """S6.5 는 T-Box 만 입력 — A-Box 는 의존 대상 아님 (v1 은 A-Box 없이 생성)."""
    from tools.pipeline_state import _STEP_DEPS
    deps = _STEP_DEPS["S6_5_DICT_V1"]
    assert "tbox_mtime" in deps
    assert "abox_mtime" not in deps, "v1 은 A-Box 의존 없어야 함 (A-Box 전 단계)"
    assert "inferred_mtime" not in deps


def test_s7_abox_depends_on_the_contract_content_not_the_file_mtime():
    """S7_ABOX 는 contract **내용의 지문** 을 의존한다 (2026-08-30 축 교체).

    원래 이 테스트는 ``dict_v1_mtime`` 을 요구했다. 의도("contract 변경 시 A-Box
    재생성")는 정당하지만 **대리 지표가 틀렸다**: S10 이 **같은 파일**을 v2 로
    덮어쓰므로 mtime 은 매 실행 갱신되고, A-Box 를 한 트리플도 건드리지 않았는데
    S7 이 영구히 "재실행 필요" 가 됐다.

    격리 실험 (2026-08-30)::

        S7 저장 직후                      can_skip=True
        S10 이 같은 경로에 v2 를 쓴 뒤       can_skip=False   ← 자기무효화

    ``dict_contract_fingerprint`` 는 S7 이 실제로 읽는 것
    (``classes.*.datatype_properties`` 의 이름 집합) 만 해싱하므로 v1→v2 덮어쓰기에
    불변이고 DP 개명에만 반응한다 — 아래 두 테스트가 그 양방향을 고정한다.
    """
    from tools.pipeline_state import _STEP_DEPS
    deps = _STEP_DEPS["S7_ABOX"]
    assert "dict_contract_fingerprint" in deps, (
        "S7 이 contract 지문 의존 없음 — contract 변경 감지 불가"
    )
    assert "dict_v1_mtime" not in deps, (
        "S7 이 다시 파일 mtime 을 의존한다 — S10 이 같은 파일을 덮어쓰므로 "
        "A-Box 가 그대로여도 영구 무효화된다"
    )


def _dict_state(monkeypatch, tmp_path, payload: dict):
    """딕셔너리 파일을 주어진 내용으로 쓰고 현재 input_state 를 반환."""
    from tools import pipeline_state as ps
    path = tmp_path / "semantic_dictionary.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ps, "SEMANTIC_DICT_PATH", str(path))
    return ps._current_input_state()


_V1 = {"classes": {"Equip": {"datatype_properties": {"equipId": {}, "equipName": {}}}}}
#: v2 = 같은 DP 이름 + A-Box 실측 통계 (S10, include_stats=True)
_V2 = {
    "classes": {"Equip": {"datatype_properties": {
        "equipId": {"value_stats": {"min": 1}}, "equipName": {"distinct_values": 5},
    }}},
    "metadata": {"stats_included": True},
}
#: DP 이름이 바뀐 경우 (S2 재생성) — A-Box 가 쓸 어휘가 달라진다
_RENAMED = {
    "classes": {"Equip": {"datatype_properties": {"equipmentId": {}, "equipName": {}}}},
}


def test_contract_fingerprint_survives_the_v1_to_v2_overwrite(tmp_path, monkeypatch):
    """THE REGRESSION: v1 → v2 덮어쓰기로 지문이 바뀌지 않는다.

    v2 는 같은 DP 이름에 통계를 덧붙일 뿐이므로 A-Box 가 쓸 어휘는 동일하다.
    """
    before = _dict_state(monkeypatch, tmp_path, _V1)["dict_contract_fingerprint"]
    after = _dict_state(monkeypatch, tmp_path, _V2)["dict_contract_fingerprint"]
    assert before, "지문이 비어 있다 — 계산 실패"
    assert before == after, (
        "통계만 추가됐는데 지문이 바뀌었다 — S7 이 불필요하게 재실행된다"
    )


def test_contract_fingerprint_changes_when_dp_names_change(tmp_path, monkeypatch):
    """PRESERVATION: DP 이름이 바뀌면 지문이 바뀐다 (재생성이 **필요한** 경우).

    이 축이 없으면 지문은 항상 같은 값이 되어 게이트가 죽는다 — 무발동 게이트를
    "안정" 으로 오독하는 함정.
    """
    before = _dict_state(monkeypatch, tmp_path, _V1)["dict_contract_fingerprint"]
    after = _dict_state(monkeypatch, tmp_path, _RENAMED)["dict_contract_fingerprint"]
    assert before != after, (
        "class-specific DP 이름이 바뀌었는데 지문이 같다 — A-Box 가 없는 DP 를 "
        "쓰게 되고 undeclared_dp 로 드러난다"
    )


def test_contract_fingerprint_empty_when_dictionary_missing(tmp_path, monkeypatch):
    """딕셔너리가 없으면 빈 문자열 — "지문 0" 같은 값과 구분한다."""
    from tools import pipeline_state as ps
    monkeypatch.setattr(ps, "SEMANTIC_DICT_PATH", str(tmp_path / "missing.json"))
    assert ps._current_input_state()["dict_contract_fingerprint"] == ""


def test_s7_is_skippable_after_s10_overwrites_the_dictionary(tmp_path, monkeypatch):
    """배선 계약: 전체 경로에서 S7 자기무효화가 사라졌는가.

    ``_STEP_DEPS`` 만 고쳐도 ``_current_input_state`` 가 지문을 안 실으면 조용히
    통과한다 — ``save_step`` → ``can_skip_step`` 왕복으로 확인한다.
    """
    import time

    from tools import pipeline_state as ps

    dict_path = tmp_path / "semantic_dictionary.json"
    dict_path.write_text(json.dumps(_V1), encoding="utf-8")
    for name in ("t_box.ttl", "a_box.ttl", "all_inferred.ttl"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    original = ps._current_input_state

    def _stable_inputs():
        state = dict(original())
        # CSV/tacit 축은 이 테스트의 대상이 아니므로 고정한다.
        state["csv_mtime"] = 1.0
        state["tacit_mtime"] = 1.0
        state["csv_per_table"] = {}
        return state

    monkeypatch.setattr(ps, "SEMANTIC_DICT_PATH", str(dict_path))
    monkeypatch.setattr(ps, "TBOX_PATH", str(tmp_path / "t_box.ttl"))
    monkeypatch.setattr(ps, "ABOX_PATH", str(tmp_path / "a_box.ttl"))
    monkeypatch.setattr(ps, "INFERRED_PATH", str(tmp_path / "all_inferred.ttl"))
    monkeypatch.setattr(ps, "_STATE_PATH", str(tmp_path / "pipeline_state.json"))
    monkeypatch.setattr(ps, "_per_table_fingerprint", lambda: {})
    monkeypatch.setattr(ps, "_current_input_state", _stable_inputs)

    ps.save_step("S7_ABOX", {"individuals": 70701})
    assert ps.can_skip_step("S7_ABOX") is True, "저장 직후인데 스킵 불가"

    time.sleep(0.02)
    dict_path.write_text(json.dumps(_V2), encoding="utf-8")   # S10 이 덮어쓴다
    assert ps.can_skip_step("S7_ABOX") is True, (
        "S10 이 딕셔너리를 덮어쓰자 S7 이 무효화됐다 — A-Box 는 그대로다"
    )

    time.sleep(0.02)
    dict_path.write_text(json.dumps(_RENAMED), encoding="utf-8")
    assert ps.can_skip_step("S7_ABOX") is False, (
        "DP 이름이 바뀌었는데 S7 을 스킵 가능으로 봤다 — A-Box 가 낡은 어휘를 유지한다"
    )


def test_s7_abox_still_depends_on_csv_and_tbox():
    """기존 S7 의존성 (csv_mtime, tbox_mtime) 유지."""
    from tools.pipeline_state import _STEP_DEPS
    deps = _STEP_DEPS["S7_ABOX"]
    assert "csv_mtime" in deps
    assert "tbox_mtime" in deps


def test_current_input_state_includes_dict_v1_mtime(tmp_path, monkeypatch):
    """_current_input_state 가 dict_v1_mtime 필드 제공."""
    # 파일 없음 시 0 기대
    from tools import pipeline_state as ps
    monkeypatch.setattr(ps, "SEMANTIC_DICT_PATH", str(tmp_path / "missing.json"))
    state = ps._current_input_state()
    assert "dict_v1_mtime" in state
    assert state["dict_v1_mtime"] == 0


def test_current_input_state_tracks_dict_v1_mtime(tmp_path, monkeypatch):
    """실제 파일 존재 시 mtime 수집."""
    from tools import pipeline_state as ps
    dict_file = tmp_path / "semantic_dictionary.json"
    dict_file.write_text('{"test": true}', encoding="utf-8")
    monkeypatch.setattr(ps, "SEMANTIC_DICT_PATH", str(dict_file))
    state = ps._current_input_state()
    assert state["dict_v1_mtime"] > 0


def test_s6_5_placement_before_s7():
    """파이프라인 순서: S6_5_DICT_V1 이 S7_ABOX 보다 먼저 등장."""
    from tools.pipeline_state import _STEP_DEPS
    steps = list(_STEP_DEPS.keys())
    s65_idx = steps.index("S6_5_DICT_V1")
    s7_idx = steps.index("S7_ABOX")
    assert s65_idx < s7_idx, f"S6.5 (idx {s65_idx}) 가 S7 (idx {s7_idx}) 보다 뒤에 있음"


def test_s6_5_placement_after_s6_vis():
    """S6.5 는 S6_VIS 바로 뒤에 위치."""
    from tools.pipeline_state import _STEP_DEPS
    steps = list(_STEP_DEPS.keys())
    s6_idx = steps.index("S6_VIS")
    s65_idx = steps.index("S6_5_DICT_V1")
    # 바로 다음이어야 함 (S6_VIS 와 S7_ABOX 사이에 S6.5 만)
    assert s65_idx == s6_idx + 1


def test_check_pipeline_state_reports_s6_5(tmp_path, monkeypatch):
    """check_pipeline_state MCP 가 S6.5 step 을 보고."""
    from tools.pipeline_state import check_pipeline_state

    with patch("tools.pipeline_state._current_input_state",
               return_value={"csv_mtime": 0, "tbox_mtime": 0, "abox_mtime": 0,
                             "tacit_mtime": 0, "inferred_mtime": 0}):
        # state dir 격리
        monkeypatch.setattr("tools.pipeline_state._STATE_PATH",
                            str(tmp_path / "state.json"))
        result = json.loads(check_pipeline_state())

    step_names = [s["step"] for s in result["steps"]]
    assert "S6_5_DICT_V1" in step_names
