"""Tests for T1 — OQuaRE Transferability dynamic calculation.

Validates `_compute_transferability(g)` helper which replaces the
previously hardcoded `Transferability: 3.0` placeholder with a dynamic
score based on `owl:imports` + standard vocabulary reuse.

Scoring formula (1~5 scale):
    base = 1.0
    + 0.5 if owl:Ontology IRI present
    + min(2.0, imports_count * 0.5)
    + min(2.0, standard_vocab_count * 1.0)
    cap at 5.0
"""

import json

from rdflib import Graph

from tools.ontology_quality import (
    _compute_transferability,
    _oquare_characteristics,
)


def _parse(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def test_no_imports_returns_base_score():
    """Empty ontology with only owl:Ontology declaration → 1.5."""
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology .
    """
    g = _parse(ttl)
    assert _compute_transferability(g) == 1.5  # 1.0 + 0.5 (onto IRI)


def test_single_non_standard_import():
    """One non-standard owl:imports → 2.0."""
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology ;
        owl:imports <urn:foo:/> .
    """
    g = _parse(ttl)
    # 1.0 + 0.5 (onto IRI) + 0.5 (1 import) = 2.0
    assert _compute_transferability(g) == 2.0


def test_prov_import_counted_as_standard():
    """One PROV import → 3.0 (import bonus + standard vocab bonus)."""
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology ;
        owl:imports <http://www.w3.org/ns/prov> .
    """
    g = _parse(ttl)
    # 1.0 + 0.5 + 0.5 (1 import) + 1.0 (1 standard) = 3.0
    assert _compute_transferability(g) == 3.0


def test_multiple_standard_imports_cap_5():
    """3 standard imports → capped at 5.0."""
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology ;
        owl:imports <http://www.w3.org/ns/prov> ,
                    <http://xmlns.com/foaf/0.1/> ,
                    <https://spec.industrialontologies.org/ontology/core/Core/> .
    """
    g = _parse(ttl)
    # 1.0 + 0.5 + 1.5 (3 imports * 0.5) + 2.0 (cap on standard) = 5.0
    assert _compute_transferability(g) == 5.0


def test_no_ontology_iri_returns_1():
    """Graph with no owl:Ontology declaration → 1.0 (no bonus)."""
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :SomeClass a owl:Class .
    """
    g = _parse(ttl)
    assert _compute_transferability(g) == 1.0


def test_four_non_standard_imports_cap_3_5():
    """4 non-standard imports → 1.0 + 0.5 + 2.0 (cap) + 0 = 3.5."""
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology ;
        owl:imports <urn:foo:/a> ,
                    <urn:foo:/b> ,
                    <urn:foo:/c> ,
                    <urn:foo:/d> .
    """
    g = _parse(ttl)
    # 1.0 + 0.5 + 2.0 (cap: 4 * 0.5 = 2.0) + 0 = 3.5
    assert _compute_transferability(g) == 3.5


def test_oquare_characteristics_backward_compat_without_graph():
    """_oquare_characteristics(metrics) without g → 3.0 placeholder."""
    metrics = {
        "dit": {"value": 2},
        "noc_avg": {"value": 1.5},
        "rr": {"value": 0.3},
        "ar": {"value": 6},
        "annotation_completeness": {"value": 80},
        "axiom_richness": {"value": 3},
        "ir": {"value": 0.3},
        "cc": {"value": 0.5},
        "tan": {"value": 15},
    }
    characteristics, _sub = _oquare_characteristics(metrics)
    assert characteristics["Transferability"] == 3.0


def test_oquare_characteristics_uses_graph_when_provided():
    """_oquare_characteristics(metrics, g) → dynamic Transferability."""
    metrics = {
        "dit": {"value": 2},
        "noc_avg": {"value": 1.5},
        "rr": {"value": 0.3},
        "ar": {"value": 6},
        "annotation_completeness": {"value": 80},
        "axiom_richness": {"value": 3},
        "ir": {"value": 0.3},
        "cc": {"value": 0.5},
        "tan": {"value": 15},
    }
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology ;
        owl:imports <http://www.w3.org/ns/prov> .
    """
    g = _parse(ttl)
    characteristics, _sub = _oquare_characteristics(metrics, g=g)
    # PROV import: 1.0 + 0.5 + 0.5 + 1.0 = 3.0 (happens to equal placeholder)
    assert characteristics["Transferability"] == 3.0

    # Now test with a graph that gives a different (non-3.0) score
    ttl2 = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix : <urn:x:> .
    :Onto a owl:Ontology .
    """
    g2 = _parse(ttl2)
    characteristics2, _sub2 = _oquare_characteristics(metrics, g=g2)
    assert characteristics2["Transferability"] == 1.5


def test_oquare_integration_uses_transferability(monkeypatch, tmp_path):
    """evaluate_oquare loads T-Box graph and computes dynamic Transferability."""
    from tools import ontology_quality as oq

    # Build a T-Box with one standard import → Transferability should be 3.0
    # but through the dynamic helper, not the placeholder. We verify that
    # removing the import shifts the score.
    tbox_ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
    @prefix : <urn:test:> .
    :Onto a owl:Ontology ;
        owl:imports <http://www.w3.org/ns/prov> ,
                    <http://xmlns.com/foaf/0.1/> .
    :ClassA a owl:Class ;
        rdfs:label "A" ;
        rdfs:comment "class A" .
    :ClassB a owl:Class ;
        rdfs:subClassOf :ClassA ;
        rdfs:label "B" ;
        rdfs:comment "class B" .
    :prop a owl:ObjectProperty ;
        rdfs:domain :ClassA ;
        rdfs:range :ClassB ;
        rdfs:label "prop" .
    """
    tbox_file = tmp_path / "t_box.ttl"
    tbox_file.write_text(tbox_ttl)

    monkeypatch.setattr(oq, "TBOX_PATH", str(tbox_file))

    # Fake measure_tbox_metrics to return reasonable metrics
    fake_metrics_json = json.dumps({
        "success": True,
        "metrics": {
            "dit": {"value": 2},
            "noc_avg": {"value": 1.5},
            "rr": {"value": 0.3},
            "ar": {"value": 6},
            "annotation_completeness": {"value": 80},
            "axiom_richness": {"value": 3},
            "ir": {"value": 0.3},
            "cc": {"value": 0.5},
            "tan": {"value": 15},
        },
    })

    import tools.tbox_metrics as tm
    monkeypatch.setattr(tm, "measure_tbox_metrics", lambda: fake_metrics_json)

    result = json.loads(oq.evaluate_oquare())
    assert result["success"] is True
    # With 2 standard imports: 1.0 + 0.5 + 1.0 (2*0.5) + 2.0 (cap) = 4.5
    assert result["characteristics_1_to_5"]["Transferability"] == 4.5
