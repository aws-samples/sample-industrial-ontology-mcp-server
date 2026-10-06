"""Tests for tools/provenance.py cell-level — #9."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from rdflib import URIRef

from domain.tbox_utils import _new_graph
from tools.provenance import (
    annotate_instance_cell_provenance,
    make_cell_provenance_uri,
    trace_instance_to_cell,
    trace_provenance,
)


def test_make_cell_uri_stable():
    u1 = make_cell_provenance_uri("EqM", 5, "equipmentID")
    u2 = make_cell_provenance_uri("EqM", 5, "equipmentID")
    assert u1 == u2
    assert "row=5" in str(u1)
    assert "col=equipmentID" in str(u1)


def test_make_cell_uri_safe_column():
    u = make_cell_provenance_uri("EqM", 1, "space col/slash")
    assert " " not in str(u)
    assert "/" not in str(u).split("//")[-1]


def test_annotate_and_trace_roundtrip():
    g = _new_graph()
    iuri = URIRef("http://ex.org/i/X")
    added = annotate_instance_cell_provenance(
        g, iuri, "TableX", 10,
        cell_map={"pId": "ID_COL", "pName": "NAME_COL"},
    )
    assert added > 0
    result = trace_instance_to_cell(g, iuri)
    assert result["csv_table"] == "TableX"
    assert result["row_number"] == 10
    cols = {c["column"] for c in result["cells"]}
    assert cols == {"ID_COL", "NAME_COL"}


def test_annotate_idempotent_row():
    g = _new_graph()
    iuri = URIRef("http://ex.org/i/A")
    annotate_instance_cell_provenance(g, iuri, "Tab", 1)
    triples_1 = len(g)
    annotate_instance_cell_provenance(g, iuri, "Tab", 1)  # 같은 row 재호출
    assert len(g) == triples_1  # row 트리플은 중복 방지


def test_trace_missing_provenance():
    g = _new_graph()
    iuri = URIRef("http://ex.org/i/nothing")
    result = trace_instance_to_cell(g, iuri)
    assert result["csv_table"] is None


@pytest.fixture
def prov_dir(tmp_path, monkeypatch):
    """도구의 data/generated 경계를 tmp_path 로 옮긴다.

    ``trace_provenance`` 는 prov_path 를 GENERATED_DIR 아래 경로로만 받으므로
    provenance TTL 을 이 디렉터리 안에 쓴다.
    """
    import tools.provenance as provenance

    monkeypatch.setattr(provenance, "GENERATED_DIR", str(tmp_path))
    return tmp_path


def test_mcp_tool_missing_file(prov_dir):
    # 경계 안의 없는 파일이어야 경계 거부가 아니라 '찾지 못함' 분기를 탄다.
    with patch("tools.provenance.ABOX_PATH",
               str(prov_dir / "nope.ttl")):
        raw = trace_provenance(
            "http://ex.org/i/X",
            prov_path=str(prov_dir / "nope2.ttl"),
        )
        data = json.loads(raw)
        assert data["success"] is False
        assert "찾지 못했습니다" in data["error"]


def test_mcp_tool_full_flow(prov_dir):
    # Provenance TTL 생성
    g = _new_graph()
    iuri = URIRef("http://ex.org/i/Y")
    annotate_instance_cell_provenance(
        g, iuri, "Equipment", 3,
        cell_map={"eqID": "EQ_ID"},
    )
    prov_ttl = prov_dir / "prov.ttl"
    g.serialize(destination=str(prov_ttl), format="turtle")

    raw = trace_provenance(str(iuri), prov_path=str(prov_ttl))
    data = json.loads(raw)
    assert data["success"] is True
    assert data["csv_table"] == "Equipment"
    assert data["row_number"] == 3
    assert data["cells"][0]["column"] == "EQ_ID"
