"""Tests for owl:disjointUnionOf injection in improve_tbox (Step 28).

When a disjoint group carries a ``union_parent`` directive and every group
member is declared subClassOf that parent in the T-Box, the parent is
promoted to:

    parent owl:disjointUnionOf ( child1 child2 ... ) .

This is strictly stronger than a pairwise disjoint + independent subClassOf
links, because it asserts classification completeness — any instance of the
parent MUST be classified into exactly one of the listed children.
"""
from __future__ import annotations

from rdflib import OWL, Graph, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import _inject_disjoint_union_of


def _build_graph(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def _union_members(g: Graph, parent_uri: URIRef) -> list[URIRef]:
    lists = list(g.objects(parent_uri, OWL.disjointUnionOf))
    if not lists:
        return []
    return list(Collection(g, lists[0]))


class TestDisjointUnionHappy:
    def test_promotes_when_all_members_subclass_of_parent(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:ManufacturingProcessStep a owl:Class .
        steel:ProcessBlastFurnace a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        steel:ProcessSteelmakingFurnace a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        steel:ProcessContinuousCasting a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        steel:ProcessRolling a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        """
        g = _build_graph(ttl)
        groups = [
            {
                "label": "공정",
                "union_parent": "ManufacturingProcessStep",
                "classes": [
                    "ProcessBlastFurnace", "ProcessSteelmakingFurnace",
                    "ProcessContinuousCasting", "ProcessRolling",
                ],
            },
        ]
        stats = _inject_disjoint_union_of(g, groups)
        parent = URIRef(f"{DOMAIN_NS}ManufacturingProcessStep")
        members = _union_members(g, parent)
        assert set(members) == {
            URIRef(f"{DOMAIN_NS}ProcessBlastFurnace"),
            URIRef(f"{DOMAIN_NS}ProcessSteelmakingFurnace"),
            URIRef(f"{DOMAIN_NS}ProcessContinuousCasting"),
            URIRef(f"{DOMAIN_NS}ProcessRolling"),
        }
        assert stats["disjoint_unions_added"] == 1


class TestDisjointUnionSkipsWhenMembershipIncomplete:
    def test_skip_when_child_not_subclass_of_parent(self):
        # Rolling is not linked to the parent — cannot safely promote to
        # disjointUnion because it would claim parent has ONLY the other three.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:ManufacturingProcessStep a owl:Class .
        steel:ProcessBlastFurnace a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        steel:ProcessSteelmakingFurnace a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        steel:ProcessContinuousCasting a owl:Class ;
            rdfs:subClassOf steel:ManufacturingProcessStep .
        steel:ProcessRolling a owl:Class .
        """
        g = _build_graph(ttl)
        groups = [
            {
                "union_parent": "ManufacturingProcessStep",
                "classes": [
                    "ProcessBlastFurnace", "ProcessSteelmakingFurnace",
                    "ProcessContinuousCasting", "ProcessRolling",
                ],
            },
        ]
        stats = _inject_disjoint_union_of(g, groups)
        parent = URIRef(f"{DOMAIN_NS}ManufacturingProcessStep")
        assert _union_members(g, parent) == []
        assert stats["disjoint_unions_added"] == 0
        assert stats["disjoint_unions_skipped_incomplete"] >= 1


class TestDisjointUnionSkipsGroupsWithoutParent:
    def test_group_without_union_parent_is_untouched(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:A a owl:Class .
        steel:B a owl:Class .
        """
        g = _build_graph(ttl)
        groups = [{"classes": ["A", "B"]}]  # no union_parent
        stats = _inject_disjoint_union_of(g, groups)
        # No disjointUnionOf anywhere in the graph.
        assert list(g.triples((None, OWL.disjointUnionOf, None))) == []
        assert stats["disjoint_unions_added"] == 0


class TestDisjointUnionIdempotent:
    def test_existing_disjoint_union_not_duplicated(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:Parent a owl:Class .
        steel:A a owl:Class ; rdfs:subClassOf steel:Parent .
        steel:B a owl:Class ; rdfs:subClassOf steel:Parent .
        """
        g = _build_graph(ttl)
        groups = [{"union_parent": "Parent", "classes": ["A", "B"]}]
        _inject_disjoint_union_of(g, groups)
        stats2 = _inject_disjoint_union_of(g, groups)
        parent = URIRef(f"{DOMAIN_NS}Parent")
        lists = list(g.objects(parent, OWL.disjointUnionOf))
        assert len(lists) == 1  # Not duplicated.
        assert stats2["disjoint_unions_added"] == 0
        assert stats2["disjoint_unions_skipped_existing"] >= 1


class TestDisjointUnionRequiresTwoOrMoreMembers:
    def test_single_member_group_skipped(self):
        # A single-member "union" is not really a union; it collapses the
        # parent into one child. Skip as degenerate.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:Parent a owl:Class .
        steel:Lone a owl:Class ; rdfs:subClassOf steel:Parent .
        """
        g = _build_graph(ttl)
        groups = [{"union_parent": "Parent", "classes": ["Lone"]}]
        stats = _inject_disjoint_union_of(g, groups)
        assert stats["disjoint_unions_added"] == 0
