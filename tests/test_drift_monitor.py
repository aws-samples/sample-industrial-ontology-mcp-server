"""Tests for tools/drift_monitor.py — Q6."""
from __future__ import annotations

import csv
import json
from unittest.mock import patch


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_ngram_similarity():
    from tools.drift_monitor import _ngram_max_similarity
    # "alarm_high" vs "alarm_low": 공통 n-gram "ala", "lar", "arm", "rm_" 등
    sim_similar = _ngram_max_similarity("alarm_high", {"alarm_low"})
    sim_diff = _ngram_max_similarity("xyz_quantum", {"alarm_low"})
    assert sim_similar > sim_diff
    # 완전히 같은 문자열이면 1.0
    assert _ngram_max_similarity("hello", {"hello"}) == 1.0
    # 빈 reference → 0.0
    assert _ngram_max_similarity("x", set()) == 0.0


def test_snapshot_creates_file(tmp_path):
    from tools.drift_monitor import snapshot_csv
    csv_path = tmp_path / "Alarm.csv"
    _write_csv(csv_path, ["id", "code"],
               [["1", "ERR_A"], ["2", "ERR_B"]])
    with patch("tools.drift_monitor.DRIFT_DIR", str(tmp_path / "drift")):
        r = snapshot_csv(str(csv_path))
    assert r["had_previous"] is False
    # 첫 스냅샷이라 delta의 added_count는 전체
    assert r["delta"]["code"]["added_count"] == 2


def test_snapshot_detects_new_values(tmp_path):
    from tools.drift_monitor import snapshot_csv
    csv_path = tmp_path / "Alarm.csv"
    drift_dir = tmp_path / "drift"

    _write_csv(csv_path, ["id", "code"],
               [["1", "ERR_A"], ["2", "ERR_B"]])
    with patch("tools.drift_monitor.DRIFT_DIR", str(drift_dir)):
        snapshot_csv(str(csv_path))
        # 신규 값 추가
        _write_csv(csv_path, ["id", "code"],
                   [["1", "ERR_A"], ["2", "ERR_B"],
                    ["3", "ERR_C"], ["4", "ERR_QUANTUM_FLUX"]])
        r = snapshot_csv(str(csv_path))

    assert r["had_previous"] is True
    added = r["delta"]["code"]["added_count"]
    assert added == 2
    # ERR_QUANTUM_FLUX는 기존 ERR_A/B와 유사도 낮음 → emerging 후보
    emerging_values = [
        v["value"] for v in r["drift_scores"]["code"]["emerging_values"]
    ]
    assert "ERR_QUANTUM_FLUX" in emerging_values


def test_snapshot_detects_removed(tmp_path):
    from tools.drift_monitor import snapshot_csv
    csv_path = tmp_path / "t.csv"
    drift_dir = tmp_path / "drift"

    _write_csv(csv_path, ["v"], [["x"], ["y"], ["z"]])
    with patch("tools.drift_monitor.DRIFT_DIR", str(drift_dir)):
        snapshot_csv(str(csv_path))
        _write_csv(csv_path, ["v"], [["x"]])  # y, z 제거
        r = snapshot_csv(str(csv_path))
    assert r["delta"]["v"]["removed_count"] == 2


def test_monitor_all_csvs(tmp_path):
    from tools.drift_monitor import monitor_all_csvs
    _write_csv(tmp_path / "A.csv", ["x"], [["1"], ["2"]])
    _write_csv(tmp_path / "B.csv", ["y"], [["a"]])
    with patch("tools.drift_monitor.DRIFT_DIR", str(tmp_path / "drift")):
        r = monitor_all_csvs(str(tmp_path))
    assert r["tables"] == 2


def test_mcp_tool(tmp_path):
    from tools.drift_monitor import monitor_csv_drift
    _write_csv(tmp_path / "A.csv", ["x"], [["1"]])
    with patch("tools.drift_monitor.SOURCE_RAWDATA_DIR", str(tmp_path)), \
         patch("tools.drift_monitor.DRIFT_DIR", str(tmp_path / "drift")), \
         patch("tools.drift_monitor.GENERATED_REPORTS_DIR", str(tmp_path / "rpt")):
        raw = monitor_csv_drift()
    data = json.loads(raw)
    assert data["success"] is True
    assert data["tables"] == 1
