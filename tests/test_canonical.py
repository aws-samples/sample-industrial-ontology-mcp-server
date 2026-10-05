"""Tests for tools/canonical.py — D."""
from __future__ import annotations

import json

from rdflib import BNode, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.canonical import (
    canonical_hash,
    canonical_nt_lines,
    canonicalize_file,
    compare_graphs,
)


def _basic_graph():
    g = _new_graph()
    ns = DOMAIN_NS
    g.add((URIRef(f"{ns}A"), RDF.type, OWL.Class))
    g.add((URIRef(f"{ns}B"), RDF.type, OWL.Class))
    g.add((URIRef(f"{ns}A"), RDFS.subClassOf, URIRef(f"{ns}B")))
    return g


def test_canonical_hash_deterministic():
    g = _basic_graph()
    h1 = canonical_hash(g)
    h2 = canonical_hash(g)
    assert h1 == h2


def test_canonical_lines_sorted():
    g = _basic_graph()
    lines = canonical_nt_lines(g)
    assert lines == sorted(lines)


def test_same_structure_different_bnode_ids_equal():
    """두 그래프가 bnode 이름만 다르고 구조 동일 → canonical hash 같음."""
    g1 = _new_graph()
    g2 = _new_graph()
    ns = DOMAIN_NS
    cls = URIRef(f"{ns}Cls")
    for g in (g1, g2):
        g.add((cls, RDF.type, OWL.Class))

    b1 = BNode("anon1")
    b2 = BNode("totally_different")
    g1.add((cls, RDFS.subClassOf, b1))
    g1.add((b1, RDF.type, OWL.Restriction))
    g2.add((cls, RDFS.subClassOf, b2))
    g2.add((b2, RDF.type, OWL.Restriction))

    assert canonical_hash(g1) == canonical_hash(g2)


def test_different_content_different_hash():
    g1 = _basic_graph()
    g2 = _basic_graph()
    g2.add((URIRef(f"{DOMAIN_NS}C"), RDF.type, OWL.Class))
    assert canonical_hash(g1) != canonical_hash(g2)


def test_canonicalize_file_roundtrip(tmp_path):
    g = _basic_graph()
    p = tmp_path / "g.ttl"
    g.serialize(destination=str(p), format="turtle")
    r = canonicalize_file(str(p))
    assert "sha256" in r
    assert r["triple_count"] == 3


def test_compare_graphs_identical(tmp_path):
    g = _basic_graph()
    pa = tmp_path / "a.ttl"
    pb = tmp_path / "b.ttl"
    g.serialize(destination=str(pa), format="turtle")
    g.serialize(destination=str(pb), format="turtle")
    r = compare_graphs(str(pa), str(pb))
    assert r["isomorphic"] is True
    assert r["hash_equal"] is True


def test_compare_graphs_different(tmp_path):
    g1 = _basic_graph()
    g2 = _basic_graph()
    g2.add((URIRef(f"{DOMAIN_NS}Extra"), RDF.type, OWL.Class))
    pa = tmp_path / "a.ttl"
    pb = tmp_path / "b.ttl"
    g1.serialize(destination=str(pa), format="turtle")
    g2.serialize(destination=str(pb), format="turtle")
    r = compare_graphs(str(pa), str(pb))
    assert r["isomorphic"] is False
    assert r["hash_equal"] is False
    assert r["only_in_b_count"] >= 1


def test_compare_missing_file(tmp_path):
    r = compare_graphs("/nonexistent/a.ttl", str(tmp_path / "b.ttl"))
    assert "error" in r


def _isolate_generated(monkeypatch, tmp_path):
    """tmp_path 를 data/generated 경계로, 사이드카 출력도 tmp 로 돌린다."""
    import tools.canonical as canonical

    monkeypatch.setattr(canonical, "GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr(canonical, "GENERATED_REPORTS_DIR", str(tmp_path / "reports"))
    return canonical


def test_mcp_tool_canonicalize(tmp_path, monkeypatch):
    canonicalize_graph = _isolate_generated(monkeypatch, tmp_path).canonicalize_graph
    g = _basic_graph()
    p = tmp_path / "g.ttl"
    g.serialize(destination=str(p), format="turtle")
    raw = canonicalize_graph(str(p))
    data = json.loads(raw)
    assert data["success"] is True
    assert "sha256" in data


def test_mcp_tool_compare(tmp_path, monkeypatch):
    compare_canonical = _isolate_generated(monkeypatch, tmp_path).compare_canonical
    g = _basic_graph()
    pa = tmp_path / "a.ttl"
    pb = tmp_path / "b.ttl"
    g.serialize(destination=str(pa), format="turtle")
    g.serialize(destination=str(pb), format="turtle")
    raw = compare_canonical(str(pa), str(pb))
    data = json.loads(raw)
    assert data["success"] is True
    assert data["hash_equal"] is True
