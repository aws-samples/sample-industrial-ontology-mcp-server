"""Tests for SHACL MaxCount(1) domain cleanup after union creation.

improve_tbox 의 Step 9e 가 domain 2개 이상인 property 를 unionOf 로 병합한다.
하지만 downstream 의 Step 13 (OP someValuesFrom) 또는 다른 후처리가
URIRef domain 을 재추가할 때 기존 unionOf BNode domain 과 공존하면
SHACL MaxCount(1) 위반이 재발한다.

이 테스트는 _cleanup_redundant_domain_after_union 헬퍼가 정확히 이 문제를
감지하고 URIRef domain 을 제거하여 unionOf BNode 만 남기는지 검증한다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS


def _make_union_with_redundant_domain() -> tuple[Graph, URIRef, URIRef, URIRef]:
    """T-Box fragment with prop having both unionOf BNode and redundant URIRef domain.

    Simulates the post-Step 9e + post-Step 13 state where cleanup is needed.
    Returns (graph, prop_uri, class_a, class_b).
    """
    from domain.tbox_utils import _new_graph
    g = _new_graph()
    prop = URIRef(DOMAIN_NS + "hasExample")
    cls_a = URIRef(DOMAIN_NS + "ClassA")
    cls_b = URIRef(DOMAIN_NS + "ClassB")

    # Classes
    g.add((cls_a, RDF.type, OWL.Class))
    g.add((cls_b, RDF.type, OWL.Class))
    # Property declaration
    g.add((prop, RDF.type, OWL.ObjectProperty))

    # unionOf BNode
    union_node = BNode()
    list_head = BNode()
    Collection(g, list_head, [cls_a, cls_b])
    g.add((union_node, RDF.type, OWL.Class))
    g.add((union_node, OWL.unionOf, list_head))
    g.add((prop, RDFS.domain, union_node))

    # Redundant URIRef domain (simulates downstream step re-adding)
    g.add((prop, RDFS.domain, cls_a))

    return g, prop, cls_a, cls_b


class TestCleanupRedundantDomainAfterUnion:
    def test_removes_uriref_when_union_bnode_covers_it(self):
        """URIRef domain 이 unionOf BNode 의 멤버이면 URIRef 제거."""
        from tools.ontology_quality import _cleanup_redundant_domain_after_union
        g, prop, cls_a, cls_b = _make_union_with_redundant_domain()

        removed = _cleanup_redundant_domain_after_union(g)

        assert removed == 1
        domains = list(g.objects(prop, RDFS.domain))
        # Only unionOf BNode remains
        assert len(domains) == 1
        assert isinstance(domains[0], BNode)
        # unionOf members unchanged
        union_list = g.value(domains[0], OWL.unionOf)
        members = list(Collection(g, union_list))
        assert set(members) == {cls_a, cls_b}

    def test_no_change_when_only_union_domain(self):
        """URIRef domain 이 없으면 아무것도 제거 안 함."""
        from domain.tbox_utils import _new_graph
        from tools.ontology_quality import _cleanup_redundant_domain_after_union
        g = _new_graph()
        prop = URIRef(DOMAIN_NS + "hasExample")
        cls_a = URIRef(DOMAIN_NS + "ClassA")
        cls_b = URIRef(DOMAIN_NS + "ClassB")
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))
        union_node = BNode()
        list_head = BNode()
        Collection(g, list_head, [cls_a, cls_b])
        g.add((union_node, RDF.type, OWL.Class))
        g.add((union_node, OWL.unionOf, list_head))
        g.add((prop, RDFS.domain, union_node))

        removed = _cleanup_redundant_domain_after_union(g)
        assert removed == 0
        assert len(list(g.objects(prop, RDFS.domain))) == 1

    def test_no_change_when_only_uriref_domains_and_no_union(self):
        """unionOf 가 없으면 URIRef 제거하지 않음 (Step 9e 영역이 아님)."""
        from domain.tbox_utils import _new_graph
        from tools.ontology_quality import _cleanup_redundant_domain_after_union
        g = _new_graph()
        prop = URIRef(DOMAIN_NS + "hasExample")
        cls_a = URIRef(DOMAIN_NS + "ClassA")
        cls_b = URIRef(DOMAIN_NS + "ClassB")
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))
        # 2 URIRef domain (no union) — SHACL 위반이지만 본 헬퍼 대상 아님
        g.add((prop, RDFS.domain, cls_a))
        g.add((prop, RDFS.domain, cls_b))

        removed = _cleanup_redundant_domain_after_union(g)
        assert removed == 0
        assert len(list(g.objects(prop, RDFS.domain))) == 2

    def test_preserves_uriref_not_in_union_members(self):
        """unionOf 에 포함되지 않은 URIRef domain 은 유지 (안전 가드)."""
        from domain.tbox_utils import _new_graph
        from tools.ontology_quality import _cleanup_redundant_domain_after_union
        g = _new_graph()
        prop = URIRef(DOMAIN_NS + "hasExample")
        cls_a = URIRef(DOMAIN_NS + "ClassA")
        cls_b = URIRef(DOMAIN_NS + "ClassB")
        cls_c = URIRef(DOMAIN_NS + "ClassC")  # union 멤버 아님
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))
        g.add((cls_c, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))
        union_node = BNode()
        list_head = BNode()
        Collection(g, list_head, [cls_a, cls_b])
        g.add((union_node, RDF.type, OWL.Class))
        g.add((union_node, OWL.unionOf, list_head))
        g.add((prop, RDFS.domain, union_node))
        g.add((prop, RDFS.domain, cls_c))  # union 에 없는 class

        removed = _cleanup_redundant_domain_after_union(g)
        # cls_c 는 union 멤버 아니므로 보존 — 0 제거
        assert removed == 0
        assert len(list(g.objects(prop, RDFS.domain))) == 2

    def test_handles_multiple_props_simultaneously(self):
        """여러 property 가 동시에 문제 있어도 전부 처리."""
        from tools.ontology_quality import _cleanup_redundant_domain_after_union
        g, p1, a, b = _make_union_with_redundant_domain()
        # 두 번째 property 추가
        prop2 = URIRef(DOMAIN_NS + "hasOther")
        cls_x = URIRef(DOMAIN_NS + "ClassX")
        cls_y = URIRef(DOMAIN_NS + "ClassY")
        g.add((cls_x, RDF.type, OWL.Class))
        g.add((cls_y, RDF.type, OWL.Class))
        g.add((prop2, RDF.type, OWL.ObjectProperty))
        union2 = BNode()
        list2 = BNode()
        Collection(g, list2, [cls_x, cls_y])
        g.add((union2, RDF.type, OWL.Class))
        g.add((union2, OWL.unionOf, list2))
        g.add((prop2, RDFS.domain, union2))
        g.add((prop2, RDFS.domain, cls_x))  # redundant

        removed = _cleanup_redundant_domain_after_union(g)
        assert removed == 2
        # 각 prop 에 unionOf BNode 1개만 남음
        assert len(list(g.objects(p1, RDFS.domain))) == 1
        assert len(list(g.objects(prop2, RDFS.domain))) == 1

    def test_also_cleans_range(self):
        """동일 로직이 rdfs:range 에도 적용 (SHACL MaxCount range 위반 대응)."""
        from domain.tbox_utils import _new_graph
        from tools.ontology_quality import _cleanup_redundant_domain_after_union
        g = _new_graph()
        prop = URIRef(DOMAIN_NS + "hasExample")
        cls_a = URIRef(DOMAIN_NS + "ClassA")
        cls_b = URIRef(DOMAIN_NS + "ClassB")
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))
        union_node = BNode()
        list_head = BNode()
        Collection(g, list_head, [cls_a, cls_b])
        g.add((union_node, RDF.type, OWL.Class))
        g.add((union_node, OWL.unionOf, list_head))
        g.add((prop, RDFS.range, union_node))
        g.add((prop, RDFS.range, cls_a))  # redundant range

        removed = _cleanup_redundant_domain_after_union(g)
        assert removed == 1
        assert len(list(g.objects(prop, RDFS.range))) == 1
