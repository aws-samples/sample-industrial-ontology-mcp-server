"""Tests for owl:propertyChainAxiom injection (Step 29, R13).

Property chains materialise multi-hop knowledge as a single reasoned step.
Declarations live in rules/domain/property_chains.json so the domain can swap them
without touching code. Each declaration names an existing ObjectProperty and
the ordered chain of ObjectProperties whose composition entails it.
"""
from __future__ import annotations

from rdflib import OWL, RDF, Graph, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import _inject_property_chain_axioms


def _g(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def _chain_members(g: Graph, op_uri: URIRef) -> list[URIRef]:
    lists = list(g.objects(op_uri, OWL.propertyChainAxiom))
    if not lists:
        return []
    return list(Collection(g, lists[0]))


class TestPropertyChainHappy:
    def test_two_hop_chain_injected(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:hasAncestor a owl:ObjectProperty .
        steel:hasPredecessor a owl:ObjectProperty .
        """
        g = _g(ttl)
        chains = [{
            "property": "hasAncestor",
            "chain": ["hasPredecessor", "hasPredecessor"],
        }]
        stats = _inject_property_chain_axioms(g, chains)
        members = _chain_members(g, URIRef(f"{DOMAIN_NS}hasAncestor"))
        assert members == [
            URIRef(f"{DOMAIN_NS}hasPredecessor"),
            URIRef(f"{DOMAIN_NS}hasPredecessor"),
        ]
        assert stats["chains_added"] == 1


class TestPropertyChainSkipsMissingProperty:
    def test_skip_when_target_property_not_in_graph(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:hasPredecessor a owl:ObjectProperty .
        """
        # hasAncestor is never declared as an OP.
        g = _g(ttl)
        chains = [{
            "property": "hasAncestor",
            "chain": ["hasPredecessor", "hasPredecessor"],
        }]
        stats = _inject_property_chain_axioms(g, chains)
        assert _chain_members(g, URIRef(f"{DOMAIN_NS}hasAncestor")) == []
        assert stats["chains_added"] == 0
        assert stats["chains_skipped_missing_property"] >= 1

    def test_skip_when_chain_segment_missing(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:hasAncestor a owl:ObjectProperty .
        """
        # hasPredecessor is missing.
        g = _g(ttl)
        chains = [{
            "property": "hasAncestor",
            "chain": ["hasPredecessor", "hasPredecessor"],
        }]
        stats = _inject_property_chain_axioms(g, chains)
        assert _chain_members(g, URIRef(f"{DOMAIN_NS}hasAncestor")) == []
        assert stats["chains_added"] == 0
        assert stats["chains_skipped_missing_segment"] >= 1


class TestPropertyChainIdempotent:
    def test_existing_chain_not_duplicated(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:hasAncestor a owl:ObjectProperty .
        steel:hasPredecessor a owl:ObjectProperty .
        """
        g = _g(ttl)
        chains = [{"property": "hasAncestor",
                   "chain": ["hasPredecessor", "hasPredecessor"]}]
        _inject_property_chain_axioms(g, chains)
        stats2 = _inject_property_chain_axioms(g, chains)
        op_uri = URIRef(f"{DOMAIN_NS}hasAncestor")
        lists = list(g.objects(op_uri, OWL.propertyChainAxiom))
        assert len(lists) == 1
        assert stats2["chains_added"] == 0
        assert stats2["chains_skipped_existing"] >= 1


class TestPropertyChainRequiresMinimumLength:
    def test_single_hop_chain_skipped(self):
        # A one-hop "chain" is just subPropertyOf, not a chain axiom.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:hasAncestor a owl:ObjectProperty .
        steel:hasPredecessor a owl:ObjectProperty .
        """
        g = _g(ttl)
        chains = [{"property": "hasAncestor",
                   "chain": ["hasPredecessor"]}]
        stats = _inject_property_chain_axioms(g, chains)
        assert _chain_members(g, URIRef(f"{DOMAIN_NS}hasAncestor")) == []
        assert stats["chains_added"] == 0


class TestPropertyChainDeclareTarget:
    """R25: declare_target 가 있으면 target OP 가 없어도 skeleton 선언 + chain 주입."""

    def test_declare_target_creates_skeleton_and_injects_chain(self):
        from rdflib import RDFS
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:WQM a owl:Class .
        steel:Point a owl:Class .
        steel:Equipment a owl:Class .
        steel:measuredAtPoint a owl:ObjectProperty .
        steel:pointMonitorsEquipment a owl:ObjectProperty .
        """
        g = _g(ttl)
        chains = [{
            "property": "hasInspectionEquipment",
            "chain": ["measuredAtPoint", "pointMonitorsEquipment"],
            "declare_target": {
                "domain": "WQM",
                "range": "Equipment",
                "label_en": "has inspection equipment",
                "label_ko": "점검 대상 설비",
                "comment_ko": "2-hop 추론",
                "inverse": "isInspectionEquipmentOf",
            },
        }]
        stats = _inject_property_chain_axioms(g, chains)
        target = URIRef(f"{DOMAIN_NS}hasInspectionEquipment")
        inv = URIRef(f"{DOMAIN_NS}isInspectionEquipmentOf")
        # target skeleton 이 선언됐어야 함
        assert (target, RDF.type, OWL.ObjectProperty) in g
        assert (target, RDFS.domain, URIRef(f"{DOMAIN_NS}WQM")) in g
        assert (target, RDFS.range, URIRef(f"{DOMAIN_NS}Equipment")) in g
        # inverse 도 선언됨
        assert (inv, RDF.type, OWL.ObjectProperty) in g
        assert (target, OWL.inverseOf, inv) in g
        assert (inv, OWL.inverseOf, target) in g
        # chain 도 주입됨
        members = _chain_members(g, target)
        assert members == [
            URIRef(f"{DOMAIN_NS}measuredAtPoint"),
            URIRef(f"{DOMAIN_NS}pointMonitorsEquipment"),
        ]
        assert stats["targets_declared"] == 1
        assert stats["chains_added"] == 1

    def test_inverse_op_gets_label_and_comment(self):
        """R26: declare_target 의 inverse OP 도 label/comment 를 받아야 SHACL 통과."""
        from rdflib import RDFS
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:WQM a owl:Class .
        steel:Equipment a owl:Class .
        steel:measuredAtPoint a owl:ObjectProperty .
        steel:pointMonitorsEquipment a owl:ObjectProperty .
        """
        g = _g(ttl)
        chains = [{
            "property": "hasInspection",
            "chain": ["measuredAtPoint", "pointMonitorsEquipment"],
            "declare_target": {
                "domain": "WQM",
                "range": "Equipment",
                "label_en": "has inspection",
                "label_ko": "점검 대상",
                "comment_ko": "2-hop 추론",
                "inverse": "isInspectionOf",
            },
        }]
        _inject_property_chain_axioms(g, chains)
        inv = URIRef(f"{DOMAIN_NS}isInspectionOf")
        inv_labels = list(g.objects(inv, RDFS.label))
        inv_comments = list(g.objects(inv, RDFS.comment))
        # 최소 en/ko label 하나씩
        assert len(inv_labels) >= 1, f"inverse should have label, got {inv_labels}"
        assert len(inv_comments) >= 1, f"inverse should have comment, got {inv_comments}"

    def test_without_declare_target_still_skips_missing_property(self):
        """declare_target 없으면 기존 동작 유지 (missing_property skip)."""
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:measuredAtPoint a owl:ObjectProperty .
        steel:pointMonitorsEquipment a owl:ObjectProperty .
        """
        g = _g(ttl)
        chains = [{
            "property": "hasInspectionEquipment",
            "chain": ["measuredAtPoint", "pointMonitorsEquipment"],
        }]
        stats = _inject_property_chain_axioms(g, chains)
        assert stats["chains_skipped_missing_property"] == 1
        assert stats["targets_declared"] == 0
