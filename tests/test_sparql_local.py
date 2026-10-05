"""Tests for tools/sparql_local.py — local SPARQL execution via rdflib."""

import json
from unittest.mock import patch

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from domain.tbox_utils import _new_graph
from tools.sparql_local import _get_graph, sparql_local, sparql_local_reload


def _build_test_graph() -> Graph:
    """Build a small rdflib Graph for testing queries."""
    g = _new_graph()
    DOMAIN_NS = Namespace("http://example.org/steel#")
    INST = Namespace("http://example.org/steel/instance#")
    g.bind("steel", DOMAIN_NS)
    g.bind("inst", INST)
    g.bind("owl", OWL)

    g.add((DOMAIN_NS.EquipmentMaster, RDF.type, OWL.Class))
    g.add((DOMAIN_NS.EquipmentMaster, RDFS.label, Literal("Equipment Master", lang="en")))
    g.add((INST.EQ001, RDF.type, DOMAIN_NS.EquipmentMaster))
    g.add((INST.EQ001, DOMAIN_NS.equipmentID, Literal("EQ001")))
    g.add((INST.EQ002, RDF.type, DOMAIN_NS.EquipmentMaster))
    g.add((INST.EQ002, DOMAIN_NS.equipmentID, Literal("EQ002")))
    return g


@pytest.fixture
def mock_graph():
    """Patch load_graph to return a pre-built test graph."""
    g = _build_test_graph()
    with patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
        yield g


class TestGetGraph:
    """_get_graph: graph loading with cache."""

    @patch("domain.tbox_utils.load_graph")
    @patch("tools.sparql_local.os.path.exists", return_value=True)
    def test_inferred_source(self, mock_exists, mock_load):
        g = _build_test_graph()
        mock_load.return_value = (g, 0)
        result_g, msg = _get_graph("inferred")
        assert len(result_g) > 0
        assert "all_inferred.ttl" in msg
        mock_load.assert_called_once_with(use_inferred=True)

    @patch("domain.tbox_utils.load_graph")
    def test_merge_source(self, mock_load):
        g = _build_test_graph()
        mock_load.return_value = (g, 0)
        result_g, msg = _get_graph("merge")
        assert len(result_g) > 0
        assert "병합" in msg
        mock_load.assert_called_once_with(use_inferred=False)


class TestSparqlLocal:
    """sparql_local: execute SPARQL against local graph."""

    def test_select_query(self, mock_graph):
        query = """
        SELECT ?s WHERE {
            ?s a <http://www.w3.org/2002/07/owl#Class> .
        }
        """
        result = json.loads(sparql_local(query, source="merge"))
        assert result["success"] is True
        assert result["count"] >= 1
        assert "vars" in result
        assert "s" in result["vars"]
        assert len(result["results"]) >= 1

    def test_ask_query(self, mock_graph):
        query = """
        ASK {
            ?s a <http://www.w3.org/2002/07/owl#Class> .
        }
        """
        result = json.loads(sparql_local(query, source="merge"))
        assert result["success"] is True
        assert result["result"] is True

    def test_ask_query_false(self, mock_graph):
        query = """
        ASK {
            <http://example.org/nonexistent> a <http://www.w3.org/2002/07/owl#Class> .
        }
        """
        result = json.loads(sparql_local(query, source="merge"))
        assert result["success"] is True
        assert result["result"] is False

    def test_construct_query(self, mock_graph):
        query = """
        CONSTRUCT {
            ?s a <http://www.w3.org/2002/07/owl#Class> .
        } WHERE {
            ?s a <http://www.w3.org/2002/07/owl#Class> .
        }
        """
        result = json.loads(sparql_local(query, source="merge"))
        assert result["success"] is True
        assert "turtle" in result
        assert result["triples"] >= 1

    def test_invalid_sparql_returns_error(self, mock_graph):
        result = json.loads(sparql_local("NOT VALID SPARQL!!!", source="merge"))
        assert result["success"] is False
        assert "error" in result

    def test_select_with_count(self, mock_graph):
        query = """
        SELECT (COUNT(?s) AS ?cnt) WHERE {
            ?s a <http://www.w3.org/2002/07/owl#Class> .
        }
        """
        result = json.loads(sparql_local(query, source="merge"))
        assert result["success"] is True
        assert result["count"] == 1
        assert int(result["results"][0]["cnt"]) >= 1

    def test_query_time_included(self, mock_graph):
        query = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"
        result = json.loads(sparql_local(query, source="merge"))
        assert result["success"] is True
        assert "query_time_seconds" in result
        assert isinstance(result["query_time_seconds"], float)


class TestSparqlLocalReload:
    """sparql_local_reload: cache invalidation."""

    @patch("domain.tbox_utils.load_graph")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    def test_reload_invalidates_and_reloads(self, mock_invalidate, mock_load):
        g = _build_test_graph()
        mock_load.return_value = (g, 0)
        result = json.loads(sparql_local_reload(source="merge"))
        assert result["success"] is True
        assert "리로드" in result["message"]
        mock_invalidate.assert_called_once()
        mock_load.assert_called_once_with(use_inferred=False)
