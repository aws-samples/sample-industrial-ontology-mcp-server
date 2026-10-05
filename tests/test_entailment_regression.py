"""Tests for run_entailment_regression (R13 — axiom behaviour verification).

Entailment regression complements mutation testing:
    * mutation testing   = does the *checker* notice bad T-Box?
    * entailment testing = does the *schema* entail what we want?

The tool loads an inferred graph and runs a set of SPARQL ASK queries
declared as "positive" (must entail) or "negative" (must NOT entail).
"""
from __future__ import annotations

import json

import pytest
from rdflib import Graph

from tools.entailment_regression import (
    _run_golden_set,
    load_golden_set,
)

SAMPLE_TTL = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

steel:Equipment a owl:Class .
steel:Pump a owl:Class ; rdfs:subClassOf steel:Equipment .
steel:pump001 a steel:Pump .
# Note: no explicit steel:pump001 a steel:Equipment — relies on subClassOf entailment.
steel:scope1 a steel:Scope1Emission .
"""


class TestLoadGoldenSet:
    def test_load_valid_schema(self, tmp_path):
        golden = {
            "positive": [
                {"id": "p1", "description": "Pump instances are Equipment",
                 "query": "ASK { <http://example.com/steel-ontology#pump001> a <http://example.com/steel-ontology#Equipment> }"},
            ],
            "negative": [
                {"id": "n1", "description": "Scope1 never also Scope2",
                 "query": "ASK { ?x a <http://example.com/steel-ontology#Scope1Emission> , <http://example.com/steel-ontology#Scope2Emission> }"},
            ],
        }
        p = tmp_path / "golden.json"
        p.write_text(json.dumps(golden), encoding="utf-8")
        loaded = load_golden_set(str(p))
        assert loaded["positive"][0]["id"] == "p1"
        assert loaded["negative"][0]["id"] == "n1"

    def test_missing_file_returns_empty(self, tmp_path):
        loaded = load_golden_set(str(tmp_path / "nope.json"))
        assert loaded == {"positive": [], "negative": []}


class TestRunGoldenSet:
    def _graph_with_entailment(self) -> Graph:
        """Build a graph that already contains the entailed triple, simulating
        the output of run_owl_rl_inference."""
        g = Graph()
        g.parse(data=SAMPLE_TTL + """
steel:pump001 a steel:Equipment .
""", format="turtle")
        return g

    def test_positive_pass_when_triple_entailed(self):
        g = self._graph_with_entailment()
        golden = {
            "positive": [{"id": "p1", "description": "",
                          "query": "ASK { <http://example.com/steel-ontology#pump001> a <http://example.com/steel-ontology#Equipment> }"}],
            "negative": [],
        }
        report = _run_golden_set(g, golden)
        assert report["pass_rate"] == 1.0
        assert report["failures"] == []
        assert report["positive_pass"] == 1

    def test_positive_fail_when_triple_missing(self):
        # No entailment done — expect failure.
        g = Graph()
        g.parse(data=SAMPLE_TTL, format="turtle")
        golden = {
            "positive": [{"id": "p1", "description": "pump inherits equipment",
                          "query": "ASK { <http://example.com/steel-ontology#pump001> a <http://example.com/steel-ontology#Equipment> }"}],
            "negative": [],
        }
        report = _run_golden_set(g, golden)
        assert report["pass_rate"] == 0.0
        assert len(report["failures"]) == 1
        assert report["failures"][0]["id"] == "p1"
        assert report["failures"][0]["kind"] == "positive"

    def test_negative_pass_when_triple_absent(self):
        g = self._graph_with_entailment()
        golden = {
            "positive": [],
            "negative": [{"id": "n1", "description": "disjoint scopes",
                          "query": "ASK { ?x a <http://example.com/steel-ontology#Scope1Emission> , <http://example.com/steel-ontology#Scope2Emission> }"}],
        }
        report = _run_golden_set(g, golden)
        assert report["pass_rate"] == 1.0
        assert report["negative_pass"] == 1

    def test_negative_fail_when_forbidden_triple_present(self):
        g = Graph()
        g.parse(data=SAMPLE_TTL + """
steel:scope1 a steel:Scope2Emission .
""", format="turtle")
        golden = {
            "positive": [],
            "negative": [{"id": "n1", "description": "disjoint scopes",
                          "query": "ASK { ?x a <http://example.com/steel-ontology#Scope1Emission> , <http://example.com/steel-ontology#Scope2Emission> }"}],
        }
        report = _run_golden_set(g, golden)
        assert report["pass_rate"] == 0.0
        assert len(report["failures"]) == 1
        assert report["failures"][0]["kind"] == "negative"

    def test_mixed_set_reports_per_kind_counts(self):
        g = self._graph_with_entailment()
        golden = {
            "positive": [
                {"id": "p1", "description": "", "query": "ASK { <http://example.com/steel-ontology#pump001> a <http://example.com/steel-ontology#Equipment> }"},
                {"id": "p2", "description": "", "query": "ASK { <http://example.com/steel-ontology#nonexistent> a <http://example.com/steel-ontology#Equipment> }"},
            ],
            "negative": [
                {"id": "n1", "description": "", "query": "ASK { ?x a <http://example.com/steel-ontology#Scope1Emission> , <http://example.com/steel-ontology#Scope2Emission> }"},
            ],
        }
        report = _run_golden_set(g, golden)
        assert report["positive_total"] == 2
        assert report["positive_pass"] == 1
        assert report["negative_total"] == 1
        assert report["negative_pass"] == 1
        # 2 out of 3 pass → pass_rate ≈ 0.666...
        assert 0.6 < report["pass_rate"] < 0.7

    def test_empty_golden_set_returns_na_pass_rate(self):
        g = self._graph_with_entailment()
        report = _run_golden_set(g, {"positive": [], "negative": []})
        assert report["total"] == 0
        # pass_rate for empty set is defined as 1.0 (vacuously true) to avoid
        # division-by-zero errors in downstream gating.
        assert report["pass_rate"] == 1.0

    def test_malformed_query_counted_as_failure(self):
        g = self._graph_with_entailment()
        golden = {
            "positive": [{"id": "bad", "description": "",
                          "query": "SELECT bogus where {}"}],
            "negative": [],
        }
        report = _run_golden_set(g, golden)
        # Malformed ASK → treated as a failure with error reason captured.
        assert report["pass_rate"] < 1.0
        assert any(f["id"] == "bad" for f in report["failures"])


class TestProjectGoldenSet:
    """Smoke-test that the shipped rules/domain/entailment_golden.json is internally
    consistent against the improved T-Box. Guards against a dev carelessly
    adding an ASK for a class or axiom that doesn't exist — such a query
    would silently pass (negatives) or fail (positives), masking regressions.
    """

    def test_golden_set_passes_on_improved_tbox(self, tmp_path):
        import os
        tbox_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "generated", "tbox", "t_box.ttl",
        )
        if not os.path.exists(tbox_path):
            pytest.skip("T-Box fixture missing; run generate_tbox first")
        from tools.ontology_quality import improve_tbox
        with open(tbox_path, encoding="utf-8") as f:
            improved, _ = improve_tbox(f.read())
        g = Graph()
        g.parse(data=improved, format="turtle")
        golden = load_golden_set()
        report = _run_golden_set(g, golden)
        # The shipped golden set must be authored so that every query matches
        # the committed T-Box. Any deviation here is an editing error, not a
        # reasoner regression.
        assert report["pass_rate"] == 1.0, (
            f"golden set out of sync with T-Box: failures={report['failures']}"
        )
