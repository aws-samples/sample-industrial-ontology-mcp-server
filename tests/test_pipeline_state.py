"""Tests for tools/pipeline_state.py — state save/load/reset, skip detection."""

import json
import os
from unittest.mock import patch

import pytest

from tools.pipeline_state import (
    can_skip_step,
    check_pipeline_state,
    get_pipeline_quality_history,
    load_state,
    reset_pipeline_state,
    save_step,
)


@pytest.fixture
def state_dir(tmp_path):
    """Provide a temporary state directory and patch _STATE_PATH."""
    state_path = str(tmp_path / "pipeline_state.json")
    with patch("tools.pipeline_state._STATE_PATH", state_path), \
         patch("tools.pipeline_state.GENERATED_DIR", str(tmp_path)):
        yield tmp_path, state_path


class TestLoadState:
    """load_state: load pipeline checkpoint."""

    def test_missing_file_returns_empty(self, state_dir):
        state = load_state()
        assert state == {"completed_steps": {}, "input_state": {}}

    def test_valid_state_loaded(self, state_dir):
        _, state_path = state_dir
        data = {"completed_steps": {"S0_CQ": {"completed_at": "2026-01-01"}}, "input_state": {}}
        with open(state_path, "w") as f:
            json.dump(data, f)
        state = load_state()
        assert "S0_CQ" in state["completed_steps"]

    def test_corrupted_json_returns_empty(self, state_dir):
        _, state_path = state_dir
        with open(state_path, "w") as f:
            f.write("{corrupted json!!!}")
        state = load_state()
        assert state == {"completed_steps": {}, "input_state": {}}


class TestSaveStep:
    """save_step: record step completion."""

    @patch("tools.pipeline_state._current_input_state", return_value={"csv_mtime": 100.0})
    def test_save_creates_file(self, mock_input, state_dir):
        _, state_path = state_dir
        save_step("S0_CQ", {"count": 10}, duration_seconds=3.5)
        assert os.path.exists(state_path)
        with open(state_path) as f:
            state = json.load(f)
        assert "S0_CQ" in state["completed_steps"]
        assert state["completed_steps"]["S0_CQ"]["duration_seconds"] == 3.5
        assert state["completed_steps"]["S0_CQ"]["result"] == {"count": 10}

    @patch("tools.pipeline_state._current_input_state", return_value={"csv_mtime": 100.0})
    def test_save_multiple_steps(self, mock_input, state_dir):
        _, state_path = state_dir
        save_step("S0_CQ", {"count": 10}, duration_seconds=1.0)
        save_step("S1_DATA", {"tables": 5}, duration_seconds=2.0)
        with open(state_path) as f:
            state = json.load(f)
        assert "S0_CQ" in state["completed_steps"]
        assert "S1_DATA" in state["completed_steps"]

    @patch("tools.pipeline_state._current_input_state", return_value={"csv_mtime": 100.0})
    def test_save_with_quality_metrics(self, mock_input, state_dir):
        tmp_path, _ = state_dir
        save_step("S4_VALIDATE", {"passed": True}, duration_seconds=5.0,
                  quality_metrics={"score": 95})
        history_path = str(tmp_path / "quality_history.json")
        assert os.path.exists(history_path)
        with open(history_path) as f:
            history = json.load(f)
        assert len(history) == 1
        assert history[0]["step"] == "S4_VALIDATE"
        assert history[0]["metrics"]["score"] == 95


class TestCanSkipStep:
    """can_skip_step: mtime-based skip detection."""

    @patch("tools.pipeline_state._current_input_state",
           return_value={"csv_mtime": 100.0, "tbox_mtime": 200.0, "tacit_mtime": 0})
    def test_completed_and_unchanged_can_skip(self, mock_input, state_dir):
        _, state_path = state_dir
        state = {
            "completed_steps": {"S0_CQ": {"completed_at": "2026-01-01"}},
            "input_state": {"csv_mtime": 100.0, "tbox_mtime": 200.0, "tacit_mtime": 0},
        }
        with open(state_path, "w") as f:
            json.dump(state, f)
        assert can_skip_step("S0_CQ", ["csv_mtime"]) is True

    @patch("tools.pipeline_state._current_input_state",
           return_value={"csv_mtime": 999.0, "tbox_mtime": 200.0, "tacit_mtime": 0})
    def test_input_changed_cannot_skip(self, mock_input, state_dir):
        _, state_path = state_dir
        state = {
            "completed_steps": {"S0_CQ": {"completed_at": "2026-01-01"}},
            "input_state": {"csv_mtime": 100.0},
        }
        with open(state_path, "w") as f:
            json.dump(state, f)
        assert can_skip_step("S0_CQ", ["csv_mtime"]) is False

    def test_never_completed_cannot_skip(self, state_dir):
        assert can_skip_step("S0_CQ", ["csv_mtime"]) is False


class TestCheckPipelineState:
    """check_pipeline_state: full pipeline status report."""

    @patch("tools.pipeline_state._current_input_state",
           return_value={"csv_mtime": 0, "tbox_mtime": 0, "abox_mtime": 0,
                         "tacit_mtime": 0, "inferred_mtime": 0})
    def test_empty_state(self, mock_input, state_dir):
        result = json.loads(check_pipeline_state())
        assert "steps" in result
        # S0-S13 + S4.5 + S6.5 (R1B) + S8.5 SWRL + S9_OWL_SANITY +
        # S9_POST_MEASURE + S9.5 + S12.5 + N1-N3
        assert len(result["steps"]) == 24
        assert all(s["completed"] is False for s in result["steps"])
        assert all(s["skippable"] is False for s in result["steps"])
        assert "hint" in result

    @patch("tools.pipeline_state._current_input_state",
           return_value={"csv_mtime": 100.0, "tbox_mtime": 200.0,
                         "abox_mtime": 0, "tacit_mtime": 0, "inferred_mtime": 0})
    def test_partial_completion(self, mock_input, state_dir):
        _, state_path = state_dir
        state = {
            "completed_steps": {
                "S0_CQ": {"completed_at": "2026-01-01", "duration_seconds": 3.0},
            },
            "input_state": {"csv_mtime": 100.0, "tbox_mtime": 200.0,
                            "abox_mtime": 0, "tacit_mtime": 0, "inferred_mtime": 0},
        }
        with open(state_path, "w") as f:
            json.dump(state, f)
        result = json.loads(check_pipeline_state())
        s0 = next(s for s in result["steps"] if s["step"] == "S0_CQ")
        assert s0["completed"] is True
        assert s0["skippable"] is True
        assert result["total_duration_seconds"] == 3.0


class TestResetPipelineState:
    """reset_pipeline_state: clear checkpoint file."""

    @patch("tools.pipeline_state._current_input_state", return_value={"csv_mtime": 100.0})
    def test_reset_removes_file(self, mock_input, state_dir):
        _, state_path = state_dir
        save_step("S0_CQ", {"count": 10})
        assert os.path.exists(state_path)
        result = json.loads(reset_pipeline_state())
        assert result["success"] is True
        assert not os.path.exists(state_path)

    def test_reset_on_missing_file(self, state_dir):
        result = json.loads(reset_pipeline_state())
        assert result["success"] is True


class TestGetQualityHistory:
    """get_pipeline_quality_history: quality trend retrieval."""

    def test_no_history(self, state_dir):
        result = json.loads(get_pipeline_quality_history())
        assert result["entries"] == []
        assert "이력 없음" in result["message"]

    @patch("tools.pipeline_state._current_input_state", return_value={"csv_mtime": 100.0})
    def test_history_with_entries(self, mock_input, state_dir):
        save_step("S4_VALIDATE", {"passed": True}, quality_metrics={"score": 90})
        save_step("S4_VALIDATE", {"passed": True}, quality_metrics={"score": 95})
        result = json.loads(get_pipeline_quality_history(last_n=10))
        assert result["total_entries"] == 2
        assert result["showing"] == 2

    @patch("tools.pipeline_state._current_input_state", return_value={"csv_mtime": 100.0})
    def test_history_last_n_limit(self, mock_input, state_dir):
        for i in range(5):
            save_step("S4_VALIDATE", {"passed": True},
                      quality_metrics={"score": 90 + i})
        result = json.loads(get_pipeline_quality_history(last_n=2))
        assert result["showing"] == 2
        assert result["total_entries"] == 5


class TestS12_5GoldenRegressionStep:
    """S12_5_GOLDEN_REGRESSION: new pipeline step registration (X1)."""

    def test_s12_5_golden_regression_registered(self):
        """Golden regression 단계가 등록돼 있고 올바른 의존성 가짐."""
        from tools.pipeline_state import _STEP_DEPS
        assert "S12_5_GOLDEN_REGRESSION" in _STEP_DEPS
        deps = _STEP_DEPS["S12_5_GOLDEN_REGRESSION"]
        assert "tbox_mtime" in deps
        assert "abox_mtime" in deps
        assert "inferred_mtime" in deps

    def test_s12_5_between_s12_and_s13(self):
        """Golden regression 이 S12 와 S13 사이에 위치."""
        from tools.pipeline_state import _ALL_STEPS
        i12 = _ALL_STEPS.index("S12_QUERY_TEST")
        i125 = _ALL_STEPS.index("S12_5_GOLDEN_REGRESSION")
        i13 = _ALL_STEPS.index("S13_REPORT")
        assert i12 < i125 < i13
