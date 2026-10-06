"""I3 — Tests for classify_inference_triples (meaningful/trivial/suspicious)."""
from __future__ import annotations

import json

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_NS
from tools.inference_classify import _classify_triple, classify_inference_triples

DOMAIN = str(DOMAIN_NS)
FOAF = Namespace("http://xmlns.com/foaf/0.1/")


def _cls(local: str) -> URIRef:
    return URIRef(DOMAIN + local)


def _inst(local: str) -> URIRef:
    return URIRef(DOMAIN + local)


@pytest.fixture
def inferred_dir(tmp_path, monkeypatch):
    """도구의 inferred 경계 디렉터리를 tmp_path 로 옮긴다.

    ``classify_inference_triples`` 는 inferred_path 를 GENERATED_INFERRED_DIR 바로
    아래 파일명으로만 받으므로, 픽스처를 이 디렉터리에 쓰고 파일명만 넘긴다.
    """
    import tools.inference_classify as inference_classify

    monkeypatch.setattr(inference_classify, "GENERATED_INFERRED_DIR", str(tmp_path))
    return tmp_path


def test_named_individual_is_trivial():
    s = _inst("EQ001")
    cat = _classify_triple(s, RDF.type, OWL.NamedIndividual)
    assert cat == "trivial"


def test_owl_thing_type_is_trivial():
    s = _inst("EQ001")
    cat = _classify_triple(s, RDF.type, OWL.Thing)
    assert cat == "trivial"


def test_domain_class_type_is_meaningful():
    s = _inst("EQ001")
    o = _cls("Equipment")
    cat = _classify_triple(s, RDF.type, o)
    assert cat == "meaningful"


def test_cross_namespace_is_suspicious():
    # subject is in domain NS; predicate is from foreign (FOAF) NS -> suspicious
    s = _inst("EQ001")
    cat = _classify_triple(s, FOAF.name, Literal("X"))
    assert cat == "suspicious"


def test_classify_inference_triples_totals(inferred_dir):
    # Build a graph with 3 meaningful, 2 trivial, 1 suspicious
    g = Graph()

    # meaningful: domain class rdf:type
    g.add((_inst("EQ001"), RDF.type, _cls("Equipment")))
    g.add((_inst("EQ002"), RDF.type, _cls("Equipment")))
    # meaningful: domain OP link (predicate in domain NS, object in domain NS)
    g.add((_inst("EQ001"), _cls("followedBy"), _inst("EQ002")))

    # trivial: owl:NamedIndividual + owl:Thing
    g.add((_inst("EQ001"), RDF.type, OWL.NamedIndividual))
    g.add((_inst("EQ002"), RDF.type, OWL.Thing))

    # suspicious: domain subject + foreign predicate
    g.add((_inst("EQ001"), FOAF.name, Literal("BlastFurnace")))

    tmp = inferred_dir / "inferred.ttl"
    tmp.write_text(g.serialize(format="turtle"), encoding="utf-8")

    resp = classify_inference_triples(inferred_path=tmp.name)
    data = json.loads(resp)

    assert data["success"] is True
    assert data["total_inferred"] == 6
    assert data["meaningful_count"] == 3
    assert data["trivial_count"] == 2
    assert data["suspicious_count"] == 1


def test_empty_graph_returns_zero(inferred_dir):
    tmp = inferred_dir / "empty.ttl"
    tmp.write_text("", encoding="utf-8")

    resp = classify_inference_triples(inferred_path=tmp.name)
    data = json.loads(resp)

    assert data["success"] is True
    assert data["total_inferred"] == 0
    assert data["meaningful_count"] == 0
    assert data["trivial_count"] == 0
    assert data["suspicious_count"] == 0


def test_subclassof_owl_thing_is_trivial():
    """reasoner-generated (domain subClassOf owl:Thing) is schema bloat."""
    cat = _classify_triple(_cls("Equipment"), RDFS.subClassOf, OWL.Thing)
    assert cat == "trivial"
    cat2 = _classify_triple(_cls("Equipment"), RDFS.subClassOf, RDFS.Resource)
    assert cat2 == "trivial"
    # Real domain subclass relation must remain meaningful.
    cat3 = _classify_triple(
        _cls("BlastFurnace"), RDFS.subClassOf, _cls("Equipment"),
    )
    assert cat3 == "meaningful"


def test_mcp_tool_returns_success_json(inferred_dir):
    """Missing-file invocation returns a success=true JSON with zero counts."""
    missing = inferred_dir / "does_not_exist.ttl"
    resp = classify_inference_triples(inferred_path=missing.name)
    data = json.loads(resp)
    assert data["success"] is True
    assert data["total_inferred"] == 0
    assert "note" in data
    assert "samples" in data
    assert set(data["samples"].keys()) == {"meaningful", "trivial", "suspicious"}


def test_bnode_subject_is_trivial():
    """OWL restriction body 등 BNode subject 는 trivial 로 분류."""
    bn = BNode()
    # Restriction body triple — 어떤 predicate/object 든 BNode subject 면 trivial
    assert _classify_triple(bn, OWL.onProperty, _cls("hasEquipment")) == "trivial"
    assert _classify_triple(bn, RDF.type, OWL.Restriction) == "trivial"
    assert _classify_triple(bn, OWL.someValuesFrom, _cls("Equipment")) == "trivial"


def test_domain_extension_ontoclean_is_meaningful():
    """T2 OntoClean annotation (steel-ontoclean:*) 은 도메인 확장 NS — meaningful."""
    from rdflib import Literal as L
    oc_ns = DOMAIN_NS.rstrip("#") + "-ontoclean#"
    s = _cls("DimensionalData")
    p = URIRef(oc_ns + "identity")
    o = L("+I")
    assert _classify_triple(s, p, o) == "meaningful"


def test_domain_extension_autolabel_flag_is_meaningful():
    """T4 autoCreated flag 도 확장 NS — meaningful."""
    oc_ns = DOMAIN_NS.rstrip("#") + "-ontoclean#"
    s = _cls("QualityManagement")
    p = URIRef(oc_ns + "autoCreated")
    assert _classify_triple(s, p, Literal(True)) == "meaningful"


def test_dcterms_predicates_are_meaningful():
    """dcterms:isPartOf (T4 module membership) 등 SKOS/DCTERMS 는 meaningful."""
    s = _cls("realTimeDataForProcess")
    p = URIRef("http://purl.org/dc/terms/isPartOf")
    o = _cls("module-production")
    assert _classify_triple(s, p, o) == "meaningful"


def test_skos_predicates_are_meaningful():
    """Step 24 abstract category annotation — skos:scopeNote, skos:notation."""
    s = _cls("EnvironmentalMonitoring")
    p = URIRef("http://www.w3.org/2004/02/skos/core#scopeNote")
    o = Literal("abstract category", lang="ko")
    assert _classify_triple(s, p, o) == "meaningful"

    p2 = URIRef("http://www.w3.org/2004/02/skos/core#notation")
    assert _classify_triple(s, p2, Literal("category")) == "meaningful"


def test_prov_predicates_are_meaningful():
    """prov-o (A4 row provenance) 는 확장 NS — meaningful.

    Note: object 는 prov:// URI (외부) 이지만 predicate 가 확장 NS 이므로
    triple 전체는 meaningful. 이 케이스는 object 기반 suspicious 판정을
    벗어남 (spec: extension vocab predicate 이면 object NS 무관).
    """
    s = _cls("EquipmentMaster_EQ001")
    p = URIRef("http://www.w3.org/ns/prov#wasDerivedFrom")
    # object 는 literal 로 단순화 — prov:// URI 는 외부 NS 라 별도 처리 필요
    o = Literal("Equipment_Master#row=1")
    assert _classify_triple(s, p, o) == "meaningful"


def test_iof_predicates_are_meaningful():
    """IOF (Industrial Ontology Foundry) upper ontology — meaningful."""
    s = _cls("EquipmentMaster")
    p = URIRef("https://spec.industrialontologies.org/ontology/core/Core/hasPart")
    o = _cls("Component")
    assert _classify_triple(s, p, o) == "meaningful"


def test_truly_foreign_predicate_still_suspicious():
    """완전히 무관한 외부 NS (FOAF 등) 는 여전히 suspicious."""
    s = _cls("EquipmentMaster_EQ001")
    p = URIRef("http://xmlns.com/foaf/0.1/name")
    assert _classify_triple(s, p, Literal("EQ001")) == "suspicious"


def test_bnode_does_not_leak_into_suspicious_or_meaningful(inferred_dir):
    """그래프에 BNode subject 가 있어도 suspicious/meaningful 집계에 포함 안 됨."""
    g = Graph()
    # Meaningful domain triple
    g.add((_inst("EQ001"), RDF.type, _cls("Equipment")))
    # BNode restriction body (reasoner-style)
    restriction = BNode()
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, _cls("hasAlarm")))
    g.add((restriction, OWL.someValuesFrom, _cls("Alarm")))

    tmp = inferred_dir / "inferred.ttl"
    tmp.write_text(g.serialize(format="turtle"), encoding="utf-8")

    resp = classify_inference_triples(inferred_path=tmp.name)
    data = json.loads(resp)
    # 3개의 BNode triple 이 모두 trivial 에 집계
    assert data["trivial_count"] == 3
    assert data["meaningful_count"] == 1
    assert data["suspicious_count"] == 0
