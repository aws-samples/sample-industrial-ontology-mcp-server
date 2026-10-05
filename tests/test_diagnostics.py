"""Tests for tools/validation_support/diagnostics.py — Session 8."""
from __future__ import annotations

import json


class TestDiagnoseCheckFailure:
    def test_inverseof_schema_gap(self):
        from tools.validation_support.diagnostics import diagnose_check_failure
        r = diagnose_check_failure({"name": "ObjectProperty 양방향 연결"})
        assert r["category"] == "schema_gap"
        assert r["auto_fixable"] is True

    def test_orphan_data_quality(self):
        from tools.validation_support.diagnostics import diagnose_check_failure
        r = diagnose_check_failure({"name": "고아 노드 탐지"})
        assert r["category"] == "data_quality"

    def test_dangling_fk_broken(self):
        from tools.validation_support.diagnostics import diagnose_check_failure
        r = diagnose_check_failure({"name": "댕글링 참조 탐지"})
        assert r["category"] == "fk_broken"

    def test_outlier_category(self):
        from tools.validation_support.diagnostics import diagnose_check_failure
        r = diagnose_check_failure({"name": "수치 이상치 탐지"})
        assert r["category"] == "data_quality"

    def test_unknown_name_fallback(self):
        from tools.validation_support.diagnostics import diagnose_check_failure
        r = diagnose_check_failure({"name": "전혀 모르는 검증"})
        assert r["category"] == "data_quality"
        assert "상세 위반 목록 참조" in r["fix_suggestion"]


class TestTraceViolations:
    def test_no_provenance_file_empty(self, tmp_path):
        from tools.validation_support.diagnostics import trace_violations_to_provenance
        r = trace_violations_to_provenance(
            [{"subject": "X"}],
            provenance_path=str(tmp_path / "nope.json"),
        )
        assert r == []

    def test_happy_path(self, tmp_path):
        from tools.validation_support.diagnostics import trace_violations_to_provenance
        prov = {
            "http://steel/inst/Eq001": {
                "source_table": "equipment.csv",
                "source_row": 42,
                "pk_column": "equipment_id",
                "pk_value": "Eq001",
            },
        }
        prov_path = tmp_path / "abox_provenance.json"
        prov_path.write_text(json.dumps(prov))
        traces = trace_violations_to_provenance(
            [{"subject": "Eq001", "issue": "missing_required"}],
            provenance_path=str(prov_path),
        )
        assert len(traces) == 1
        assert traces[0]["source_table"] == "equipment.csv"
        assert traces[0]["source_row"] == 42
        assert traces[0]["violation"] == "missing_required"

    def test_max_traces_respected(self, tmp_path):
        from tools.validation_support.diagnostics import trace_violations_to_provenance
        prov = {
            f"http://steel/inst/I{i}": {"source_table": "t.csv", "source_row": i}
            for i in range(20)
        }
        prov_path = tmp_path / "abox_provenance.json"
        prov_path.write_text(json.dumps(prov))
        violations = [{"subject": f"I{i}"} for i in range(20)]
        traces = trace_violations_to_provenance(
            violations, max_traces=5, provenance_path=str(prov_path),
        )
        assert len(traces) == 5

    def test_no_matching_subject_skipped(self, tmp_path):
        from tools.validation_support.diagnostics import trace_violations_to_provenance
        prov_path = tmp_path / "abox_provenance.json"
        prov_path.write_text(json.dumps({"http://x/A": {"source_table": "a.csv"}}))
        traces = trace_violations_to_provenance(
            [{"subject": "NonExistent"}], provenance_path=str(prov_path),
        )
        assert traces == []

    def test_delegate_from_kg_validation(self, tmp_path):
        """kg_validation._trace_violations_to_provenance가 동일하게 작동."""
        from unittest.mock import patch

        from tools.kg_validation import _trace_violations_to_provenance

        prov_path = tmp_path / "abox_provenance.json"
        prov_path.write_text(json.dumps({
            "http://x/Eq1": {"source_table": "eq.csv", "source_row": 1},
        }))
        with patch("tools.kg_validation.GENERATED_ABOX_DIR", str(tmp_path)):
            r = _trace_violations_to_provenance([{"subject": "Eq1"}])
        assert len(r) == 1
        assert r[0]["source_table"] == "eq.csv"


class TestPackageReexports:
    def test_init_exports(self):
        from tools.validation_support import (
            diagnose_check_failure,
            trace_violations_to_provenance,
        )
        assert callable(diagnose_check_failure)
        assert callable(trace_violations_to_provenance)
