"""S2 가 ``quality_history.json`` 에 항목을 남긴다 (예전엔 0건이었다).

2026-08-22 감사 실측: 파이프라인 한 바퀴 후 ``quality_history.json`` 7개 항목의
step 분포는 ``S3_IMPROVE / S4_VALIDATE / S4_5_MUTATION / S9_POST_MEASURE /
S12_QUERY_TEST`` + validate_kg 2건이었다 — **S2 는 0건**. 48분(실측 2883.7초)이
걸린 단계가 추세 추적에서 통째로 빠져 있었고, ``get_pipeline_quality_history`` 의
회귀 탐지도 S2 에는 발화할 수 없었다.

원인: ``_compute_round_quality_metrics`` 가 라운드마다 FAIR/Färber 를 계산해
``round_log["quality_metrics"]`` 에 넣지만, 그 값이 ``save_step`` /
``_append_quality_history`` 로 넘어가는 경로가 없었다.

## 이 파일이 고정하는 계약

- S2 종료 시 ``S2_TBOX`` 항목이 기록된다 (배선).
- **최종 라운드 값** 을 쓴다 — 평균은 나쁜 초기 라운드로 최종 산출물 점수를
  희석해 실제 품질을 왜곡한다 (지표 매수의 반대 방향이지만 여전히 부정확).
- 없는 지표를 **0 으로 채우지 않는다** (측정 실패를 "점수 0" 으로 위장 금지).
- **fail-open**: 기록 실패가 S2 산출물 반환을 막지 않는다.
- **테스트 격리**: 배포 ``quality_history.json`` 에 쓰지 않는다.
"""
from __future__ import annotations

import json
import os

import pytest

import tools.multi_agent_tbox as mt
import tools.pipeline_state as ps

_STATS = {"classes": 53, "object_properties": 88,
          "data_properties": 256, "triples": 3069}


def _history(tmp_path) -> list[dict]:
    path = os.path.join(os.path.dirname(ps._STATE_PATH), "quality_history.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """``_STATE_PATH`` 를 tmp 로 — quality_history 가 여기서 파생된다."""
    monkeypatch.setattr(ps, "_STATE_PATH", str(tmp_path / "pipeline_state.json"))
    return tmp_path


def test_s2_appears_in_quality_history(isolated_state):
    state = {
        "debate_log": [
            {"round": 2, "quality_metrics": {"fair": {"overall": 70.0},
                                             "farber": {"overall": 50.0}}},
            {"round": 3, "quality_metrics": {"fair": {"overall": 72.9},
                                             "farber": {"overall": 57.2}}},
        ],
        "consensus_reached": False,
        "veto_lock_triggered": True,
    }
    mt._record_s2_quality_history(state, _STATS, 2883.7, {"persistent_issues": 14})

    history = _history(isolated_state)
    steps = [e.get("step") for e in history]
    assert "S2_TBOX" in steps, f"S2 항목이 없다: {steps}"
    entry = next(e for e in history if e["step"] == "S2_TBOX")
    assert entry["duration_seconds"] == 2883.7
    m = entry["metrics"]
    assert m["debate_rounds"] == 2
    assert m["consensus_reached"] is False
    assert m["veto_lock_triggered"] is True
    assert m["persistent_issues"] == 14
    assert m["object_properties"] == 88


def test_uses_final_round_not_average(isolated_state):
    """평균(71.45)이 아니라 최종 라운드(72.9)여야 한다."""
    state = {
        "debate_log": [
            {"round": 2, "quality_metrics": {"fair": {"overall": 70.0},
                                             "farber": {"overall": 50.0}}},
            {"round": 3, "quality_metrics": {"fair": {"overall": 72.9},
                                             "farber": {"overall": 57.2}}},
        ],
    }
    mt._record_s2_quality_history(state, _STATS, 10.0, {})
    m = next(e for e in _history(isolated_state) if e["step"] == "S2_TBOX")["metrics"]
    assert m["fair_overall"] == 72.9
    assert m["farber_overall"] == 57.2


def test_missing_metrics_are_omitted_not_zeroed(isolated_state):
    """측정 실패를 "점수 0" 으로 위장하면 회귀 탐지가 거짓 경보를 낸다."""
    state = {"debate_log": [{"round": 2, "quality_metrics": {"error": "tempfile 실패"}}]}
    mt._record_s2_quality_history(state, _STATS, 10.0, {})
    m = next(e for e in _history(isolated_state) if e["step"] == "S2_TBOX")["metrics"]
    assert "fair_overall" not in m, m
    assert "farber_overall" not in m, m
    # 구조 통계는 여전히 있어야 한다 (그건 측정된 값이다).
    assert m["classes"] == 53


def test_records_even_when_no_round_had_metrics(isolated_state):
    """라운드가 0개여도 구조 통계는 기록한다 — S2 자체가 사라지면 안 된다."""
    mt._record_s2_quality_history({"debate_log": []}, _STATS, 5.0, {})
    assert "S2_TBOX" in [e.get("step") for e in _history(isolated_state)]


def test_fail_open_does_not_raise(isolated_state):
    """기록 실패가 S2 산출물 반환을 막지 않는다 (T-Box 저장이 우선)."""
    mt._record_s2_quality_history(None, {}, 0.0, {})  # state=None → 내부 예외
    # 예외가 전파되지 않는 것 자체가 계약이다.


def test_wired_into_finalize():
    """단위 함수만 통과하고 배선이 없으면 산출물이 안 바뀐다 (실측 2회 발생)."""
    import inspect
    src = inspect.getsource(mt._finalize_and_save)
    assert "_record_s2_quality_history(" in src, \
        "_finalize_and_save 가 호출하지 않으면 S2 항목은 영구히 0건이다"
