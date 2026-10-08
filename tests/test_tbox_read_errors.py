"""T-Box 읽기 실패와 구문 오류를 MCP 오류 응답으로 구분한다."""
from __future__ import annotations

import json

import pytest

from tools import local_artifacts, tbox_metrics


@pytest.mark.parametrize("tool", [tbox_metrics.analyze_tbox, tbox_metrics.measure_tbox_metrics])
def test_missing_tbox_returns_read_error(tool, tmp_path, monkeypatch):
    missing = tmp_path / "missing.ttl"
    monkeypatch.setattr(local_artifacts, "TBOX_PATH", str(missing))

    result = json.loads(tool())

    assert result["success"] is False
    assert str(missing) in result["error"]
    assert result["hint"]
    assert "Bad syntax" not in result["error"]
    assert "metrics" not in result
    assert "classes" not in result


@pytest.mark.parametrize("tool", [tbox_metrics.analyze_tbox, tbox_metrics.measure_tbox_metrics])
def test_tbox_reader_error_and_hint_are_preserved(tool, monkeypatch):
    failure = {"success": False, "error": "fixture read denied", "hint": "check fixture access"}
    monkeypatch.setattr(local_artifacts, "read_tbox", lambda: json.dumps(failure))

    assert json.loads(tool()) == failure


@pytest.mark.parametrize("tool", [tbox_metrics.analyze_tbox, tbox_metrics.measure_tbox_metrics])
def test_malformed_tbox_returns_error_without_measurements(tool, sample_tbox_file, invalid_ttl):
    sample_tbox_file.write_text(invalid_ttl, encoding="utf-8")

    result = json.loads(tool())

    assert result["success"] is False
    assert result["error"]
    assert str(sample_tbox_file) in result["error"]
    assert "hint" not in result
    assert "metrics" not in result
    assert "classes" not in result
