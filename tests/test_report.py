"""Tests for tools/report.py — pipeline final report generation."""

import json
import os
from unittest.mock import patch

from tools.report import (
    _collect_abox_stats,
    _collect_inferred_stats,
    _collect_tbox_stats,
    _file_info,
    _human_size,
    generate_pipeline_report,
)


class TestHumanSize:
    """_human_size: human-readable file size."""

    def test_bytes(self):
        assert _human_size(512) == "512.0 B"

    def test_kilobytes(self):
        assert _human_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _human_size(3 * 1024 * 1024) == "3.0 MB"

    def test_zero(self):
        assert _human_size(0) == "0.0 B"


class TestFileInfo:
    """_file_info: file metadata extraction."""

    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.ttl"
        f.write_text("some content")
        info = _file_info(str(f))
        assert info["exists"] is True
        assert info["size_bytes"] > 0
        assert "modified" in info
        assert "size_display" in info

    def test_missing_file(self):
        info = _file_info("/nonexistent/file.ttl")
        assert info["exists"] is False
        assert info["path"] == "/nonexistent/file.ttl"


class TestCollectTboxStats:
    """_collect_tbox_stats: T-Box statistics."""

    def test_with_valid_tbox(self, sample_tbox_ttl, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(sample_tbox_ttl)
        stats = _collect_tbox_stats(str(tbox_file))
        assert stats is not None
        assert stats["classes"] == 2
        assert stats["object_properties"] == 1
        assert stats["datatype_properties"] == 2
        assert stats["triples"] > 0
        assert "hierarchy" in stats

    def test_missing_file_returns_none(self):
        stats = _collect_tbox_stats("/nonexistent/t_box.ttl")
        assert stats is None


class TestCollectAboxStats:
    """_collect_abox_stats: A-Box line count."""

    def test_with_valid_file(self, sample_abox_ttl, tmp_path):
        abox_file = tmp_path / "a_box.ttl"
        abox_file.write_text(sample_abox_ttl)
        stats = _collect_abox_stats(str(abox_file))
        assert stats is not None
        assert stats["lines"] > 0

    def test_missing_file_returns_none(self):
        stats = _collect_abox_stats("/nonexistent/a_box.ttl")
        assert stats is None


class TestCollectInferredStats:
    """_collect_inferred_stats: inferred result line count."""

    def test_with_valid_file(self, tmp_path):
        f = tmp_path / "all_inferred.ttl"
        f.write_text("line1\nline2\nline3\n")
        stats = _collect_inferred_stats(str(f))
        assert stats is not None
        assert stats["lines"] == 3

    def test_missing_file_returns_none(self):
        stats = _collect_inferred_stats("/nonexistent/inferred.ttl")
        assert stats is None


class TestGeneratePipelineReport:
    """generate_pipeline_report: full HTML report generation."""

    @patch("tools.report.webbrowser.open")
    @patch("tools.report._generate_tbox_visualization", return_value="")
    @patch("tools.report._collect_dict_validation", return_value={"passed": True, "summary": {}, "coverage": {}, "sections": {}, "issues": []})
    @patch("tools.report._collect_dict_stats", return_value={"classes": 5, "object_properties": 3, "functional_properties": 0, "anti_patterns": 2, "common_patterns": 4, "question_templates": 10})
    def test_happy_path(self, mock_dict_stats, mock_dict_valid, mock_vis, mock_browser,
                        sample_tbox_ttl, sample_abox_ttl, tmp_path):
        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        reports_dir = tmp_path / "reports"
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report.GENERATED_REPORTS_DIR", str(reports_dir)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(generate_pipeline_report(open_report=False))

        assert result["success"] is True
        assert os.path.exists(result["path"])
        assert result["summary"]["classes"] == 2
        mock_browser.assert_not_called()

    @patch("tools.report.webbrowser.open")
    @patch("tools.report._generate_tbox_visualization", return_value="")
    @patch("tools.report._collect_dict_validation", return_value=None)
    @patch("tools.report._collect_dict_stats", return_value=None)
    def test_report_with_missing_artifacts(self, mock_dict_stats, mock_dict_valid,
                                            mock_vis, mock_browser, tmp_path):
        reports_dir = tmp_path / "reports"
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        with patch("tools.report.TBOX_PATH", "/nonexistent/t_box.ttl"), \
             patch("tools.report.ABOX_PATH", "/nonexistent/a_box.ttl"), \
             patch("tools.report.INFERRED_PATH", "/nonexistent/inferred.ttl"), \
             patch("tools.report.SEMANTIC_DICT_PATH", "/nonexistent/dict.json"), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report.GENERATED_REPORTS_DIR", str(reports_dir)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(generate_pipeline_report(open_report=False))

        assert result["success"] is True
        assert result["summary"]["classes"] == 0
        mock_browser.assert_not_called()


class TestCollectGoldenQueriesResult:
    """_collect_golden_queries_result: golden_history.json integration (X1)."""

    def test_no_history_file_returns_not_configured(self, tmp_path):
        """golden_history.json 없을 때 not_configured 상태."""
        from tools.report import _collect_golden_queries_result
        with patch("tools.report.GENERATED_REPORTS_DIR", str(tmp_path)):
            result = _collect_golden_queries_result()
        assert result["status"] == "not_configured"
        assert "미설정" in result["message"]

    def test_empty_history_returns_not_configured(self, tmp_path):
        """golden_history.json 비어있을 때 not_configured 상태."""
        from tools.report import _collect_golden_queries_result
        hist_path = tmp_path / "golden_history.json"
        hist_path.write_text("[]")
        with patch("tools.report.GENERATED_REPORTS_DIR", str(tmp_path)):
            result = _collect_golden_queries_result()
        assert result["status"] == "not_configured"

    def test_valid_history_returns_executed(self, tmp_path):
        """유효한 golden_history.json 있을 때 executed 상태."""
        from tools.report import _collect_golden_queries_result
        hist_path = tmp_path / "golden_history.json"
        history = [
            {
                "timestamp": "2026-05-04T12:00:00",
                "summary": {"total": 5, "passed": 4, "failed": 1, "pass_rate": 80.0},
                "results": [{"id": "g1", "status": "PASS"}],
                "regression": {"regressions": [{"id": "g2", "from": "PASS", "to": "FAIL"}]},
            }
        ]
        hist_path.write_text(json.dumps(history))
        with patch("tools.report.GENERATED_REPORTS_DIR", str(tmp_path)):
            result = _collect_golden_queries_result()
        assert result["status"] == "executed"
        assert result["timestamp"] == "2026-05-04T12:00:00"
        assert result["summary"]["total"] == 5
        assert result["summary"]["passed"] == 4
        assert len(result["regression"]["regressions"]) == 1
        assert result["results_count"] == 1

    def test_corrupted_json_returns_error(self, tmp_path):
        """손상된 golden_history.json 파일 시 error 상태."""
        from tools.report import _collect_golden_queries_result
        hist_path = tmp_path / "golden_history.json"
        hist_path.write_text("{bad json")
        with patch("tools.report.GENERATED_REPORTS_DIR", str(tmp_path)):
            result = _collect_golden_queries_result()
        assert result["status"] == "error"


class TestReportGoldenQueriesIntegration:
    """Report HTML includes golden queries section (X1)."""

    def test_report_includes_golden_section_when_configured(self, tmp_path):
        """보고서에 golden_history 섹션 포함 — 있을 때."""
        from tools.report import _build_html, _collect_golden_queries_result

        # Setup golden_history.json
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        hist_path = reports_dir / "golden_history.json"
        history = [
            {
                "timestamp": "2026-05-04T12:00:00",
                "summary": {"total": 5, "passed": 5, "failed": 0, "pass_rate": 100.0},
                "results": [{"id": "g1", "status": "PASS"}],
                "regression": {"regressions": [], "recoveries": []},
            }
        ]
        hist_path.write_text(json.dumps(history))

        # Build HTML with golden data
        with patch("tools.report.GENERATED_REPORTS_DIR", str(reports_dir)):
            gq_result = _collect_golden_queries_result()

        data = {
            "generated_at": "2026-05-04 12:00:00",
            "artifacts": {
                "tbox": {"exists": False},
                "abox": {"exists": False},
                "inferred": {"exists": False},
                "semantic_dict": {"exists": False},
            },
            "source": {"csv_count": 0},
            "tbox_stats": None,
            "abox_stats": None,
            "inferred_stats": None,
            "tacit_stats": {"count": 0, "files": [], "total_triples": 0},
            "dict_stats": {},
            "dict_validation": None,
            "loss_budget": None,
            "golden_queries": gq_result,
            "tbox_vis_path": "",
            "meta_audit_html": "",
        }

        html = _build_html(data)
        assert "Golden Query 회귀" in html
        assert "100% PASS" in html or "100.0% PASS" in html
        assert "총 케이스" in html

    def test_report_shows_not_configured_when_no_history(self, tmp_path):
        """golden_history.json 없을 때 '미설정' 메시지 표시."""
        from tools.report import _build_html, _collect_golden_queries_result

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        with patch("tools.report.GENERATED_REPORTS_DIR", str(reports_dir)):
            gq_result = _collect_golden_queries_result()

        data = {
            "generated_at": "2026-05-04 12:00:00",
            "artifacts": {
                "tbox": {"exists": False},
                "abox": {"exists": False},
                "inferred": {"exists": False},
                "semantic_dict": {"exists": False},
            },
            "source": {"csv_count": 0},
            "tbox_stats": None,
            "abox_stats": None,
            "inferred_stats": None,
            "tacit_stats": {"count": 0, "files": [], "total_triples": 0},
            "dict_stats": {},
            "dict_validation": None,
            "loss_budget": None,
            "golden_queries": gq_result,
            "tbox_vis_path": "",
            "meta_audit_html": "",
        }

        html = _build_html(data)
        assert "Golden Query 회귀" in html
        assert "미설정" in html
        assert "add_golden_queries" in html


class TestCqFeedbackRotation:
    """_render_cq_feedback_rotation: S13 보고서 반복 실패 경고 섹션.

    T3 리뷰 Important #4: branches (no file / no iterations / no stubborn /
    stubborn present) 모두 unit test.
    """

    def test_cq_feedback_rotation_no_file(self, tmp_path, monkeypatch):
        """파일 없으면 빈 문자열."""
        from tools.report import _render_cq_feedback_rotation
        monkeypatch.setattr(
            "tools.cq_feedback.CQ_FEEDBACK_PATH", str(tmp_path / "nonexistent.json")
        )
        assert _render_cq_feedback_rotation() == ""

    def test_cq_feedback_rotation_no_stubborn(self, tmp_path, monkeypatch):
        """age < 3 인 suggestion 만 있으면 빈 문자열."""
        import tools.cq_feedback as cq_fb
        from tools.report import _render_cq_feedback_rotation
        monkeypatch.setattr(cq_fb, "CQ_FEEDBACK_PATH", str(tmp_path / "fb.json"))

        cq_fb.append_iteration(
            pass_rate=60.0, total_cqs=10, passed=6, failed=4,
            suggestions=[
                {"cq_id": "CQ-01", "type": "missing_connection",
                 "class_a": "A", "class_b": "B", "action": "...",
                 "age_iterations": 2},
            ],
        )
        assert _render_cq_feedback_rotation() == ""

    def test_cq_feedback_rotation_renders_stubborn(self, tmp_path, monkeypatch):
        """age >= 3 인 suggestion 있으면 경고 섹션 HTML 생성."""
        import tools.cq_feedback as cq_fb
        from tools.report import _render_cq_feedback_rotation
        monkeypatch.setattr(cq_fb, "CQ_FEEDBACK_PATH", str(tmp_path / "fb.json"))

        # 같은 suggestion 을 3번 기록해 age=3 달성
        sug = {"cq_id": "CQ-01", "type": "missing_connection",
               "class_a": "A", "class_b": "B", "action": "..."}
        for rate in [50.0, 55.0, 60.0]:  # 개선 추세 (circuit breaker 회피)
            cq_fb.append_iteration(
                pass_rate=rate, total_cqs=10, passed=int(rate / 10),
                failed=10 - int(rate / 10), suggestions=[dict(sug)],
            )

        html = _render_cq_feedback_rotation()
        assert html != ""
        assert "반복" in html or "⚠️" in html
        assert "A" in html  # class_a 가 렌더됨


class TestMetaAuditPlaybook:
    """_render_meta_audit_playbook: S13 보고서 meta-audit 액션 플레이북 섹션 (X3).

    3 분기: 파일 없음 / playbook 비어있음 / entries 존재.
    """

    def test_playbook_missing_file(self, tmp_path, monkeypatch):
        """latest.json 없으면 빈 문자열 반환."""
        import tools.report as report_mod
        from tools.report import _render_meta_audit_playbook
        monkeypatch.setattr(report_mod, "GENERATED_DIR", str(tmp_path))
        # meta_audit 디렉토리 미생성 → 파일 없음
        assert _render_meta_audit_playbook() == ""

    def test_playbook_empty(self, tmp_path, monkeypatch):
        """action_playbook 비어있으면 '블라인드 스팟 없음' info 박스."""
        import tools.report as report_mod
        from tools.report import _render_meta_audit_playbook
        monkeypatch.setattr(report_mod, "GENERATED_DIR", str(tmp_path))
        audit_dir = tmp_path / "meta_audit"
        audit_dir.mkdir()
        (audit_dir / "latest.json").write_text(json.dumps({
            "action_playbook": [],
            "blind_spots": [],
            "dead_checks": [],
        }))
        html_out = _render_meta_audit_playbook()
        assert "블라인드 스팟 없음" in html_out
        # 경고 테이블 없어야
        assert "<table>" not in html_out

    def test_playbook_renders_entries(self, tmp_path, monkeypatch):
        """playbook 있으면 우선순위 테이블 렌더."""
        import tools.report as report_mod
        from tools.report import _render_meta_audit_playbook
        monkeypatch.setattr(report_mod, "GENERATED_DIR", str(tmp_path))
        audit_dir = tmp_path / "meta_audit"
        audit_dir.mkdir()
        (audit_dir / "latest.json").write_text(json.dumps({
            "action_playbook": [
                {
                    "priority": "high",
                    "type": "blind_spot",
                    "category": "M4",
                    "issue": "M4 mutant 2개 중 0개만 감지됨",
                    "expected_checks": ["disjoint_class_violations"],
                    "suggested_action": {
                        "kind": "extend_check",
                        "investigation_hint": "...",
                        "catalog_path": "rules/mutations/M4_*/*.sparql",
                    },
                    "playbook_reference": "post-workshop-guide §11",
                },
                {
                    "priority": "low",
                    "type": "dead_check",
                    "check": "fk_op_coverage",
                    "issue": "check 'fk_op_coverage' 가 0 catch",
                    "suggested_action": {
                        "kind": "investigate_or_remove",
                        "keep_if": "...",
                        "remove_if": "...",
                    },
                },
            ],
        }))
        html_out = _render_meta_audit_playbook()
        assert html_out != ""
        assert "HIGH" in html_out
        assert "LOW" in html_out
        assert "M4" in html_out
        assert "fk_op_coverage" in html_out
        assert "extend_check" in html_out
        assert "investigate_or_remove" in html_out
