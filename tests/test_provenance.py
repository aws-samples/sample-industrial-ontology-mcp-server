"""Tests for tools/provenance.py — P2 PROV-O."""
from __future__ import annotations

from rdflib import URIRef
from rdflib.namespace import RDF

from domain.namespaces import PROV
from tools.provenance import (
    PROV_BASE,
    _make_activity_uri,
    _make_agent_uri,
    _make_rule_uri,
    build_abox_provenance,
    build_inference_provenance,
    write_abox_provenance,
    write_inference_provenance,
)


class TestUriHelpers:
    def test_activity_uri_contains_stage(self):
        u = _make_activity_uri("inference", "2026-04-18T10:20:30")
        assert "inference" in str(u)
        assert ":" not in str(u).split("act_")[1]  # colons replaced

    def test_agent_uri_contains_version(self):
        u = _make_agent_uri("owlrl", "7.1.4")
        assert "owlrl" in str(u) and "7.1.4" in str(u)

    def test_rule_uri_sanitizes(self):
        u = _make_rule_uri("eq/ref with space")
        assert "/" not in str(u).split("rule_")[1]
        assert " " not in str(u)


class TestBuildInferenceProvenance:
    def _summary(self, **kw):
        base = {
            "generated_at": "2026-04-18T00:00:00",
            "total_inferred": 100,
            "justified": 90,
            "unjustified": 10,
            "by_rule": {
                "subClassOf": {"count": 50, "sample": []},
                "inverseOf": {"count": 40, "sample": []},
                "unknown": {"count": 10, "sample": []},
            },
        }
        base.update(kw)
        return base

    def test_minimum_triples(self):
        g = build_inference_provenance(self._summary())
        # Activity, Agent, 3 rules, 1 summary, + properties → 상당히 많음
        assert len(g) > 10

    def test_activity_typed(self):
        g = build_inference_provenance(self._summary())
        activities = list(g.subjects(RDF.type, URIRef(f"{PROV}Activity")))
        assert len(activities) == 1

    def test_rule_entities_count(self):
        g = build_inference_provenance(self._summary())
        entities = list(g.subjects(RDF.type, URIRef(f"{PROV}Entity")))
        # 3 rules + 1 summary = 4
        assert len(entities) >= 4

    def test_rule_count_literal_present(self):
        g = build_inference_provenance(self._summary())
        rule_u = _make_rule_uri("subClassOf")
        counts = list(g.objects(rule_u, URIRef(f"{PROV_BASE}count")))
        assert len(counts) == 1
        assert int(counts[0].toPython()) == 50

    def test_wasAssociatedWith_agent(self):
        g = build_inference_provenance(self._summary(),
                                       tool_name="owlrl", tool_version="7.1.4")
        assoc = list(g.objects(None, URIRef(f"{PROV}wasAssociatedWith")))
        assert any("owlrl" in str(a) and "7.1.4" in str(a) for a in assoc)

    def test_used_inputs(self, tmp_path):
        tbox = tmp_path / "t.ttl"
        tbox.write_text("dummy")
        g = build_inference_provenance(self._summary(),
                                       input_ttl_paths=[str(tbox)])
        used = list(g.objects(None, URIRef(f"{PROV}used")))
        assert any(str(tbox) in str(u) for u in used)


class TestWriteInferenceProvenance:
    def test_writes_valid_ttl(self, tmp_path):
        from rdflib import Graph
        summary = {
            "generated_at": "2026-04-18T00:00:00",
            "total_inferred": 5,
            "justified": 5,
            "unjustified": 0,
            "by_rule": {"subClassOf": {"count": 5, "sample": []}},
        }
        out = tmp_path / "prov.ttl"
        n = write_inference_provenance(summary, str(out))
        assert n > 0
        assert out.exists()
        # Roundtrip parse
        g = Graph()
        g.parse(str(out), format="turtle")
        assert len(g) == n


class TestAboxProvenance:
    def test_class_entities_and_source(self, tmp_path):
        per_class = {
            "EquipmentMaster": {"instance_count": 50, "source_table": "Equipment"},
            "Alarm": {"instance_count": 1200},
        }
        g = build_abox_provenance(per_class, source_csv_dir=str(tmp_path))
        entities = list(g.subjects(RDF.type, URIRef(f"{PROV}Entity")))
        # 2 class entities + 1 CSV entity (Equipment)
        assert len(entities) >= 3

    def test_write_roundtrip(self, tmp_path):
        from rdflib import Graph
        per_class = {"Cls": {"instance_count": 10, "source_table": "t"}}
        out = tmp_path / "abox_prov.ttl"
        n = write_abox_provenance(per_class, str(out),
                                  source_csv_dir=str(tmp_path))
        assert n > 0 and out.exists()
        g = Graph()
        g.parse(str(out), format="turtle")
        assert len(g) == n
