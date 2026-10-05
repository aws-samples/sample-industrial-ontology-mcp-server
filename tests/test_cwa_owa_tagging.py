"""Tests for tools/cwa_owa_tagging.py — #19."""
from __future__ import annotations

import json

import pytest
from rdflib import URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.cwa_owa_tagging import (
    ALLOWED_ORIGINS,
    summarize_origin_distribution,
    summarize_origin_sidecar,
    tag_graph_origin,
)
from tools.triple_confidence import PROVENANCE


def _triple_graph(origin_suffix="A"):
    g = _new_graph()
    s = URIRef(f"{DOMAIN_INST_NS}{origin_suffix}1")
    p = URIRef(f"{DOMAIN_NS}hasX")
    o = URIRef(f"{DOMAIN_INST_NS}{origin_suffix}2")
    g.add((s, p, o))
    return g


def test_tag_graph_origin_csv():
    src = _triple_graph("CSV")
    sidecar = tag_graph_origin(src, "csv_direct")
    provs = [str(x) for x in sidecar.objects(None, PROVENANCE)]
    assert provs == ["csv_direct"]


def test_tag_graph_multi_origin_accum():
    src1 = _triple_graph("A")
    src2 = _triple_graph("B")
    sc = tag_graph_origin(src1, "csv_direct")
    sc = tag_graph_origin(src2, "tacit_sme", sidecar=sc)
    provs = sorted(str(x) for x in sc.objects(None, PROVENANCE))
    assert provs == ["csv_direct", "tacit_sme"]


def test_invalid_origin_raises():
    src = _triple_graph("X")
    with pytest.raises(ValueError, match="Invalid origin"):
        tag_graph_origin(src, "not_valid")  # type: ignore[arg-type]


def test_allowed_origins_set():
    assert "csv_direct" in ALLOWED_ORIGINS
    assert "owl_rl_1hop" in ALLOWED_ORIGINS
    assert "tacit_sme" in ALLOWED_ORIGINS


def test_summarize_distribution_rollup(tmp_path):
    src1 = _triple_graph("A")
    src2 = _triple_graph("B")
    src3 = _triple_graph("C")
    sc = tag_graph_origin(src1, "csv_direct")
    sc = tag_graph_origin(src2, "tacit_sme", sidecar=sc)
    sc = tag_graph_origin(src3, "owl_rl_chain", sidecar=sc)
    path = tmp_path / "sc.ttl"
    sc.serialize(destination=str(path), format="turtle")

    r = summarize_origin_distribution(str(path))
    assert r["cwa_count"] == 1
    assert r["owa_count"] == 1
    assert r["inferred_count"] == 1
    assert r["total_classified"] == 3


def test_mcp_tool_success(tmp_path):
    src = _triple_graph("Q")
    sc = tag_graph_origin(src, "csv_direct")
    path = tmp_path / "s.ttl"
    sc.serialize(destination=str(path), format="turtle")
    raw = summarize_origin_sidecar(str(path))
    data = json.loads(raw)
    assert data["success"] is True
    assert data["cwa_count"] == 1


def test_mcp_tool_missing():
    raw = summarize_origin_sidecar("/nonexistent.ttl")
    data = json.loads(raw)
    assert data["success"] is False
