"""체크포인트가 **단계별** 입력 지문을 갖는지 회귀 가드.

배경 (2026-08-08 실측): ``save_step`` 이 ``state["input_state"]`` 하나만 갱신했다.
``can_skip_step`` 은 그 전역 스냅샷과 현재 입력을 비교하므로, 입력이 바뀌어
무효화된 앞 단계가 **나중 단계의 save_step 만으로 다시 "건너뛰기 가능"** 이 됐다.

실패 모습이 조용하다. CLAUDE.md 는 파이프라인 시작 시 ``check_pipeline_state`` 를
먼저 호출해 ``skippable`` 을 보고 건너뛸 단계를 정하라고 규정하므로, 잘못된
skippable=True 는 곧 **재생성 생략** 이다: CSV 에 컬럼을 추가했는데 S2/S7 을
건너뛰어 T-Box·A-Box 가 옛 스키마로 남는다.
"""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """격리된 DATA_DIR 에 CSV 한 개를 둔 pipeline_state 모듈을 준다."""
    import tools.pipeline_state as ps

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir()
    csv_path = rawdata / "T1.csv"
    csv_path.write_text("A\n1\n", encoding="utf-8")

    monkeypatch.setattr(ps, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ps, "SOURCE_TACIT_DIR", str(tmp_path / "tacit"))
    monkeypatch.setattr(ps, "_STATE_PATH", str(tmp_path / "pipeline_state.json"))
    return ps, csv_path


def _touch_forward(path) -> None:
    """mtime 을 확실히 전진시킨다 (같은 초 안의 수정도 감지되게)."""
    future = time.time() + 5
    os.utime(path, (future, future))


def test_later_save_does_not_resurrect_invalidated_step(isolated_state):
    """THE REGRESSION: 나중 단계 save 가 앞 단계의 무효화를 되돌리지 않는다."""
    ps, csv_path = isolated_state

    ps.save_step("S2_TBOX", {"classes": 10})
    assert ps.can_skip_step("S2_TBOX", ps._STEP_DEPS["S2_TBOX"]) is True

    csv_path.write_text("A\n1\n2\n", encoding="utf-8")
    _touch_forward(csv_path)
    assert ps.can_skip_step("S2_TBOX", ps._STEP_DEPS["S2_TBOX"]) is False

    # 이 단계는 CSV 를 읽지 않으므로 저장 자체는 정상. 문제는 S2 에 미치는 영향.
    ps.save_step("S7_ABOX", {"triples": 100})

    assert ps.can_skip_step("S2_TBOX", ps._STEP_DEPS["S2_TBOX"]) is False, (
        "앞 단계가 되살아났다 — CSV 가 바뀐 뒤에도 T-Box 재생성이 생략된다"
    )


def test_linear_progress_also_preserves_invalidation(isolated_state):
    """CSV 수정 후 선형으로 계속 진행해도 무효화가 유지된다.

    앞 테스트의 순서가 인위적이라는 반론을 막는다 — S1 수정 루프 뒤 S3~S6 을
    정상 순서로 진행하는 흔한 경로에서도 성립해야 한다.
    """
    ps, csv_path = isolated_state

    for step in ("S0_CQ", "S1_DATA", "S2_TBOX"):
        ps.save_step(step, {})
    csv_path.write_text("A,B\n1,x\n", encoding="utf-8")
    _touch_forward(csv_path)

    for step in ("S3_IMPROVE", "S4_VALIDATE", "S6_VIS", "S6_5_DICT_V1"):
        ps.save_step(step, {})

    for step in ("S0_CQ", "S1_DATA", "S2_TBOX"):
        assert ps.can_skip_step(step, ps._STEP_DEPS[step]) is False, (
            f"{step} 가 CSV 변경 후에도 건너뛰기 가능으로 보고됐다"
        )


def test_per_step_snapshot_is_persisted(isolated_state):
    """각 체크포인트가 자신의 input_state 를 들고 있어야 한다 (fix 의 메커니즘)."""
    import json

    ps, _csv = isolated_state
    ps.save_step("S2_TBOX", {})
    with open(ps._STATE_PATH, encoding="utf-8") as handle:
        saved = json.load(handle)
    entry = saved["completed_steps"]["S2_TBOX"]
    assert "input_state" in entry and entry["input_state"].get("csv_mtime"), (
        "단계별 지문이 저장되지 않으면 전역 스냅샷 덮어쓰기 문제가 재발한다"
    )


def test_legacy_state_without_per_step_snapshot_still_works(isolated_state):
    """구 상태 파일 (단계별 지문 없음) 은 전역 스냅샷으로 폴백한다."""
    import json

    ps, _csv = isolated_state
    ps.save_step("S2_TBOX", {})
    with open(ps._STATE_PATH, encoding="utf-8") as handle:
        state = json.load(handle)
    del state["completed_steps"]["S2_TBOX"]["input_state"]        # 구 포맷 재현
    with open(ps._STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle)

    assert ps.can_skip_step("S2_TBOX", ps._STEP_DEPS["S2_TBOX"]) is True
