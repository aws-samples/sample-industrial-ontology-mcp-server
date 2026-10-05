"""Tests for tools/foops_fair.py — R1."""
from __future__ import annotations

import json

from rdflib import Literal, URIRef
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS

from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.foops_fair import (
    _find_ontology_iri,
    evaluate_fair,
)


def _build_minimal_tbox(tmp_path, with_metadata: bool = False,
                        persistent_iri: bool = False,
                        with_license: bool = False,
                        with_version: bool = False,
                        with_imports: bool = False):
    g = _new_graph()
    iri = URIRef("https://w3id.org/ex/ontology" if persistent_iri
                 else "http://example.org/ontology")
    g.add((iri, RDF.type, OWL.Ontology))
    if with_metadata:
        g.add((iri, RDFS.label, Literal("Example Ontology", lang="en")))
        g.add((iri, DCTERMS.description, Literal("Description here.")))
        g.add((iri, DCTERMS.creator, Literal("Test Author")))
    if with_license:
        g.add((iri, DCTERMS.license,
               URIRef("https://spdx.org/licenses/MIT-0.html")))
    if with_version:
        g.add((iri, OWL.versionInfo, Literal("1.0.0")))
    if with_imports:
        g.add((iri, OWL.imports,
               URIRef("http://www.w3.org/ns/prov-o")))
        g.add((iri, OWL.imports,
               URIRef("https://spec.industrialontologies.org/ontology/core/Core/")))
    g.add((URIRef(f"{DOMAIN_NS}X"), RDF.type, OWL.Class))
    path = tmp_path / "tbox.ttl"
    g.serialize(destination=str(path), format="turtle")
    return str(path), iri


def test_find_ontology_iri():
    g = _new_graph()
    iri = URIRef("http://example.org/o")
    g.add((iri, RDF.type, OWL.Ontology))
    assert _find_ontology_iri(g) == iri


def test_findable_missing_metadata(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path, with_metadata=False)
    r = evaluate_fair(path)
    f = r["axis_scores"]["Findable"]
    # F2_title, F2_description, F4_keywords 등 여러 실패
    assert f["passed"] < f["total"]


def test_findable_persistent_iri_helps(tmp_path):
    p1, _ = _build_minimal_tbox(tmp_path, persistent_iri=False,
                                 with_metadata=True)
    p2_dir = tmp_path / "p2"
    p2_dir.mkdir()
    p2, _ = _build_minimal_tbox(p2_dir, persistent_iri=True,
                                 with_metadata=True)
    r1 = evaluate_fair(p1)
    r2 = evaluate_fair(p2)
    # persistent IRI 사용한 쪽 점수가 높거나 같음
    assert r2["axis_scores"]["Findable"]["score"] >= r1["axis_scores"]["Findable"]["score"]


def test_accessible_http_protocol(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path)
    r = evaluate_fair(path)
    # http:// 스킴이므로 A1_protocol pass
    checks = r["axis_scores"]["Accessible"]["checks"]
    assert any(c["check"] == "A1_protocol" and c["pass"] for c in checks)


def test_interoperable_no_imports(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path, with_imports=False)
    r = evaluate_fair(path)
    i = r["axis_scores"]["Interoperable"]
    # I2_imports 실패 예상
    fails = [c for c in i["checks"] if not c["pass"]]
    assert any(c["check"] == "I2_imports" for c in fails)


def test_interoperable_with_imports(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path, with_imports=True)
    r = evaluate_fair(path)
    i = r["axis_scores"]["Interoperable"]
    passes = [c for c in i["checks"] if c["pass"]]
    assert any(c["check"] == "I2_imports" for c in passes)


def test_reusable_no_license(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path, with_license=False)
    r = evaluate_fair(path)
    fails = [c for c in r["axis_scores"]["Reusable"]["checks"] if not c["pass"]]
    assert any(c["check"] == "R1_1_license" for c in fails)


def test_reusable_full(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path, with_metadata=True,
                                   with_license=True, with_version=True,
                                   with_imports=True)
    r = evaluate_fair(path)
    reusable = r["axis_scores"]["Reusable"]
    # license + version + provenance(creator in metadata) + community standards
    assert reusable["passed"] == reusable["total"]


def test_overall_grade_poor_ontology(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path)  # 아무 메타데이터 없음
    r = evaluate_fair(path)
    assert r["grade"] in ("C", "D")


def test_overall_grade_well_annotated(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path, with_metadata=True,
                                   persistent_iri=True, with_license=True,
                                   with_version=True, with_imports=True)
    r = evaluate_fair(path)
    assert r["grade"] in ("A", "B")


def test_missing_file_returns_error(tmp_path):
    r = evaluate_fair(str(tmp_path / "nope.ttl"))
    assert "error" in r


def test_mcp_tool(tmp_path, monkeypatch):
    import tools.foops_fair as foops_fair

    path, _ = _build_minimal_tbox(tmp_path, with_metadata=True, with_license=True)
    # MCP 도구는 data/generated/tbox 아래 파일명만 받는다.
    monkeypatch.setattr(foops_fair, "GENERATED_TBOX_DIR", str(tmp_path))
    monkeypatch.setattr(foops_fair, "GENERATED_REPORTS_DIR", str(tmp_path / "reports"))
    raw = foops_fair.evaluate_fair_score("tbox.ttl")
    data = json.loads(raw)
    assert data["success"] is True
    assert "overall_score" in data
    assert data["grade"] in ("A", "B", "C", "D")


def test_citation_in_report(tmp_path):
    path, _ = _build_minimal_tbox(tmp_path)
    r = evaluate_fair(path)
    assert "Garijo" in r["citation"]
    assert "Wilkinson" in r["citation"]
