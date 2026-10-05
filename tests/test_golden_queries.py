"""Tests for tools/golden_queries.py — P1 Oracle-First Quality."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from rdflib import OWL, RDF, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph


@pytest.fixture
def sample_cases():
    return [
        {
            "id": "count_equipment",
            "question": "설비 수",
            "difficulty": "easy",
            "domains": ["EquipmentMaster"],
            "golden_sparql": (
                "SELECT (COUNT(?s) AS ?cnt) WHERE { ?s a steel:EquipmentMaster }"
            ),
            "assertions": {
                "min_rows": 1,
                "required_vars": ["cnt"],
                "sparql_must_contain": ["EquipmentMaster"],
                "sparql_must_not_contain": ["DELETE", "INSERT"],
            },
        },
        {
            "id": "missing_class",
            "question": "존재 안 하는 클래스",
            "difficulty": "easy",
            "domains": ["Nonexistent"],
            "golden_sparql": (
                "SELECT ?s WHERE { ?s a steel:Nonexistent }"
            ),
            "assertions": {
                "min_rows": 1,
                "required_vars": ["s"],
                "sparql_must_contain": ["Nonexistent"],
                "sparql_must_not_contain": [],
            },
        },
    ]


@pytest.fixture
def populated_graph():
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS
    eq = URIRef(f"{ns}EquipmentMaster")
    g.add((eq, RDF.type, OWL.Class))
    for i in range(3):
        u = URIRef(f"{inst}EquipmentMaster_EQ{i:03d}")
        g.add((u, RDF.type, eq))
    return g


class TestEvaluateAssertions:
    def test_min_rows_failure(self):
        from tools.golden_queries import _evaluate_assertions
        case = {"golden_sparql": "SELECT ?s", "assertions": {"min_rows": 5}}
        passed, failures = _evaluate_assertions([], case)
        assert passed is False
        assert any("min_rows" in f for f in failures)

    def test_required_vars_missing(self):
        from tools.golden_queries import _evaluate_assertions
        case = {"golden_sparql": "SELECT ?x", "assertions": {"min_rows": 1, "required_vars": ["cnt"]}}
        passed, failures = _evaluate_assertions([{"x": "v"}], case)
        assert passed is False
        assert any("required_vars" in f for f in failures)

    def test_must_not_contain_violation(self):
        from tools.golden_queries import _evaluate_assertions
        case = {"golden_sparql": "DELETE { ?s ?p ?o }",
                "assertions": {"sparql_must_not_contain": ["DELETE"]}}
        passed, failures = _evaluate_assertions([], case)
        assert passed is False
        assert any("DELETE" in f for f in failures)

    def test_all_pass(self):
        from tools.golden_queries import _evaluate_assertions
        case = {"golden_sparql": "SELECT ?cnt WHERE { ?x a steel:Equipment }",
                "assertions": {"min_rows": 1, "required_vars": ["cnt"],
                               "sparql_must_contain": ["Equipment"],
                               "sparql_must_not_contain": ["DELETE"]}}
        passed, failures = _evaluate_assertions([{"cnt": "3"}], case)
        assert passed is True
        assert failures == []


class TestRunSingle:
    def test_happy_path(self, sample_cases, populated_graph):
        from tools.golden_queries import _run_single
        with patch("tools.sparql_local._get_graph", return_value=(populated_graph, "merged")):
            r = _run_single(sample_cases[0], "merge")
        assert r["status"] == "PASS"
        assert r["row_count"] == 1

    def test_empty_result_fails_min_rows(self, sample_cases, populated_graph):
        from tools.golden_queries import _run_single
        with patch("tools.sparql_local._get_graph", return_value=(populated_graph, "merged")):
            r = _run_single(sample_cases[1], "merge")
        # SELECT without GROUP returns 0 rows for missing class → fails min_rows=1
        assert r["status"] == "FAIL"
        assert any("min_rows" in f for f in r["failures"])

    def test_sparql_error_captured(self, sample_cases, populated_graph):
        from tools.golden_queries import _run_single
        bad_case = dict(sample_cases[0], golden_sparql="SELECT ?s WHERE { NOT SPARQL")
        with patch("tools.sparql_local._get_graph", return_value=(populated_graph, "merged")):
            r = _run_single(bad_case, "merge")
        assert r["status"] == "ERROR"
        assert "error" in r


class TestDetectRegressions:
    def test_no_history(self):
        from tools.golden_queries import _detect_regressions
        r = _detect_regressions([{"id": "a", "status": "PASS"}], [])
        assert r["has_previous"] is False

    def test_detects_pass_to_fail(self):
        from tools.golden_queries import _detect_regressions
        prev = [{"timestamp": "2026-04-17T00:00:00",
                 "results": [{"id": "a", "status": "PASS"}, {"id": "b", "status": "PASS"}]}]
        cur = [{"id": "a", "status": "FAIL", "failures": ["min_rows"]},
               {"id": "b", "status": "PASS"}]
        r = _detect_regressions(cur, prev)
        assert r["has_previous"] is True
        assert len(r["regressions"]) == 1
        assert r["regressions"][0]["id"] == "a"

    def test_detects_recovery(self):
        from tools.golden_queries import _detect_regressions
        prev = [{"timestamp": "t", "results": [{"id": "a", "status": "FAIL"}]}]
        cur = [{"id": "a", "status": "PASS"}]
        r = _detect_regressions(cur, prev)
        assert len(r["recoveries"]) == 1


class TestSkipGoldenRegression:
    """skip_golden_regression: explicit skip marker for S12.5 (X1)."""

    def test_skip_golden_regression_records_checkpoint(self, tmp_path):
        """skip_golden_regression 이 save_step 호출해 체크포인트 기록."""
        import json
        from unittest.mock import patch

        from tools.golden_queries import skip_golden_regression

        # STATE_PATH 를 tmp_path 로 리다이렉트
        state_path = str(tmp_path / "state.json")
        with patch("tools.pipeline_state._STATE_PATH", state_path), \
             patch("tools.pipeline_state.GENERATED_DIR", str(tmp_path)):
            result = json.loads(skip_golden_regression(reason="test"))

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "test"
        assert result["next_step"] == "S13_REPORT"

        # Verify save_step was called
        import os
        assert os.path.exists(state_path)
        with open(state_path) as f:
            state = json.load(f)
        assert "S12_5_GOLDEN_REGRESSION" in state["completed_steps"]
        step = state["completed_steps"]["S12_5_GOLDEN_REGRESSION"]
        assert step["result"]["skipped"] is True
        assert step["result"]["reason"] == "test"


class TestRunGoldenQueriesTool:
    def test_missing_file_returns_error(self, tmp_path):
        from tools.golden_queries import run_golden_queries
        with patch("tools.golden_queries._GOLDEN_PATH", str(tmp_path / "nope.json")):
            result = json.loads(run_golden_queries(record_history=False))
        assert result["success"] is False

    def test_happy_path(self, sample_cases, populated_graph, tmp_path):
        from tools.golden_queries import run_golden_queries
        golden_path = tmp_path / "golden.json"
        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(sample_cases, f)
        hist_path = tmp_path / "hist.json"

        with patch("tools.golden_queries._GOLDEN_PATH", str(golden_path)), \
             patch("tools.golden_queries._HISTORY_PATH", str(hist_path)), \
             patch("tools.sparql_local._get_graph",
                   return_value=(populated_graph, "merged")):
            result = json.loads(run_golden_queries(record_history=True))

        assert result["success"] is True
        assert result["summary"]["total"] == 2
        assert result["summary"]["passed"] == 1  # count_equipment pass
        assert result["summary"]["failed"] == 1  # missing_class fail
        assert hist_path.exists()

    def test_history_append_and_regression(self, sample_cases, populated_graph, tmp_path):
        from tools.golden_queries import run_golden_queries
        golden_path = tmp_path / "golden.json"
        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(sample_cases, f)
        hist_path = tmp_path / "hist.json"

        with patch("tools.golden_queries._GOLDEN_PATH", str(golden_path)), \
             patch("tools.golden_queries._HISTORY_PATH", str(hist_path)), \
             patch("tools.sparql_local._get_graph",
                   return_value=(populated_graph, "merged")):
            run_golden_queries(record_history=True)
            r2 = json.loads(run_golden_queries(record_history=True))

        # 두 번째 실행은 이전과 비교 가능
        assert r2["regression"]["has_previous"] is True
        # 동일 그래프라 regression/recovery 없음
        assert r2["regression"]["regressions"] == []

    def test_run_golden_queries_response_has_next_step(self, sample_cases, populated_graph, tmp_path):
        """run_golden_queries 응답에 next_step 필드 포함 (X1)."""
        from tools.golden_queries import run_golden_queries
        golden_path = tmp_path / "golden.json"
        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(sample_cases, f)
        hist_path = tmp_path / "hist.json"

        with patch("tools.golden_queries._GOLDEN_PATH", str(golden_path)), \
             patch("tools.golden_queries._HISTORY_PATH", str(hist_path)), \
             patch("tools.sparql_local._get_graph",
                   return_value=(populated_graph, "merged")):
            result = json.loads(run_golden_queries(record_history=False))

        assert result["success"] is True
        assert "next_step" in result
        assert result["next_step"] == "S13_REPORT"
