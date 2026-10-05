"""Tests for tools/report.py::_render_meta_audit_section — R10 관련 단계."""

import json

from tools.report import _render_meta_audit_section


def test_render_meta_audit_section_when_artifact_exists(tmp_path, monkeypatch):
    audit_path = tmp_path / "latest.json"
    audit_path.write_text(json.dumps({
        "sensitivity_matrix": {"check_a": {"M1": 0.83, "M5": 0.0}},
        "blind_spots": [{"category": "M5", "mutants": 3, "caught_by_any_check": 0,
                         "recommendation": "add_annotation_check"}],
        "pairwise_correlation": [{"pair": ["x", "y"], "cooccurrence": 0.95,
                                  "sample_size": 6, "interpretation": "redundant_candidate"}],
        "dead_checks": [{"check": "z", "history_fires": 0, "mutation_catches": 0,
                         "recommendation": "consider_removal"}],
        "roi_column": [],
        "history_insufficient": False,
    }))
    html = _render_meta_audit_section(str(audit_path))
    assert "Meta-Audit" in html
    assert "Blind Spots" in html
    assert "M5" in html
    assert "redundant_candidate" in html
    assert "consider_removal" in html


def test_render_meta_audit_section_returns_empty_when_missing():
    html = _render_meta_audit_section("/nonexistent/path.json")
    assert html == ""
