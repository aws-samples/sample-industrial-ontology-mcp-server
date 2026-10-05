"""Tests for tools/adversarial.py — P5 Red Team."""
from __future__ import annotations

import json
from pathlib import Path


def test_builders_produce_files(tmp_path):
    from tools.adversarial import ADVERSARIAL_CASES
    for case in ADVERSARIAL_CASES:
        p = case["builder"](str(tmp_path))
        assert Path(p).exists()
        if case["id"] != "empty_table":
            assert Path(p).stat().st_size > 0


def test_run_adversarial_suite_returns_expected_fields():
    from tools.adversarial import run_adversarial_suite
    report = run_adversarial_suite()
    assert "summary" in report
    s = report["summary"]
    assert s["total_cases"] == 8
    assert s["detected"] + s["partial"] + s["missed"] + s["known_gaps"] == s["total_cases"]
    assert 0 <= s["detection_rate"] <= 100


def test_type_confusion_detected():
    """mixed_types는 profile_csv_data가 반드시 잡아야 한다."""
    from tools.adversarial import run_adversarial_suite
    report = run_adversarial_suite()
    case = next(c for c in report["cases"] if c["id"] == "type_confusion")
    assert case["status"] in ("detected", "partial")
    assert "mixed_types" in case["detected"]


def test_empty_table_detected():
    from tools.adversarial import run_adversarial_suite
    report = run_adversarial_suite()
    case = next(c for c in report["cases"] if c["id"] == "empty_table")
    assert case["status"] in ("detected", "partial")


def test_high_null_detected():
    from tools.adversarial import run_adversarial_suite
    report = run_adversarial_suite()
    case = next(c for c in report["cases"] if c["id"] == "high_null")
    assert case["status"] in ("detected", "partial")
    assert "high_null" in case["detected"]


def test_known_gaps_reported():
    """fk_circular, duplicate_pk 등은 현재 탐지 불가 → known_gap로 보고."""
    from tools.adversarial import run_adversarial_suite
    report = run_adversarial_suite()
    gap_ids = {c["id"] for c in report["cases"] if c["status"] == "known_gap"}
    # 최소한 명시적으로 gap으로 등록한 것은 known_gap 상태여야 함
    for expected_gap in ("fk_circular", "duplicate_pk", "time_reversal"):
        assert expected_gap in gap_ids


def test_report_written_when_dir_given(tmp_path):
    from tools.adversarial import run_adversarial_suite
    report = run_adversarial_suite(output_dir=str(tmp_path))
    p = Path(report["report_path"])
    assert p.exists()
    data = json.loads(p.read_text())
    assert "summary" in data and "cases" in data


def test_mcp_tool_runs(tmp_path, monkeypatch):
    """MCP 래퍼가 성공 응답을 낸다.

    ``GENERATED_REPORTS_DIR`` 을 tmp 로 돌린다 — 이 도구는 배포
    ``reports/adversarial_report.json`` 에 쓰고, 그것을 테스트가 덮으면 실측 감사
    산출물이 픽스처로 교체된다 (실측 2026-09-01: 오염된 12개 파일 중 하나가 이것이다).
    래퍼가 확인해야 할 것은 응답 계약이지 쓰기 위치가 아니다.
    """
    import tools.adversarial as adv

    monkeypatch.setattr(adv, "GENERATED_REPORTS_DIR", str(tmp_path), raising=False)
    raw = adv.run_adversarial_csv_suite()
    data = json.loads(raw)
    assert data["success"] is True
    assert "summary" in data
