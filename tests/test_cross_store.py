"""Tests for tools/cross_store_validation.py — P4."""
from __future__ import annotations

import json
from unittest.mock import patch

from rdflib import OWL, RDF, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph


def _populated_graph(n_instances: int = 3) -> object:
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS
    eq = URIRef(f"{ns}EquipmentMaster")
    g.add((eq, RDF.type, OWL.Class))
    for i in range(n_instances):
        u = URIRef(f"{inst}EquipmentMaster_EQ{i:03d}")
        g.add((u, RDF.type, eq))
    has_p = URIRef(f"{ns}hasProperty")
    g.add((has_p, RDF.type, OWL.ObjectProperty))
    return g


class TestCompareRow:
    def test_identical_when_same_graph(self):
        from tools.cross_store_validation import _QUERIES, _compare_row
        g = _populated_graph(5)
        with patch("tools.sparql_local._get_graph", return_value=(g, "merged")), \
             patch("tools.cross_store_validation.os.path.exists", return_value=True):
            r = _compare_row(_QUERIES[0])  # total_triples
        # merge와 inferred가 같은 mock 리턴 → identical
        assert r["status"] in ("identical", "near_identical")
        assert r["max_diff_pct"] == 0

    def test_insufficient_when_single_store(self):
        from tools.cross_store_validation import _QUERIES, _compare_row
        g = _populated_graph(5)
        # inferred 없음, neo4j 없음 → 1 store만
        with patch("tools.sparql_local._get_graph", return_value=(g, "merged")), \
             patch("tools.cross_store_validation.os.path.exists", return_value=False):
            r = _compare_row(_QUERIES[1])  # class_count (cypher=None)
        assert r["status"] == "insufficient_stores"

    def test_divergent_when_values_differ(self):
        from tools.cross_store_validation import _QUERIES, _compare_row
        g_small = _populated_graph(3)
        g_big = _populated_graph(100)

        def fake_get_graph(source):
            return (g_small if source == "merge" else g_big), "loaded"

        with patch("tools.sparql_local._get_graph", side_effect=fake_get_graph), \
             patch("tools.cross_store_validation.os.path.exists", return_value=True):
            r = _compare_row(_QUERIES[0])
        assert r["status"] == "divergent"
        assert r["max_diff_pct"] is not None and r["max_diff_pct"] > 5


class TestRunCrossStoreValidation:
    def test_summary_fields(self):
        from tools.cross_store_validation import run_cross_store_validation
        g = _populated_graph(5)
        with patch("tools.sparql_local._get_graph", return_value=(g, "merged")), \
             patch("tools.cross_store_validation.os.path.exists", return_value=True), \
             patch("tools.cross_store_validation._run_cypher_count", return_value=None):
            r = run_cross_store_validation()
        s = r["summary"]
        assert s["total_queries"] == 5
        assert s["identical"] + s["near_identical"] + s["divergent"] + s["insufficient_stores"] == 5


class TestVerifyCrossStoreParityTool:
    def test_mcp_tool_returns_json(self, tmp_path):
        from tools.cross_store_validation import verify_cross_store_parity
        g = _populated_graph(5)
        with patch("tools.sparql_local._get_graph", return_value=(g, "merged")), \
             patch("tools.cross_store_validation.os.path.exists", return_value=True), \
             patch("tools.cross_store_validation.GENERATED_REPORTS_DIR", str(tmp_path)), \
             patch("tools.cross_store_validation._run_cypher_count", return_value=None):
            raw = verify_cross_store_parity()
        data = json.loads(raw)
        assert data["success"] is True
        assert "summary" in data


class TestRunCypherCount:
    def test_returns_none_when_no_env(self):
        from tools.cross_store_validation import _run_cypher_count
        with patch("tools.cross_store_validation.NEO4J_URI", ""), \
             patch("tools.cross_store_validation.NEO4J_USER", ""), \
             patch("tools.cross_store_validation.NEO4J_PASSWORD", ""):
            assert _run_cypher_count("MATCH (n) RETURN count(n) AS n") is None
