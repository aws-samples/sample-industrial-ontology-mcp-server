"""tests for _skolemize_bnodes() — BNode 제거 (Step 18)."""

import hashlib

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS, DOMAIN_NS_OBJ
from domain.tbox_utils import _new_graph


def _make_graph_with_prefixes() -> Graph:
    """표준 PREFIX가 바인딩된 빈 그래프 생성."""
    g = _new_graph()
    g.bind("steel", DOMAIN_NS_OBJ)
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    return g


# ── owl:Restriction BNode ────────────────────────────


class TestRestrictionSkolemization:
    """owl:Restriction BNode -> deterministic named IRI."""

    def test_someValuesFrom_restriction(self):
        """someValuesFrom restriction BNode이 IRI로 변환되어야 함."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["EquipmentMaster"]
        prop = DOMAIN_NS_OBJ["hasStatus"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.someValuesFrom, DOMAIN_NS_OBJ["EquipmentStatus"]))
        g.add((cls, RDFS.subClassOf, bnode))

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 1
        assert result["by_type"]["Restriction"] == 1
        # BNode이 그래프에 남아있지 않아야 함
        restriction_subjects = [s for s in g.subjects(RDF.type, OWL.Restriction)
                                if isinstance(s, BNode)]
        assert restriction_subjects == []
        # named IRI가 생겨야 함
        expected_uri = DOMAIN_NS_OBJ["EquipmentMaster_hasStatus_someValuesFrom"]
        assert (expected_uri, RDF.type, OWL.Restriction) in g
        assert (expected_uri, OWL.onProperty, prop) in g
        assert (cls, RDFS.subClassOf, expected_uri) in g

    def test_hasValue_restriction(self):
        """hasValue restriction이 올바르게 skolemize 되어야 함."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["Scope1Emission"]
        prop = DOMAIN_NS_OBJ["scopeType"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.DatatypeProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.hasValue, Literal("Scope1")))
        g.add((cls, RDFS.subClassOf, bnode))

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 1
        expected_uri = DOMAIN_NS_OBJ["Scope1Emission_scopeType_hasValue"]
        assert (expected_uri, RDF.type, OWL.Restriction) in g
        assert (expected_uri, OWL.hasValue, Literal("Scope1")) in g

    def test_allValuesFrom_restriction(self):
        """allValuesFrom restriction skolemization."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["QualityTest"]
        prop = DOMAIN_NS_OBJ["hasResult"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.allValuesFrom, DOMAIN_NS_OBJ["TestResult"]))
        g.add((cls, RDFS.subClassOf, bnode))

        _skolemize_bnodes(g)

        expected_uri = DOMAIN_NS_OBJ["QualityTest_hasResult_allValuesFrom"]
        assert (expected_uri, RDF.type, OWL.Restriction) in g

    def test_minCardinality_restriction(self):
        """minCardinality restriction skolemization."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["EquipmentMaster"]
        prop = DOMAIN_NS_OBJ["equipmentID"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.DatatypeProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.minCardinality, Literal(1)))
        g.add((cls, RDFS.subClassOf, bnode))

        _skolemize_bnodes(g)

        expected_uri = DOMAIN_NS_OBJ["EquipmentMaster_equipmentID_minCardinality"]
        assert (expected_uri, RDF.type, OWL.Restriction) in g

    def test_maxCardinality_restriction(self):
        """maxCardinality restriction skolemization."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["SensorReading"]
        prop = DOMAIN_NS_OBJ["sensorValue"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.DatatypeProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.maxCardinality, Literal(1)))
        g.add((cls, RDFS.subClassOf, bnode))

        _skolemize_bnodes(g)

        expected_uri = DOMAIN_NS_OBJ["SensorReading_sensorValue_maxCardinality"]
        assert (expected_uri, RDF.type, OWL.Restriction) in g

    def test_equivalentClass_restriction(self):
        """owl:equivalentClass로 참조된 restriction도 skolemize."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["ActiveEquipment"]
        prop = DOMAIN_NS_OBJ["isActive"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.DatatypeProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.hasValue, Literal(True)))
        g.add((cls, OWL.equivalentClass, bnode))

        _skolemize_bnodes(g)

        expected_uri = DOMAIN_NS_OBJ["ActiveEquipment_isActive_hasValue"]
        assert (expected_uri, RDF.type, OWL.Restriction) in g
        assert (cls, OWL.equivalentClass, expected_uri) in g

    def test_collision_avoidance(self):
        """동일 클래스/프로퍼티에 여러 restriction이면 hash suffix로 충돌 방지."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["EquipmentMaster"]
        prop = DOMAIN_NS_OBJ["equipmentID"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.DatatypeProperty))

        # 같은 restriction_type으로 2개 BNode 생성
        bnode1 = BNode()
        g.add((bnode1, RDF.type, OWL.Restriction))
        g.add((bnode1, OWL.onProperty, prop))
        g.add((bnode1, OWL.minCardinality, Literal(1)))
        g.add((cls, RDFS.subClassOf, bnode1))

        bnode2 = BNode()
        g.add((bnode2, RDF.type, OWL.Restriction))
        g.add((bnode2, OWL.onProperty, prop))
        g.add((bnode2, OWL.minCardinality, Literal(2)))
        g.add((cls, RDFS.subClassOf, bnode2))

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 2
        # BNode이 모두 제거됨
        restriction_bnodes = [s for s in g.subjects(RDF.type, OWL.Restriction)
                              if isinstance(s, BNode)]
        assert restriction_bnodes == []

    def test_duplicate_bnodes_merged_into_single_uri(self):
        """의미론적으로 동일한 restriction BNode 3개가 하나의 URI로 병합되어야 한다.

        과거 skolemize는 hash suffix 를 붙여 별개 URI 로 분기시켜 OWL RL 추론
        closure 계산 폭발을 초래했다. 같은 (cls, prop, type, target) 은 하나의
        URI 로 병합해야 한다.
        """
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["Equipment"]
        prop = DOMAIN_NS_OBJ["hasEnergy"]
        target = DOMAIN_NS_OBJ["Energy"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((target, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))

        # 의미론적으로 동일한 restriction BNode 3개
        for _ in range(3):
            b = BNode()
            g.add((b, RDF.type, OWL.Restriction))
            g.add((b, OWL.onProperty, prop))
            g.add((b, OWL.someValuesFrom, target))
            g.add((cls, RDFS.subClassOf, b))

        result = _skolemize_bnodes(g)

        # 최초 1개만 skolemize, 나머지 2개는 merged
        assert result["by_type"]["Restriction"] == 1
        assert result["by_type"]["Restriction_merged_duplicates"] == 2
        # cls 의 subClassOf 중 restriction URI 는 단 1개만 남아야 함
        restriction_uris = {
            o for o in g.objects(cls, RDFS.subClassOf)
            if isinstance(o, URIRef)
        }
        assert len(restriction_uris) == 1

    def test_dedup_named_restrictions_merges_existing_uris(self):
        """이미 URIRef 로 skolemize 된 동일 의미 restriction 3개가 1개로 병합."""
        from tools.ontology_quality import _dedup_named_restrictions

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["Equipment"]
        prop = DOMAIN_NS_OBJ["hasEnergy"]
        target = DOMAIN_NS_OBJ["Energy"]
        uris = [
            DOMAIN_NS_OBJ["Equipment_hasEnergy_someValuesFrom"],
            DOMAIN_NS_OBJ["Equipment_hasEnergy_someValuesFrom_abcd1234"],
            DOMAIN_NS_OBJ["Equipment_hasEnergy_someValuesFrom_deadbeef"],
        ]
        for u in uris:
            g.add((u, RDF.type, OWL.Restriction))
            g.add((u, OWL.onProperty, prop))
            g.add((u, OWL.someValuesFrom, target))
            g.add((cls, RDFS.subClassOf, u))

        stats = _dedup_named_restrictions(g)

        assert stats["merged"] == 2
        assert stats["removed"] > 0
        # subClassOf 가 1개 canonical URI 만 참조해야 함
        sub_restr = {o for o in g.objects(cls, RDFS.subClassOf)
                     if isinstance(o, URIRef)}
        assert len(sub_restr) == 1
        # canonical = alphabetical first (접미사 없는 URI)
        assert next(iter(sub_restr)) == uris[0]

    def test_no_referencing_class_skipped(self):
        """참조 클래스가 없는 orphan restriction BNode은 건너뜀."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        prop = DOMAIN_NS_OBJ["hasStatus"]
        g.add((prop, RDF.type, OWL.ObjectProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.someValuesFrom, DOMAIN_NS_OBJ["Status"]))
        # 참조하는 클래스 없음

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 0
        # BNode 그대로 남아있어야 함
        assert (bnode, RDF.type, OWL.Restriction) in g


# ── AllDisjointClasses BNode ─────────────────────────


class TestAllDisjointClassesSkolemization:
    """owl:AllDisjointClasses BNode -> deterministic named IRI."""

    def test_basic_disjoint_classes(self):
        """AllDisjointClasses BNode이 hash 기반 IRI로 변환."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls_a = DOMAIN_NS_OBJ["ClassA"]
        cls_b = DOMAIN_NS_OBJ["ClassB"]
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.AllDisjointClasses))
        members_list = BNode()
        g.add((bnode, OWL.members, members_list))
        Collection(g, members_list, [cls_a, cls_b])

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 1
        assert result["by_type"]["AllDisjointClasses"] == 1
        # BNode이 제거됨
        adj_bnodes = [s for s in g.subjects(RDF.type, OWL.AllDisjointClasses)
                      if isinstance(s, BNode)]
        assert adj_bnodes == []
        # IRI가 생김
        hash8 = hashlib.md5(
            b"ClassA_ClassB", usedforsecurity=False
        ).hexdigest()[:8]
        expected_uri = DOMAIN_NS_OBJ[f"AllDisjoint_{hash8}"]
        assert (expected_uri, RDF.type, OWL.AllDisjointClasses) in g

    def test_members_order_independent(self):
        """멤버 순서가 달라도 동일 hash 생성 (sorted)."""
        from tools.ontology_quality import _skolemize_bnodes

        g1 = _make_graph_with_prefixes()
        g2 = _make_graph_with_prefixes()
        cls_a = DOMAIN_NS_OBJ["ZZZClass"]
        cls_b = DOMAIN_NS_OBJ["AAAClass"]

        for g in [g1, g2]:
            g.add((cls_a, RDF.type, OWL.Class))
            g.add((cls_b, RDF.type, OWL.Class))

        # g1: A, B 순서
        bnode1 = BNode()
        g1.add((bnode1, RDF.type, OWL.AllDisjointClasses))
        ml1 = BNode()
        g1.add((bnode1, OWL.members, ml1))
        Collection(g1, ml1, [cls_a, cls_b])

        # g2: B, A 순서
        bnode2 = BNode()
        g2.add((bnode2, RDF.type, OWL.AllDisjointClasses))
        ml2 = BNode()
        g2.add((bnode2, OWL.members, ml2))
        Collection(g2, ml2, [cls_b, cls_a])

        _skolemize_bnodes(g1)
        _skolemize_bnodes(g2)

        # 동일한 IRI 생성
        named_uris_1 = {s for s in g1.subjects(RDF.type, OWL.AllDisjointClasses)
                        if isinstance(s, URIRef)}
        named_uris_2 = {s for s in g2.subjects(RDF.type, OWL.AllDisjointClasses)
                        if isinstance(s, URIRef)}
        assert named_uris_1 == named_uris_2

    def test_rdf_list_bnodes_preserved(self):
        """RDF List 내부 BNode(rdf:first/rdf:rest)는 skolemize 하지 않음."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls_a = DOMAIN_NS_OBJ["ClassA"]
        cls_b = DOMAIN_NS_OBJ["ClassB"]
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.AllDisjointClasses))
        members_list = BNode()
        g.add((bnode, OWL.members, members_list))
        Collection(g, members_list, [cls_a, cls_b])

        # Collection에 의해 생성된 rdf:first/rdf:rest BNode 개수 세기
        list_bnodes_before = set()
        for s, _p, _o in g.triples((None, RDF.first, None)):
            if isinstance(s, BNode):
                list_bnodes_before.add(s)
        for s, _p, _o in g.triples((None, RDF.rest, None)):
            if isinstance(s, BNode):
                list_bnodes_before.add(s)

        _skolemize_bnodes(g)

        # RDF List BNode는 그대로 남아있어야 함
        list_bnodes_after = set()
        for s, _p, _o in g.triples((None, RDF.first, None)):
            if isinstance(s, BNode):
                list_bnodes_after.add(s)
        for s, _p, _o in g.triples((None, RDF.rest, None)):
            if isinstance(s, BNode):
                list_bnodes_after.add(s)

        assert list_bnodes_before == list_bnodes_after


# ── equivalentClass/unionOf BNode ────────────────────


class TestUnionSkolemization:
    """equivalentClass + unionOf BNode -> deterministic named IRI."""

    def test_basic_union(self):
        """equivalentClass unionOf BNode이 IRI로 변환."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["ManufacturingProcess"]
        cls_a = DOMAIN_NS_OBJ["Ironmaking"]
        cls_b = DOMAIN_NS_OBJ["Steelmaking"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))

        union_bnode = BNode()
        union_list = BNode()
        g.add((cls, OWL.equivalentClass, union_bnode))
        g.add((union_bnode, OWL.unionOf, union_list))
        Collection(g, union_list, [cls_a, cls_b])

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 1
        assert result["by_type"]["Union"] == 1
        # BNode이 제거됨
        union_bnodes = [o for o in g.objects(cls, OWL.equivalentClass)
                        if isinstance(o, BNode)]
        assert union_bnodes == []

    def test_union_iri_contains_class_name(self):
        """Union IRI는 클래스 이름을 포함해야 함."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["ManufacturingProcess"]
        cls_a = DOMAIN_NS_OBJ["Ironmaking"]
        cls_b = DOMAIN_NS_OBJ["Steelmaking"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))

        union_bnode = BNode()
        union_list = BNode()
        g.add((cls, OWL.equivalentClass, union_bnode))
        g.add((union_bnode, OWL.unionOf, union_list))
        Collection(g, union_list, [cls_a, cls_b])

        _skolemize_bnodes(g)

        # 새 IRI가 Union_ManufacturingProcess_ 로 시작해야 함
        named_uris = [o for o in g.objects(cls, OWL.equivalentClass)
                      if isinstance(o, URIRef)]
        assert len(named_uris) == 1
        assert "Union_ManufacturingProcess_" in str(named_uris[0])

    def test_union_list_bnodes_preserved(self):
        """unionOf RDF List 내부 BNode는 보존."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["Process"]
        cls_a = DOMAIN_NS_OBJ["StepA"]
        cls_b = DOMAIN_NS_OBJ["StepB"]
        g.add((cls, RDF.type, OWL.Class))

        union_bnode = BNode()
        union_list = BNode()
        g.add((cls, OWL.equivalentClass, union_bnode))
        g.add((union_bnode, OWL.unionOf, union_list))
        Collection(g, union_list, [cls_a, cls_b])

        # rdf:first BNode 세기
        list_bnodes_before = {s for s, _, _ in g.triples((None, RDF.first, None))
                              if isinstance(s, BNode)}

        _skolemize_bnodes(g)

        list_bnodes_after = {s for s, _, _ in g.triples((None, RDF.first, None))
                             if isinstance(s, BNode)}
        assert list_bnodes_before == list_bnodes_after


# ── Return value / Edge cases ────────────────────────


class TestReturnValueAndEdgeCases:
    """반환값 구조, 빈 그래프, 혼합 BNode 테스트."""

    def test_empty_graph(self):
        """빈 그래프에서 에러 없이 0 반환."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 0
        assert result["by_type"]["Restriction"] == 0
        assert result["by_type"]["AllDisjointClasses"] == 0
        assert result["by_type"]["Union"] == 0
        # 신규 키 — 중복 병합 수 (빈 그래프에서 0)
        assert result["by_type"].get("Restriction_merged_duplicates", 0) == 0

    def test_return_structure(self):
        """반환값이 지정된 구조를 따라야 함."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        result = _skolemize_bnodes(g)

        assert "total_skolemized" in result
        assert "by_type" in result
        assert "Restriction" in result["by_type"]
        assert "AllDisjointClasses" in result["by_type"]
        assert "Union" in result["by_type"]
        assert result["total_skolemized"] == sum(result["by_type"].values())

    def test_mixed_bnode_types(self):
        """Restriction + AllDisjointClasses + Union 혼합 그래프."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls_a = DOMAIN_NS_OBJ["ClassA"]
        cls_b = DOMAIN_NS_OBJ["ClassB"]
        cls_union = DOMAIN_NS_OBJ["UnionClass"]
        prop = DOMAIN_NS_OBJ["hasProp"]
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((cls_b, RDF.type, OWL.Class))
        g.add((cls_union, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))

        # Restriction
        r_bnode = BNode()
        g.add((r_bnode, RDF.type, OWL.Restriction))
        g.add((r_bnode, OWL.onProperty, prop))
        g.add((r_bnode, OWL.someValuesFrom, cls_b))
        g.add((cls_a, RDFS.subClassOf, r_bnode))

        # AllDisjointClasses
        adj_bnode = BNode()
        g.add((adj_bnode, RDF.type, OWL.AllDisjointClasses))
        ml = BNode()
        g.add((adj_bnode, OWL.members, ml))
        Collection(g, ml, [cls_a, cls_b])

        # Union
        u_bnode = BNode()
        u_list = BNode()
        g.add((cls_union, OWL.equivalentClass, u_bnode))
        g.add((u_bnode, OWL.unionOf, u_list))
        Collection(g, u_list, [cls_a, cls_b])

        result = _skolemize_bnodes(g)

        assert result["total_skolemized"] == 3
        assert result["by_type"]["Restriction"] == 1
        assert result["by_type"]["AllDisjointClasses"] == 1
        assert result["by_type"]["Union"] == 1

    def test_triple_count_preserved(self):
        """skolemization 전후 트리플 수가 동일해야 함 (IRI 교체일 뿐)."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["EquipmentMaster"]
        prop = DOMAIN_NS_OBJ["hasStatus"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))

        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, prop))
        g.add((bnode, OWL.someValuesFrom, DOMAIN_NS_OBJ["Status"]))
        g.add((cls, RDFS.subClassOf, bnode))

        count_before = len(g)
        _skolemize_bnodes(g)
        count_after = len(g)

        assert count_before == count_after

    def test_no_bnodes_is_noop(self):
        """BNode이 없는 그래프는 변경 없이 통과."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        cls = DOMAIN_NS_OBJ["EquipmentMaster"]
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal("Equipment Master", lang="en")))

        triples_before = set(g)
        result = _skolemize_bnodes(g)
        triples_after = set(g)

        assert result["total_skolemized"] == 0
        assert triples_before == triples_after


# ── Step 18 통합 테스트 ──────────────────────────────


class TestStep18Integration:
    """improve_tbox() Step 18 통합: stats에 bnode_skolemized가 포함되어야 함."""

    def test_stats_contains_bnode_keys(self):
        """improve_tbox 결과 stats에 bnode_skolemized, bnode_by_type 키가 존재."""
        from tools.ontology_quality import improve_tbox

        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .

steel:MaintenanceHistory a owl:Class ;
    rdfs:label "Maintenance History"@en, "정비 이력"@ko ;
    rdfs:comment "정비 이력"@ko .

steel:hasMaintenanceHistory a owl:ObjectProperty ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range steel:MaintenanceHistory ;
    rdfs:label "has maintenance history"@en, "정비 이력"@ko ;
    rdfs:comment "설비의 정비 이력"@ko .
"""
        _, stats = improve_tbox(tbox)
        assert "bnode_skolemized" in stats
        assert "bnode_by_type" in stats
        assert isinstance(stats["bnode_skolemized"], int)
        assert isinstance(stats["bnode_by_type"], dict)

    def test_change_log_has_bnode_skolemization_step(self):
        """change_log에 bnode_skolemization 엔트리가 존재해야 함 (step 번호는 Q1~Q3로 바뀜)."""
        from tools.ontology_quality import improve_tbox

        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .
"""
        _, stats = improve_tbox(tbox)
        change_log = stats.get("change_log", [])
        # step 번호는 Q1~Q3 에서 label_synthesis(17) 삽입으로 하나씩 밀렸으므로 name 기준 매칭.
        bnode_entries = [e for e in change_log if e.get("name") == "bnode_skolemization"]
        assert len(bnode_entries) == 1
        assert bnode_entries[0]["step"] >= 18


class TestAllDisjointMembersCollision:
    """같은 멤버 집합을 가진 AllDisjointClasses 노드 병합 시 ``owl:members`` 중복.

    실측 (2026-08-18): 배포 T-Box 가 **HermiT 로드 자체를 거부**했다:

        java.lang.IllegalArgumentException: Error: Parsed DisjointClasses().
        A DisjointClasses axiom in OWL 2 DL must have at least two classes
        as parameters.

    원인은 skolemize 의 IRI 가 **멤버 이름만** 으로 해시된다는 점이다
    (``AllDisjoint_{md5(sorted member names)[:8]}``). 서로 다른 BNode 두 개가 같은
    멤버 집합을 가지면 같은 IRI 로 병합되는데, 각자의 ``owl:members`` 리스트가
    **둘 다** 그 IRI 에 붙는다. 둘째 리스트는 ``_replace`` 후 ``rdf:first`` 를 잃고
    rest-only 체인으로 남아, HermiT 가 "멤버 0개 DisjointClasses" 로 읽는다.

    실측 배포본에서 3개 노드가 이 상태였고 (``AllDisjoint_0e48f013`` /
    ``_34b55723`` / ``_9b1bea68``), 각각 정상 리스트 1개 + rest-only 체인 1개를
    갖고 있었다. S3 재실행은 이것을 **자기수리하지 못한다**.

    Restriction 경로는 같은 상황을 ``Restriction_merged_duplicates`` 로 정리하는데
    (``test_duplicate_bnodes_merged_into_single_uri``) AllDisjointClasses 경로에는
    그 정리가 없었다 — 병합 자체는 의도된 동작이므로 막지 말고, **잔여 리스트를
    정리** 해야 한다.
    """

    @staticmethod
    def _list_len(g, node) -> int:
        """``rdf:first`` 없는 노드에도 안전한 리스트 길이."""
        n = 0
        seen = set()
        while node is not None and node != RDF.nil and node not in seen:
            seen.add(node)
            if next(g.objects(node, RDF.first), None) is not None:
                n += 1
            node = next(g.objects(node, RDF.rest), None)
        return n

    def test_same_member_set_yields_single_members_list(self):
        """THE REGRESSION: 병합 후 ``owl:members`` 가 정확히 1개여야 한다."""
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        a, b = DOMAIN_NS_OBJ["ClsA"], DOMAIN_NS_OBJ["ClsB"]
        g.add((a, RDF.type, OWL.Class))
        g.add((b, RDF.type, OWL.Class))
        for _ in range(2):
            node = BNode()
            g.add((node, RDF.type, OWL.AllDisjointClasses))
            lst = BNode()
            Collection(g, lst, [a, b])
            g.add((node, OWL.members, lst))

        _skolemize_bnodes(g)

        named = [s for s in g.subjects(RDF.type, OWL.AllDisjointClasses)]
        assert len(named) == 1, f"병합되지 않았다: {named}"
        members = list(g.objects(named[0], OWL.members))
        assert len(members) == 1, (
            f"owl:members 가 {len(members)}개 — HermiT 가 빈 DisjointClasses 로 읽는다"
        )
        assert self._list_len(g, members[0]) == 2

    def test_no_rest_only_chain_survives(self):
        """``rdf:first`` 없는 rest-only 체인이 그래프에 남지 않는다.

        멤버 수만 보면 정상처럼 보이지만, 고아 리스트가 남아 있으면 다른 소비자가
        그것을 빈 컬렉션으로 읽는다 (실측 배포본의 형태다).
        """
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        a, b = DOMAIN_NS_OBJ["ClsA"], DOMAIN_NS_OBJ["ClsB"]
        g.add((a, RDF.type, OWL.Class))
        g.add((b, RDF.type, OWL.Class))
        for _ in range(3):
            node = BNode()
            g.add((node, RDF.type, OWL.AllDisjointClasses))
            lst = BNode()
            Collection(g, lst, [a, b])
            g.add((node, OWL.members, lst))

        _skolemize_bnodes(g)

        orphans = [
            s for s in set(g.subjects(RDF.rest, None))
            if next(g.objects(s, RDF.first), None) is None
        ]
        assert orphans == [], f"rest-only 체인이 남았다: {orphans}"

    def test_distinct_member_sets_stay_separate(self):
        """NEGATIVE: 멤버 집합이 다르면 별개 노드로 유지된다.

        중복 정리가 과해서 서로 다른 disjoint 공리를 합치면 의미가 바뀐다.
        """
        from tools.ontology_quality import _skolemize_bnodes

        g = _make_graph_with_prefixes()
        a, b, c = (DOMAIN_NS_OBJ["ClsA"], DOMAIN_NS_OBJ["ClsB"],
                   DOMAIN_NS_OBJ["ClsC"])
        for x in (a, b, c):
            g.add((x, RDF.type, OWL.Class))
        for members in ([a, b], [b, c]):
            node = BNode()
            g.add((node, RDF.type, OWL.AllDisjointClasses))
            lst = BNode()
            Collection(g, lst, members)
            g.add((node, OWL.members, lst))

        _skolemize_bnodes(g)

        named = list(g.subjects(RDF.type, OWL.AllDisjointClasses))
        assert len(named) == 2, f"서로 다른 disjoint 공리가 합쳐졌다: {named}"
        for n in named:
            assert len(list(g.objects(n, OWL.members))) == 1

    def test_deployed_tbox_has_no_members_collision(self):
        """실측 회귀: 배포 T-Box 에 이 결함이 없어야 한다.

        이것이 HermiT 로드를 막았고 S3 는 자기수리하지 못한다 — 산출물 수준에서
        고정한다. 파일이 없으면 skip.
        """
        import os

        from config import TBOX_PATH

        if not os.path.exists(TBOX_PATH):
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(TBOX_PATH, format="turtle")
        bad = {
            str(s).rsplit("#", 1)[-1]: len(list(g.objects(s, OWL.members)))
            for s in g.subjects(RDF.type, OWL.AllDisjointClasses)
            if len(list(g.objects(s, OWL.members))) > 1
        }
        assert bad == {}, (
            f"owl:members 가 2개 이상인 AllDisjointClasses: {bad} — "
            "HermiT 가 로드를 거부한다"
        )

    def test_step_01_cleans_pre_existing_collision(self):
        """``step_01`` 이 **이미 파일에 있는** 중복 리스트를 정리한다.

        skolemize 수정은 신규 발생만 막는다 — 배포 T-Box 에 이미 박힌 3개는
        S3 재실행으로도 사라지지 않았다(실측). 정리 스텝이 따로 필요하다.

        배포본 테스트(``test_deployed_tbox_has_no_members_collision``)는 이미
        정리된 파일을 읽으므로 이 스텝을 무력화해도 통과한다 — mutation 으로
        확인된 공백이라 동작 테스트를 별도로 둔다.
        """
        from tools.quality_steps import step_01_disjoint_complete as step
        from tools.quality_steps._base import StepContext

        g = _make_graph_with_prefixes()
        a, b = DOMAIN_NS_OBJ["ClsA"], DOMAIN_NS_OBJ["ClsB"]
        g.add((a, RDF.type, OWL.Class))
        g.add((b, RDF.type, OWL.Class))
        node = DOMAIN_NS_OBJ["AllDisjoint_deadbeef"]
        g.add((node, RDF.type, OWL.AllDisjointClasses))
        good = BNode()
        Collection(g, good, [a, b])
        g.add((node, OWL.members, good))
        # rest-only 체인 (rdf:first 없음) — 실측 배포본의 형태
        broken_head = BNode()
        tail = BNode()
        g.add((broken_head, RDF.rest, tail))
        g.add((tail, RDF.rest, RDF.nil))
        g.add((node, OWL.members, broken_head))

        step.apply(g, StepContext(domain_ns=DOMAIN_NS))

        remaining = [
            s for s in g.subjects(RDF.type, OWL.AllDisjointClasses)
            if len(list(g.objects(s, OWL.members))) > 1
        ]
        assert remaining == [], f"중복 리스트가 남았다: {remaining}"
        orphans = [
            s for s in set(g.subjects(RDF.rest, None))
            if next(g.objects(s, RDF.first), None) is None
        ]
        assert orphans == [], f"rest-only 체인이 남았다: {orphans}"
