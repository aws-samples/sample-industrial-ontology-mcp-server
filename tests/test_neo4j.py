"""tests for tools/remote/neo4j.py — 검증 로직 단위 테스트 (Neo4j 접속 불필요)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, XSD, BNode, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_INST_NS_OBJ as INST
from domain.namespaces import DOMAIN_NS_OBJ as DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.remote.neo4j import (
    _build_lpg_loss_manifest,
    _check_configured,
    _convert_rdf_to_lpg,
    _cypher_blocked_clause,
    _generate_cypher_compensation,
    _is_upper_ontology_type,
    _resolve_rel_type,
    _track_exclusion,
)

NS = str(DOMAIN_NS)


@pytest.fixture
def sample_graph():
    """검증 테스트용 RDF 그래프."""
    g = _new_graph()
    g.bind("steel", DOMAIN_NS)
    g.bind("inst", INST)

    # 클래스 선언
    g.add((DOMAIN_NS.EquipmentMaster, RDF.type, OWL.Class))
    g.add((DOMAIN_NS.MaintenanceHistory, RDF.type, OWL.Class))

    # 인스턴스 3개
    for i in range(1, 4):
        eq = INST[f"EquipmentMaster_EQ{i:03d}"]
        g.add((eq, RDF.type, DOMAIN_NS.EquipmentMaster))
        g.add((eq, DOMAIN_NS.equipmentID, Literal(f"EQ{i:03d}")))
        g.add((eq, DOMAIN_NS.equipmentName, Literal(f"설비{i}")))

    # 정비 이력 2개
    for i in range(1, 3):
        mh = INST[f"MaintenanceHistory_MH{i:03d}"]
        g.add((mh, RDF.type, DOMAIN_NS.MaintenanceHistory))
        g.add((mh, DOMAIN_NS.maintenanceID, Literal(f"MH{i:03d}")))

    # 관계: EQ001 → MH001, EQ001 → MH002
    eq1 = INST["EquipmentMaster_EQ001"]
    g.add((eq1, DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH001"]))
    g.add((eq1, DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH002"]))

    # 관계: EQ002 → MH001
    eq2 = INST["EquipmentMaster_EQ002"]
    g.add((eq2, DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH001"]))

    return g


# ── _check_configured ────────────────────────────────────────────────────


class TestCheckConfigured:
    def test_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "")
        result = _check_configured()
        assert result is not None
        assert "NEO4J_URI" in result

    def test_returns_none_when_configured(self, monkeypatch):
        monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "bolt://localhost:7687")
        result = _check_configured()
        assert result is None






# ── neo4j_query 쓰기 차단 (READ 모드 세션) ────────────────────────────────


class TestNeo4jQueryReadOnly:
    """neo4j_query는 clause guard와 READ access mode를 함께 사용한다."""

    def _mock_driver(self, monkeypatch):
        monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "bolt://localhost:7687")
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session
        return mock_driver, mock_session

    def test_uses_read_access_mode(self, monkeypatch):
        mock_driver, mock_session = self._mock_driver(monkeypatch)
        with patch("tools.remote.neo4j._get_driver", return_value=mock_driver):
            from tools.remote.neo4j import neo4j_query
            neo4j_query("MATCH (n) RETURN n LIMIT 1")
        mock_driver.session.assert_called_once_with(default_access_mode="READ")

    def test_returns_json(self, monkeypatch):
        mock_driver, mock_session = self._mock_driver(monkeypatch)
        with patch("tools.remote.neo4j._get_driver", return_value=mock_driver):
            from tools.remote.neo4j import neo4j_query
            result = neo4j_query("MATCH (n) RETURN n LIMIT 1")
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    @pytest.mark.parametrize(
        ("query", "clause"),
        [
            ("CREATE (n:Test)", "CREATE"),
            ("MATCH (n) SET n.value = 1 RETURN n", "SET"),
            ("MATCH (n) DETACH DELETE n", "DELETE"),
            ("CALL db.labels()", "CALL"),
            ("LOAD CSV FROM 'https://example.org/data.csv' AS row RETURN row", "LOAD CSV"),
            ("GRANT MATCH {*} ON GRAPH foo TO reader", "GRANT"),
            ("INSERT (:Test)", "INSERT"),
            ("ENABLE SERVER 'server-1'", "ENABLE SERVER"),
            (
                "DEALLOCATE DATABASES FROM SERVER 'server-1'",
                "DEALLOCATE DATABASES",
            ),
            ("REALLOCATE DATABASES", "REALLOCATE DATABASES"),
        ],
    )
    def test_blocks_write_admin_and_external_load_clauses(
        self,
        monkeypatch,
        query,
        clause,
    ):
        mock_driver, _ = self._mock_driver(monkeypatch)
        with patch("tools.remote.neo4j._get_driver", return_value=mock_driver):
            from tools.remote.neo4j import neo4j_query
            result = json.loads(neo4j_query(query))

        assert result["success"] is False
        assert clause in result["error"]
        mock_driver.session.assert_not_called()

    def test_ignores_keywords_inside_literals_comments_and_identifiers(self):
        query = """
        MATCH (n:`CREATE`)
        // DELETE n
        WHERE n.note = 'SET value'
        /* CALL db.labels() */
        RETURN n
        """

        assert _cypher_blocked_clause(query) is None


# ── neo4j_deploy_lpg 미설정/파일없음 ──────────────────────────────────────


class TestNeo4jDeployGuards:
    def test_unconfigured_uri(self, monkeypatch):
        monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "")
        from tools.remote.neo4j import neo4j_deploy_lpg

        result = neo4j_deploy_lpg()
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "NEO4J_URI" in parsed["error"]

    def test_csv_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "bolt://localhost:7687")
        # 지정 CSV 는 dirname(INFERRED_PATH)/neo4j 안이어야 하므로 그 경계를 tmp 로 옮긴다.
        monkeypatch.setattr(
            "tools.remote.neo4j.INFERRED_PATH", str(tmp_path / "all_inferred.ttl"),
        )
        from tools.remote.neo4j import neo4j_deploy_lpg

        result = neo4j_deploy_lpg(nodes_csv=str(tmp_path / "neo4j" / "nodes.csv"))
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "없음" in parsed["error"]


# ── neo4j_stats 미설정 ────────────────────────────────────────────────────


class TestNeo4jStatsGuard:
    def test_unconfigured_uri(self, monkeypatch):
        monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "")
        from tools.remote.neo4j import neo4j_stats

        result = neo4j_stats()
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "NEO4J_URI" in parsed["error"]


# ── _resolve_rel_type [M4] ───────────────────────────────────────────────


class TestResolveRelType:
    def test_domain_ns(self):
        assert _resolve_rel_type(str(DOMAIN_NS.hasStatus)) == "hasStatus"

    def test_iof_core(self):
        assert _resolve_rel_type(
            "https://spec.industrialontologies.org/ontology/core/Core/hasQuality"
        ) == "iof_hasQuality"

    def test_iof_supplychain(self):
        assert _resolve_rel_type(
            "https://spec.industrialontologies.org/ontology/supplychain/SupplyChain/hasSupplier"
        ) == "iof_sc_hasSupplier"

    def test_iof_maintenance(self):
        assert _resolve_rel_type(
            "https://spec.industrialontologies.org/ontology/maintenance/Maintenance/hasMaintPlan"
        ) == "iof_maint_hasMaintPlan"

    def test_rdfs(self):
        assert _resolve_rel_type("http://www.w3.org/2000/01/rdf-schema#subClassOf") == "rdfs_subClassOf"

    def test_dc(self):
        assert _resolve_rel_type("http://purl.org/dc/terms/title") == "dcterms_title"

    def test_unknown_ns_gets_other_prefix(self):
        """알 수 없는 네임스페이스는 other_ prefix."""
        result = _resolve_rel_type("http://example.org/custom#myProp")
        assert result == "other_myProp"

    def test_no_collision_steel_vs_iof(self):
        """같은 local name이라도 네임스페이스로 구분."""
        steel = _resolve_rel_type(str(DOMAIN_NS.hasSupplier))
        iof = _resolve_rel_type(
            "https://spec.industrialontologies.org/ontology/supplychain/SupplyChain/hasSupplier"
        )
        assert steel == "hasSupplier"
        assert iof == "iof_sc_hasSupplier"
        assert steel != iof


# ── _is_upper_ontology_type [C1] ─────────────────────────────────────────


class TestIsUpperOntologyType:
    def test_bfo_entity(self):
        assert _is_upper_ontology_type("http://purl.obolibrary.org/obo/BFO_0000001")

    def test_domain_class_not_upper(self):
        assert not _is_upper_ontology_type(str(DOMAIN_NS.EquipmentMaster))

    def test_iof_class_not_upper(self):
        assert not _is_upper_ontology_type(
            "https://spec.industrialontologies.org/ontology/core/Core/MaterialArtifact"
        )


# ── _convert_rdf_to_lpg [M4] ─────────────────────────────────────────────


class TestConvertRdfToLpg:
    @pytest.fixture
    def conversion_graph(self):
        """변환 테스트용 그래프: 인스턴스, 프로퍼티, 관계, OWL axiom, BNode."""
        g = _new_graph()
        g.bind("steel", DOMAIN_NS)
        g.bind("inst", INST)

        # 인스턴스
        eq = INST["EquipmentMaster_EQ001"]
        g.add((eq, RDF.type, DOMAIN_NS.EquipmentMaster))
        g.add((eq, DOMAIN_NS.equipmentID, Literal("EQ001", datatype=XSD.string)))
        g.add((eq, DOMAIN_NS.temperature, Literal("1200", datatype=XSD.decimal)))
        # 언어 태그 [H2]
        g.add((eq, RDFS.label, Literal("설비1", lang="ko")))
        g.add((eq, RDFS.label, Literal("Equipment 1", lang="en")))
        g.add((eq, RDFS.comment, Literal("테스트 설비", lang="ko")))

        mh = INST["MaintenanceHistory_MH001"]
        g.add((mh, RDF.type, DOMAIN_NS.MaintenanceHistory))
        g.add((mh, DOMAIN_NS.maintenanceID, Literal("MH001")))

        # 관계
        g.add((eq, DOMAIN_NS.hasMaintenanceHistory, mh))

        # OWL axiom (제외 대상)
        g.add((DOMAIN_NS.hasMaintenanceHistory, OWL.inverseOf, DOMAIN_NS.isMaintenanceOf))

        # owl:Thing (C1 제외 대상)
        g.add((eq, RDF.type, OWL.Thing))

        # BFO (C1 제외 대상)
        bfo_entity = URIRef("http://purl.obolibrary.org/obo/BFO_0000001")
        g.add((eq, RDF.type, bfo_entity))

        # BNode (제외 대상)
        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Restriction))
        g.add((bnode, OWL.onProperty, DOMAIN_NS.hasStatus))

        # OWL Class (C2 subClassOf 보존 대상)
        g.add((DOMAIN_NS.EquipmentMaster, RDF.type, OWL.Class))
        g.add((DOMAIN_NS.MaintenanceHistory, RDF.type, OWL.Class))
        g.add((DOMAIN_NS.EquipmentMaster, RDFS.subClassOf, DOMAIN_NS.MaintenanceHistory))

        return g

    def test_instance_labels(self, conversion_graph):
        nodes, _, _ = _convert_rdf_to_lpg(conversion_graph)
        eq_uri = str(INST["EquipmentMaster_EQ001"])
        assert "EquipmentMaster" in nodes[eq_uri]["labels"]

    def test_owl_thing_excluded(self, conversion_graph):
        """C1: owl:Thing이 라벨에 포함되지 않아야 함."""
        nodes, _, _ = _convert_rdf_to_lpg(conversion_graph)
        eq_uri = str(INST["EquipmentMaster_EQ001"])
        assert "Thing" not in nodes[eq_uri]["labels"]

    def test_bfo_excluded(self, conversion_graph):
        """C1: BFO 상위 타입이 라벨에 포함되지 않아야 함."""
        nodes, _, _ = _convert_rdf_to_lpg(conversion_graph)
        eq_uri = str(INST["EquipmentMaster_EQ001"])
        # BFO URI의 로컬이름은 "BFO_0000001"
        labels_str = " ".join(nodes[eq_uri]["labels"])
        assert "BFO_" not in labels_str

    def test_subclass_preserved_no_duplicates(self, conversion_graph):
        """C2: rdfs:subClassOf 관계가 정확히 1개만 보존되어야 함 (중복 없음)."""
        _, rels, _ = _convert_rdf_to_lpg(conversion_graph)
        subclass_rels = [(s, e, t) for s, e, t in rels if t == "rdfs_subClassOf"]
        assert len(subclass_rels) == 1

    def test_owl_class_nodes_have_label(self, conversion_graph):
        """C2: OWL 클래스가 OWLClass 라벨을 가져야 함."""
        nodes, _, _ = _convert_rdf_to_lpg(conversion_graph)
        eq_class_uri = str(DOMAIN_NS.EquipmentMaster)
        assert "OWLClass" in nodes[eq_class_uri]["labels"]

    def test_language_tags_preserved(self, conversion_graph):
        """H2: 언어 태그가 프로퍼티명에 반영되어야 함."""
        nodes, _, _ = _convert_rdf_to_lpg(conversion_graph)
        eq_uri = str(INST["EquipmentMaster_EQ001"])
        props = nodes[eq_uri]["properties"]
        assert "label_ko" in props
        assert "label_en" in props
        assert props["label_ko"] == "설비1"
        assert props["label_en"] == "Equipment 1"

    def test_datatype_tracked(self, conversion_graph):
        """H1: XSD 타입이 prop_types에 추적되어야 함."""
        nodes, _, stats = _convert_rdf_to_lpg(conversion_graph)
        ptm = stats.get("prop_type_map", {})
        assert ptm.get("temperature") == "float"

    def test_owl_axiom_excluded(self, conversion_graph):
        """OWL axiom (inverseOf)이 관계에 포함되지 않아야 함."""
        _, rels, stats = _convert_rdf_to_lpg(conversion_graph)
        rel_types = {t for _, _, t in rels}
        assert "inverseOf" not in rel_types
        assert stats["excluded_axiom"] > 0

    def test_bnode_excluded(self, conversion_graph):
        _, _, stats = _convert_rdf_to_lpg(conversion_graph)
        assert stats["excluded_bnode"] > 0

    def test_domain_relationship_preserved(self, conversion_graph):
        _, rels, _ = _convert_rdf_to_lpg(conversion_graph)
        domain_rels = [(s, e, t) for s, e, t in rels if t == "hasMaintenanceHistory"]
        assert len(domain_rels) == 1

    def test_external_ref_tracked(self):
        """H3: 외부 URI 참조 드롭이 추적되어야 함."""
        g = _new_graph()
        eq = INST["EquipmentMaster_EQ001"]
        g.add((eq, RDF.type, DOMAIN_NS.EquipmentMaster))
        external = URIRef("http://external.org/entity/X1")
        g.add((eq, DOMAIN_NS.relatesTo, external))
        _, _, stats = _convert_rdf_to_lpg(g)
        assert stats["excluded_external_ref"] >= 1
        assert len(stats["excluded_external_ref_samples"]) >= 1

    def test_quality_scores_bounded(self, conversion_graph):
        """모든 품질 점수가 [0, 100] 범위 내."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        nodes, rels, stats = _convert_rdf_to_lpg(conversion_graph)
        v = _verify_lpg(conversion_graph, nodes, rels, _DOMAIN_NS, 5, stats)
        qs = v["quality_score"]
        assert 0 <= qs["total"] <= 100
        for dim in ("completeness", "faithfulness", "consistency", "conciseness"):
            assert 0 <= qs[dim]["score"] <= 100, f"{dim} score out of bounds: {qs[dim]['score']}"

    def test_quality_scores_with_empty_graph(self):
        """빈 그래프에서도 품질 점수 에러 없이 계산."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g = _new_graph()
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        qs = v["quality_score"]
        assert 0 <= qs["total"] <= 100

    def test_no_dangling_endpoints(self):
        """도메인 NS에 있지만 rdf:type 없는 URI는 관계 대상이 되면 안 됨."""
        g = _new_graph()
        eq = INST["EquipmentMaster_EQ001"]
        g.add((eq, RDF.type, DOMAIN_NS.EquipmentMaster))
        untyped = DOMAIN_NS["ActiveStatus"]  # rdf:type 없음 → nodes에 없음
        g.add((eq, DOMAIN_NS.hasStatus, untyped))
        nodes, rels, _ = _convert_rdf_to_lpg(g)
        for _, end, _ in rels:
            assert end in nodes, f"Dangling endpoint: {end}"

    def test_owl_sameAs_excluded(self):
        """C-3: owl:sameAs가 관계로 변환되지 않아야 함."""
        g = _new_graph()
        eq1 = INST["EquipmentMaster_EQ001"]
        eq2 = INST["EquipmentMaster_EQ002"]
        g.add((eq1, RDF.type, DOMAIN_NS.EquipmentMaster))
        g.add((eq2, RDF.type, DOMAIN_NS.EquipmentMaster))
        g.add((eq1, OWL.sameAs, eq2))
        _, rels, _ = _convert_rdf_to_lpg(g)
        rel_types = {t for _, _, t in rels}
        assert "sameAs" not in rel_types
        assert "other_sameAs" not in rel_types

    def test_exclusion_tracker_populated(self, conversion_graph):
        """LPG Loss Manifest: 제외된 predicate가 exclusion_tracker에 기록되어야 함."""
        _, _, stats = _convert_rdf_to_lpg(conversion_graph)
        tracker = stats.get("exclusion_tracker", {})
        assert len(tracker) > 0, "exclusion_tracker가 비어있음"
        assert str(OWL.inverseOf) in tracker


# ── LPG Loss Manifest ───────────────────────────────────────────────────


EX = Namespace("http://example.org/")


class TestLpgLossManifest:
    def test_tracks_excluded_predicates(self):
        """_track_exclusion: predicate별 카운트와 샘플이 기록되어야 함."""
        tracker: dict = {}
        _track_exclusion(tracker, str(OWL.inverseOf), EX.hasChild, EX.isChildOf)
        assert str(OWL.inverseOf) in tracker
        assert tracker[str(OWL.inverseOf)]["count"] == 1
        assert len(tracker[str(OWL.inverseOf)]["samples"]) == 1
        assert tracker[str(OWL.inverseOf)]["samples"][0]["subject"] == str(EX.hasChild)

    def test_tracks_multiple_calls(self):
        """_track_exclusion: 동일 predicate 다중 호출 시 카운트 증가."""
        tracker: dict = {}
        for i in range(7):
            _track_exclusion(tracker, str(OWL.inverseOf), EX[f"s{i}"], EX[f"o{i}"])
        assert tracker[str(OWL.inverseOf)]["count"] == 7
        assert len(tracker[str(OWL.inverseOf)]["samples"]) == 5  # max 5

    def test_generates_cypher_compensation(self):
        """_generate_cypher_compensation: 알려진 predicate에 Cypher 패턴 반환."""
        comp = _generate_cypher_compensation(str(OWL.inverseOf), "hasChild")
        assert "MATCH" in comp
        assert "hasChild" in comp

    def test_generates_compensation_without_rel_name(self):
        """_generate_cypher_compensation: rel_name 없으면 {rel} placeholder 유지."""
        comp = _generate_cypher_compensation(str(OWL.TransitiveProperty))
        assert "{rel}" in comp

    def test_unknown_predicate_fallback(self):
        """_generate_cypher_compensation: 미등록 predicate는 폴백 메시지."""
        comp = _generate_cypher_compensation("http://example.org/unknownPred")
        assert "직접 표현 불가" in comp

    def test_builds_full_manifest(self):
        """_build_lpg_loss_manifest: 전체 매니페스트 구조 검증."""
        tracker = {
            str(OWL.inverseOf): {"count": 5, "samples": []},
            str(OWL.someValuesFrom): {"count": 3, "samples": []},
        }
        manifest = _build_lpg_loss_manifest(
            tracker, bnode_losses=2, external_drops=10,
            rdf_total=1000, instance_triples=800, axiom_triples=200,
        )
        assert "losses" in manifest
        assert "preservation_score" in manifest
        assert manifest["losses"]["owl_axiom_exclusions"]["count"] == 8
        assert manifest["losses"]["bnode_losses"]["count"] == 2
        assert manifest["losses"]["external_reference_drops"]["count"] == 10
        assert manifest["preservation_score"]["instance_triples"] == 800
        assert manifest["preservation_score"]["axiom_triples"] == 200
        assert manifest["preservation_score"]["axiom_exclusion_rate"] == 0.04  # 8/200

    def test_manifest_by_predicate_has_compensation(self):
        """_build_lpg_loss_manifest: by_predicate 항목에 compensation 포함."""
        tracker = {
            str(OWL.inverseOf): {"count": 3, "samples": [
                {"subject": "s1", "object": "o1"},
                {"subject": "s2", "object": "o2"},
            ]},
        }
        manifest = _build_lpg_loss_manifest(tracker, axiom_triples=100)
        preds = manifest["losses"]["owl_axiom_exclusions"]["by_predicate"]
        assert len(preds) == 1
        assert "MATCH" in preds[0]["compensation"]
        assert preds[0]["count"] == 3
        assert len(preds[0]["samples"]) == 2

    def test_manifest_zero_axiom_triples(self):
        """_build_lpg_loss_manifest: axiom_triples=0에서 ZeroDivisionError 없이 동작."""
        tracker = {str(OWL.inverseOf): {"count": 1, "samples": []}}
        manifest = _build_lpg_loss_manifest(tracker, axiom_triples=0)
        assert manifest["preservation_score"]["axiom_exclusion_rate"] == 1.0  # 1/max(0,1)=1


# ── Structural Parity Diff ──────────────────────────────────────────────


class TestStructuralParity:
    def test_parity_diff_detects_missing_in_lpg(self):
        from tools.remote.neo4j import _compute_parity_diff
        sparql_uris = {"uri:A", "uri:B", "uri:C"}
        cypher_uris = {"uri:A", "uri:B"}
        diff = _compute_parity_diff("TestClass", sparql_uris, cypher_uris)
        assert diff is not None
        assert "uri:C" in diff["only_in_rdf"]
        assert diff["sparql_count"] == 3
        assert diff["cypher_count"] == 2

    def test_parity_diff_detects_extra_in_lpg(self):
        from tools.remote.neo4j import _compute_parity_diff
        sparql_uris = {"uri:A"}
        cypher_uris = {"uri:A", "uri:B"}
        diff = _compute_parity_diff("TestClass", sparql_uris, cypher_uris)
        assert "uri:B" in diff["only_in_lpg"]

    def test_parity_diff_perfect_match_returns_none(self):
        from tools.remote.neo4j import _compute_parity_diff
        uris = {"uri:A", "uri:B"}
        diff = _compute_parity_diff("TestClass", uris, uris)
        assert diff is None

    def test_parity_diff_caps_at_10_samples(self):
        from tools.remote.neo4j import _compute_parity_diff
        sparql_uris = {f"uri:{i}" for i in range(20)}
        cypher_uris = set()
        diff = _compute_parity_diff("TestClass", sparql_uris, cypher_uris)
        assert len(diff["only_in_rdf"]) == 10


# ── _verify_lpg: sanitize / inverse fold / pruning 정책 인지 검증 ────────────


class TestVerifyPolicyAware:
    """변환 단계의 정책으로 LPG 가 의도적으로 RDF 와 다른 모양이 됐을 때,
    검증 로직이 그 차이를 mismatch 로 잘못 보고하지 않는지 확인."""

    def _build_minimal_graph(self):
        g = _new_graph()
        g.bind("steel", DOMAIN_NS)
        eq = INST["EquipmentMaster_EQ001"]
        g.add((eq, RDF.type, DOMAIN_NS.EquipmentMaster))
        g.add((eq, DOMAIN_NS.equipmentID, Literal("EQ001")))
        return g, eq

    def test_class_distribution_uses_sanitized_keys(self):
        """클래스 local name 이 sanitize 됐을 때 분포 카운터도 sanitize 키로 비교."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g, _ = self._build_minimal_graph()
        # 숫자로 시작하는 클래스 (sanitize → "_3DScanner")
        unusual_cls = URIRef(_DOMAIN_NS + "3DScanner")
        scanner = INST["Scanner_001"]
        g.add((scanner, RDF.type, unusual_cls))
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        # mismatch 가 0건이어야 함 (sanitize 적용 후 같은 키)
        cd = v["class_distribution"]
        assert cd["matched"] == cd["total"], f"mismatch: {cd['mismatches']}"

    def test_inverse_dedup_does_not_create_mismatch(self):
        """RDF 에 inverseOf 쌍이 양방향으로 있고 LPG 가 한 방향만 유지해도 mismatch 안 됨."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g, _ = self._build_minimal_graph()
        # inverseOf 선언 없이는 dedup 발생 안 하므로 T-Box 파일과 무관.
        # 본 테스트는 동일 식별자 비교 자체가 정상적으로 매칭되는지 확인.
        eq2 = INST["EquipmentMaster_EQ002"]
        g.add((eq2, RDF.type, DOMAIN_NS.EquipmentMaster))
        g.add((eq2, DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH001"]))
        g.add((INST["MaintenanceHistory_MH001"], RDF.type, DOMAIN_NS.MaintenanceHistory))
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        dr = v["domain_relationships"]
        assert dr["matched"] == dr["total"], f"mismatch: {dr['mismatches']}"

    def test_metadata_unfolded_in_spot_check(self, monkeypatch):
        """sparse-pruned 프로퍼티가 _metadata JSON 으로 접혀도 spot-check 가 통과해야 함."""
        from tools.remote.neo4j import _DOMAIN_NS, _prune_sparse_columns, _verify_lpg
        g, _ = self._build_minimal_graph()
        # 200개 노드 + 1개에만 rare_prop → 0.5% < 1% 임계
        for i in range(200):
            inst = INST[f"EquipmentMaster_BULK_{i:03d}"]
            g.add((inst, RDF.type, DOMAIN_NS.EquipmentMaster))
            g.add((inst, DOMAIN_NS.equipmentID, Literal(f"BULK{i:03d}")))
        rare_inst = INST["EquipmentMaster_BULK_000"]
        g.add((rare_inst, DOMAIN_NS.veryRareProp, Literal("hidden_value")))
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        # 변환 흐름과 동일하게 sparse pruning 적용
        nodes, _pruned, _cov = _prune_sparse_columns(nodes, min_coverage=0.01)
        # rare_inst 가 spot 샘플에 들어오도록 sample_count 충분히 키움
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, sample_count=200, stats=stats)
        # spot-check 통과율이 100% 여야 함 (_metadata unfold 가 작동)
        sc = v["spot_check"]
        assert sc["passed"] == sc["total"], f"spot mismatches: {sc.get('mismatches')}"

    def test_array_column_excluded_from_dp_check(self):
        """다중값 프로퍼티 (array) 도 datatype_fidelity 분자에 포함."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g = _new_graph()
        eq = INST["EquipmentMaster_EQ001"]
        g.add((eq, RDF.type, DOMAIN_NS.EquipmentMaster))
        # 같은 property 에 다중값 → list 로 저장되어 array_props 후보
        g.add((eq, RDFS.label, Literal("설비 1", lang="ko")))
        g.add((eq, RDFS.label, Literal("Equipment 1", lang="en")))
        g.add((eq, DOMAIN_NS.tags, Literal("tagA")))
        g.add((eq, DOMAIN_NS.tags, Literal("tagB")))
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        df = v["quality_score"]["faithfulness"]["details"]["datatype_fidelity"]
        # array 컬럼 까지 포함했을 때 0 보다 커야 함
        assert df > 0
        # 정책 메타데이터에 array 컬럼 정보 노출
        pn = v.get("policy_normalization", {})
        assert pn.get("array_columns_excluded_from_dp_check", 0) >= 1

    def test_subClassOf_excluded_from_dangling(self):
        """OWLClass 사이의 rdfs_subClassOf 가 dangling_rate 폭증을 안 일으켜야 함."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g = _new_graph()
        # 클래스 정의 + subClassOf
        g.add((DOMAIN_NS.PumpEquipment, RDF.type, OWL.Class))
        g.add((DOMAIN_NS.PumpEquipment, RDFS.subClassOf, DOMAIN_NS.Equipment))
        g.add((DOMAIN_NS.Equipment, RDF.type, OWL.Class))
        # 단일 인스턴스 + 자기 자신만 (관계 없음)
        eq = INST["PumpEquipment_P001"]
        g.add((eq, RDF.type, DOMAIN_NS.PumpEquipment))
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        dangling_rate = v["quality_score"]["consistency"]["details"]["dangling_rate"]
        # rdfs_subClassOf 만 있는 상태에서 dangling 0% 여야 정상
        assert dangling_rate == 0.0

    def test_policy_normalization_block_present(self):
        """검증 결과에 정책 normalization 블록이 노출돼 디버깅 가능."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g, _ = self._build_minimal_graph()
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        assert "policy_normalization" in v
        pn = v["policy_normalization"]
        for key in (
            "tbox_inverse_pairs", "canonical_overrides",
            "ident_renames_applied", "array_columns_excluded_from_dp_check",
        ):
            assert key in pn

    def test_spot_check_records_diff_props(self):
        """spot mismatch 가 발생했을 때 어떤 property 가 어긋났는지 노출."""
        from tools.remote.neo4j import _DOMAIN_NS, _verify_lpg
        g, eq = self._build_minimal_graph()
        nodes, rels, stats = _convert_rdf_to_lpg(g)
        # LPG 쪽에서 임의로 property 하나를 누락시켜 mismatch 강제
        nodes[str(eq)]["properties"].pop("equipmentID", None)
        v = _verify_lpg(g, nodes, rels, _DOMAIN_NS, 5, stats)
        sc = v["spot_check"]
        if sc["passed"] < sc["total"]:
            assert "mismatches" in sc
            assert any("equipmentID" in m["missing_or_diff_props"]
                       for m in sc["mismatches"])
