"""Tests for tools/topology.py — Q3."""
from __future__ import annotations

import json
from unittest.mock import patch

from rdflib import OWL, RDF, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph


def _build_test_graph(components: int = 1, size_per_comp: int = 10):
    """components 개의 독립 컴포넌트를 가진 A-Box 생성."""
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS
    cls = URIRef(f"{ns}Node")
    g.add((cls, RDF.type, OWL.Class))
    has_next = URIRef(f"{ns}hasNext")
    g.add((has_next, RDF.type, OWL.ObjectProperty))

    for c in range(components):
        prev = None
        for i in range(size_per_comp):
            u = URIRef(f"{inst}c{c}_n{i}")
            g.add((u, RDF.type, cls))
            if prev is not None:
                g.add((prev, has_next, u))
            prev = u
    return g


class TestBuildInstanceGraph:
    def test_nodes_and_edges_counted(self):
        from tools.topology import _build_instance_graph
        g = _build_test_graph(components=2, size_per_comp=5)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
            G, size = _build_instance_graph()
        # 5 nodes × 2 comps = 10 typed; 4 edges × 2 = 8 hasNext
        assert size["nodes"] == 10
        assert size["edges"] == 8


class TestComponentStats:
    def test_two_disjoint_components(self):
        from tools.topology import _build_instance_graph, _component_stats
        g = _build_test_graph(components=3, size_per_comp=4)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
            G, _ = _build_instance_graph()
        s = _component_stats(G)
        assert s["component_count"] == 3
        assert s["largest_component_size"] == 4


class TestDegreeStats:
    def test_top_nodes_present(self):
        from tools.topology import _build_instance_graph, _degree_stats
        g = _build_test_graph(components=1, size_per_comp=5)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
            G, _ = _build_instance_graph()
        s = _degree_stats(G, top_n=3)
        assert len(s["top_nodes"]) == 3
        assert s["percentiles"]["max"] >= 1


class TestDiameter:
    def test_chain_diameter(self):
        from tools.topology import _approximate_diameter, _build_instance_graph
        g = _build_test_graph(components=1, size_per_comp=10)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
            G, _ = _build_instance_graph()
        r = _approximate_diameter(G, n_sources=5)
        # 10-chain의 최장 shortest path = 9
        assert r["approx_diameter"] >= 5


class TestAnalyzeTopology:
    def test_signals_disconnected(self):
        from tools.topology import analyze_topology
        g = _build_test_graph(components=3, size_per_comp=4)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
            r = analyze_topology(heavy=False)
        assert r["components"]["component_count"] == 3
        codes = {s["code"] for s in r["signals"]}
        assert "disconnected" in codes

    def test_heavy_includes_betweenness(self):
        from tools.topology import analyze_topology
        g = _build_test_graph(components=1, size_per_comp=6)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
            r = analyze_topology(heavy=True)
        assert "betweenness" in r


class TestMcpTool:
    def test_tool_returns_json(self, tmp_path):
        from tools.topology import analyze_graph_topology
        g = _build_test_graph(components=1, size_per_comp=5)
        with patch("domain.tbox_utils.load_graph", return_value=(g, 0)), \
             patch("tools.topology.GENERATED_REPORTS_DIR", str(tmp_path)):
            raw = analyze_graph_topology(heavy=False)
        data = json.loads(raw)
        assert data["success"] is True
        assert "components" in data
