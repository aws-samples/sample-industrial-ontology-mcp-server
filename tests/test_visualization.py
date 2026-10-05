"""Tests for tools/visualization.py — T-Box interactive HTML visualization."""

import json
import os
from unittest.mock import patch

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from domain.namespaces import DOMAIN_NS_OBJ as DOMAIN_NS
from domain.namespaces import NS_PREFIX
from domain.tbox_utils import _new_graph
from tools.visualization import (
    _build_html,
    _extract_tbox_data,
    _get_comment,
    _get_label,
    _short_uri,
    visualize_tbox,
)


def _build_test_tbox() -> Graph:
    """Build a minimal T-Box graph for visualization tests."""
    g = _new_graph()
    g.bind(NS_PREFIX, DOMAIN_NS)

    # Classes
    g.add((DOMAIN_NS.EquipmentMaster, RDF.type, OWL.Class))
    g.add((DOMAIN_NS.EquipmentMaster, RDFS.label, Literal("Equipment Master", lang="en")))
    g.add((DOMAIN_NS.EquipmentMaster, RDFS.label, Literal("설비 마스터", lang="ko")))
    g.add((DOMAIN_NS.EquipmentMaster, RDFS.comment, Literal("설비 정보", lang="ko")))

    g.add((DOMAIN_NS.EquipmentStatus, RDF.type, OWL.Class))
    g.add((DOMAIN_NS.EquipmentStatus, RDFS.label, Literal("Equipment Status", lang="en")))
    g.add((DOMAIN_NS.EquipmentStatus, RDFS.label, Literal("설비 상태", lang="ko")))

    # ObjectProperty
    g.add((DOMAIN_NS.hasEquipmentStatus, RDF.type, OWL.ObjectProperty))
    g.add((DOMAIN_NS.hasEquipmentStatus, RDFS.domain, DOMAIN_NS.EquipmentMaster))
    g.add((DOMAIN_NS.hasEquipmentStatus, RDFS.range, DOMAIN_NS.EquipmentStatus))
    g.add((DOMAIN_NS.hasEquipmentStatus, RDFS.label, Literal("설비 상태 관계", lang="ko")))

    # DatatypeProperty
    g.add((DOMAIN_NS.equipmentID, RDF.type, OWL.DatatypeProperty))
    g.add((DOMAIN_NS.equipmentID, RDFS.domain, DOMAIN_NS.EquipmentMaster))
    g.add((DOMAIN_NS.equipmentID, RDFS.range, XSD.string))
    g.add((DOMAIN_NS.equipmentID, RDFS.label, Literal("설비 ID", lang="ko")))

    return g


class TestShortUri:
    """_short_uri: URI abbreviation."""

    def test_xsd_uri(self):
        result = _short_uri("http://www.w3.org/2001/XMLSchema#string")
        assert result == "xsd:string"

    def test_owl_uri(self):
        result = _short_uri("http://www.w3.org/2002/07/owl#Class")
        assert result == "owl:Class"


class TestGetLabel:
    """_get_label: label extraction with language fallback."""

    def test_korean_label(self):
        g = _build_test_tbox()

        label = _get_label(g, DOMAIN_NS.EquipmentMaster, "ko")
        assert label == "설비 마스터"

    def test_english_fallback(self):
        g = _build_test_tbox()

        # EquipmentStatus has no French label, should fall back to English
        label = _get_label(g, DOMAIN_NS.EquipmentStatus, "fr")
        assert label == "Equipment Status"

    def test_missing_label_returns_local_name(self):
        g = _new_graph()
        uri = URIRef("http://example.org/test#NoLabel")
        label = _get_label(g, uri, "ko")
        assert label == "NoLabel"


class TestGetComment:
    """_get_comment: comment extraction."""

    def test_korean_comment(self):
        g = _build_test_tbox()

        comment = _get_comment(g, DOMAIN_NS.EquipmentMaster, "ko")
        assert comment == "설비 정보"

    def test_missing_comment_returns_empty(self):
        g = _new_graph()
        uri = URIRef("http://example.org/test#NoComment")
        comment = _get_comment(g, uri, "ko")
        assert comment == ""


class TestExtractTboxData:
    """_extract_tbox_data: data extraction from T-Box graph."""

    def test_class_extraction(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        assert data["stats"]["class_count"] == 2
        class_ids = [c["id"] for c in data["classes"]]
        assert "EquipmentMaster" in class_ids
        assert "EquipmentStatus" in class_ids

    def test_object_property_extraction(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        assert data["stats"]["object_property_count"] == 1
        op = data["object_properties"][0]
        assert op["id"] == "hasEquipmentStatus"
        assert op["domain"] == "EquipmentMaster"
        assert op["range"] == "EquipmentStatus"

    def test_datatype_property_extraction(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        assert data["stats"]["datatype_property_count"] == 1
        eq_cls = next(c for c in data["classes"] if c["id"] == "EquipmentMaster")
        dp_names = [dp["name"] for dp in eq_cls["datatype_properties"]]
        assert "equipmentID" in dp_names

    def test_outgoing_incoming_edges(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        eq_cls = next(c for c in data["classes"] if c["id"] == "EquipmentMaster")
        assert len(eq_cls["outgoing"]) == 1
        assert eq_cls["outgoing"][0]["target"] == "EquipmentStatus"
        status_cls = next(c for c in data["classes"] if c["id"] == "EquipmentStatus")
        assert len(status_cls["incoming"]) == 1


class TestBuildHtml:
    """_build_html: HTML rendering."""

    def test_html_contains_vis_js(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        html = _build_html(data)
        assert "vis-network" in html
        assert "vis.Network" in html
        assert "vis.DataSet" in html

    def test_html_contains_class_names(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        html = _build_html(data)
        assert "EquipmentMaster" in html
        assert "EquipmentStatus" in html

    def test_html_three_panel_layout(self):
        g = _build_test_tbox()
        data = _extract_tbox_data(g)
        html = _build_html(data)
        assert "sidebar-left" in html
        assert "sidebar-right" in html
        assert 'id="network"' in html


class TestVisualizeTbox:
    """visualize_tbox: full tool execution."""

    @patch("tools.visualization.webbrowser.open")
    def test_happy_path(self, mock_browser, sample_tbox_ttl, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(sample_tbox_ttl)
        with (
            patch("tools.visualization.GENERATED_TBOX_DIR", str(tmp_path)),
            patch("config.GENERATED_DIR", str(tmp_path)),
        ):
            result = json.loads(
                visualize_tbox(tbox_path=tbox_file.name, open_report=False)
            )
        assert result["success"] is True
        assert "stats" in result
        assert result["stats"]["class_count"] == 2
        assert os.path.exists(result["path"])
        mock_browser.assert_not_called()

    def test_missing_tbox_file(self, tmp_path):
        with patch("tools.visualization.GENERATED_TBOX_DIR", str(tmp_path)):
            result = json.loads(
                visualize_tbox(tbox_path="missing.ttl", open_report=False)
            )
        assert "error" in result
        assert "hint" in result
