"""Tests for tools/kg_validation.py — KG 데이터 품질 검증 도구 테스트."""

import json
from unittest.mock import patch

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph

# ── 헬퍼 ──────────────────────────────────────────


def _make_tbox_graph():
    """테스트용 T-Box 그래프 생성."""
    g = _new_graph()
    ns = DOMAIN_NS
    eq = URIRef(f"{ns}EquipmentMaster")
    st = URIRef(f"{ns}EquipmentStatus")
    g.add((eq, RDF.type, OWL.Class))
    g.add((st, RDF.type, OWL.Class))

    has_status = URIRef(f"{ns}hasEquipmentStatus")
    g.add((has_status, RDF.type, OWL.ObjectProperty))
    g.add((has_status, RDFS.domain, eq))
    g.add((has_status, RDFS.range, st))

    inv_prop = URIRef(f"{ns}isEquipmentStatusOf")
    g.add((inv_prop, RDF.type, OWL.ObjectProperty))
    g.add((inv_prop, RDFS.domain, st))
    g.add((inv_prop, RDFS.range, eq))
    g.add((has_status, OWL.inverseOf, inv_prop))
    g.add((inv_prop, OWL.inverseOf, has_status))

    eid = URIRef(f"{ns}equipmentID")
    g.add((eid, RDF.type, OWL.DatatypeProperty))
    g.add((eid, RDFS.domain, eq))
    return g


def _make_abox_graph(tbox: Graph):
    """T-Box를 포함한 A-Box 그래프 생성."""
    g = _new_graph()
    for s, p, o in tbox:
        g.add((s, p, o))

    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS
    eq_inst = URIRef(f"{inst}EquipmentMaster_EQ001")
    st_inst = URIRef(f"{inst}EquipmentStatus_EQ001")
    g.add((eq_inst, RDF.type, URIRef(f"{ns}EquipmentMaster")))
    g.add((st_inst, RDF.type, URIRef(f"{ns}EquipmentStatus")))
    g.add((eq_inst, URIRef(f"{ns}hasEquipmentStatus"), st_inst))
    g.add((st_inst, URIRef(f"{ns}isEquipmentStatusOf"), eq_inst))
    g.add((eq_inst, URIRef(f"{ns}equipmentID"), Literal("EQ001")))
    return g


# ── _validate_prop_name ───────────────────────────


class TestValidatePropName:
    """프로퍼티명 안전성 검사."""

    def test_valid_names(self):
        from tools.kg_validation import _validate_prop_name

        assert _validate_prop_name("hasEquipment") == "hasEquipment"
        assert _validate_prop_name("equipmentID") == "equipmentID"
        assert _validate_prop_name("A") == "A"

    @pytest.mark.parametrize("bad_name", [
        "123bad", "", " space", "has-dash", "has.dot", "한글",
    ])
    def test_invalid_names(self, bad_name):
        from tools.kg_validation import _validate_prop_name

        with pytest.raises(ValueError, match="Invalid property name"):
            _validate_prop_name(bad_name)


# ── 개별 검증 함수 ────────────────────────────────


class TestCheckProcessFlow:
    """공정 흐름 체인 검증."""

    def test_no_process_flow_auto_pass(self):
        """ManufacturingProcessStep/followedBy 없으면 자동 통과."""
        from tools.kg_validation import _check_process_flow

        g = _new_graph()
        result = _check_process_flow(g)
        assert result["passed"] is True
        assert result["steps"] == 0

    def test_with_process_flow(self):
        """followedBy 체인이 있으면 steps/links 집계."""
        from tools.kg_validation import _check_process_flow

        g = _new_graph()
        ns = DOMAIN_NS
        step_cls = URIRef(f"{ns}ManufacturingProcessStep")
        a = URIRef(f"{DOMAIN_INST_NS}Step_A")
        b = URIRef(f"{DOMAIN_INST_NS}Step_B")
        g.add((a, RDF.type, step_cls))
        g.add((b, RDF.type, step_cls))
        g.add((a, URIRef(f"{ns}followedBy"), b))
        # SPARQL prefix 필요
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        result = _check_process_flow(g)
        assert result["steps"] == 2
        assert result["links"] == 1
        assert result["passed"] is True


class TestCheckOrphanNodes:
    """고아 노드 탐지 검증."""

    def test_no_orphans(self):
        """정상 연결된 인스턴스 — 고아 없음."""
        from tools.kg_validation import _check_orphan_nodes

        tbox = _make_tbox_graph()
        g = _make_abox_graph(tbox)
        result = _check_orphan_nodes(g)
        assert result["passed"] is True
        assert result["orphan_count"] == 0

    def test_orphan_detected(self):
        """프로퍼티 연결 없는 인스턴스 → 고아 탐지."""
        from tools.kg_validation import _check_orphan_nodes

        g = _new_graph()
        ns = DOMAIN_NS
        orphan = URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_Orphan")
        g.add((orphan, RDF.type, URIRef(f"{ns}EquipmentMaster")))
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        result = _check_orphan_nodes(g)
        assert result["passed"] is False
        assert result["orphan_count"] >= 1


class TestCheckBidirectionalOp:
    """ObjectProperty 양방향 연결 검증."""

    def test_both_directions_present(self):
        """정방향/역방향 모두 존재 — 통과."""
        from tools.kg_validation import _check_bidirectional_op

        tbox = _make_tbox_graph()
        g = _make_abox_graph(tbox)
        result = _check_bidirectional_op(g, tbox)
        assert result["passed"] is True
        assert len(result["missing"]) == 0

    def test_missing_inverse_detected(self):
        """역방향 트리플 없음 → missing 탐지."""
        from tools.kg_validation import _check_bidirectional_op

        tbox = _make_tbox_graph()
        g = _make_abox_graph(tbox)
        # 역방향 트리플 제거
        inv_prop = URIRef(f"{DOMAIN_NS}isEquipmentStatusOf")
        for s, p, o in list(g.triples((None, inv_prop, None))):
            g.remove((s, p, o))

        result = _check_bidirectional_op(g, tbox)
        assert result["passed"] is False
        assert len(result["missing"]) >= 1


class TestBidirectionalFull:
    """Full-population set-diff 기반 양방향 검증."""

    def test_detects_missing_inverse_triple(self):
        """정방향은 있는데 역방향 없는 쌍을 감지."""
        from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
        from tools.kg_validation import _check_bidirectional_op

        tbox = _new_graph()
        tbox.add((DOMAIN_NS_OBJ.hasChild, RDF.type, OWL.ObjectProperty))
        tbox.add((DOMAIN_NS_OBJ.isChildOf, RDF.type, OWL.ObjectProperty))
        tbox.add((DOMAIN_NS_OBJ.hasChild, OWL.inverseOf, DOMAIN_NS_OBJ.isChildOf))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)
        g.add((DOMAIN_INST_NS_OBJ.A, DOMAIN_NS_OBJ.hasChild, DOMAIN_INST_NS_OBJ.B))
        # Missing: DOMAIN_INST_NS_OBJ.B isChildOf DOMAIN_INST_NS_OBJ.A

        result = _check_bidirectional_op(g, tbox)
        assert result["passed"] is False
        assert result.get("missing_inverse_count", 0) > 0

    def test_passes_when_all_inverses_present(self):
        """정방향/역방향 모두 존재하면 통과."""
        from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
        from tools.kg_validation import _check_bidirectional_op

        tbox = _new_graph()
        tbox.add((DOMAIN_NS_OBJ.hasChild, RDF.type, OWL.ObjectProperty))
        tbox.add((DOMAIN_NS_OBJ.isChildOf, RDF.type, OWL.ObjectProperty))
        tbox.add((DOMAIN_NS_OBJ.hasChild, OWL.inverseOf, DOMAIN_NS_OBJ.isChildOf))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)
        g.add((DOMAIN_INST_NS_OBJ.A, DOMAIN_NS_OBJ.hasChild, DOMAIN_INST_NS_OBJ.B))
        g.add((DOMAIN_INST_NS_OBJ.B, DOMAIN_NS_OBJ.isChildOf, DOMAIN_INST_NS_OBJ.A))  # inverse present

        result = _check_bidirectional_op(g, tbox)
        assert result["passed"] is True


class TestCheckInferenceSanity:
    """추론 sanity check."""

    def test_inferred_increase(self):
        """추론 후 트리플 증가 확인."""
        from tools.kg_validation import _check_inference_sanity

        g_raw = _new_graph()
        for i in range(10):
            g_raw.add((URIRef(f"http://example.org/{i}"), RDF.type, OWL.Thing))

        g_inferred = _new_graph()
        for s, p, o in g_raw:
            g_inferred.add((s, p, o))
        for i in range(5):
            g_inferred.add((URIRef(f"http://example.org/inferred_{i}"), RDF.type, OWL.Thing))

        result = _check_inference_sanity(g_inferred, g_raw)
        assert result["passed"] is True
        assert result["increase"] == 5

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    def test_no_inferred_file(self):
        """추론 파일 없으면 실패."""
        from tools.kg_validation import _check_inference_sanity

        result = _check_inference_sanity(_new_graph(), _new_graph())
        assert result["passed"] is False
        assert "파일 없음" in result.get("message", "")


# ── validate_kg MCP 도구 ──────────────────────────


class TestValidateKg:
    """validate_kg() 통합 테스트."""

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_happy_path_all_pass(self, mock_exists, mock_load_graph):
        """모든 검증 통과 시 score 확인."""
        from tools.kg_validation import validate_kg

        # os.path.exists: TBOX_PATH만 False로 반환하여 빈 tbox 사용
        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        result = json.loads(validate_kg(use_inferred=False))
        assert result["success"] is True
        assert "score" in result
        assert "checks" in result
        # 24 = 기존 23 + undeclared_dp (2026-08-27 신설). A-Box 가 리터럴 값으로
        # 쓰는데 T-Box 에 선언이 없는 DP 를 잡는다 — undeclared_op 의 DP 거울상이고,
        # 그 축이 23 check 전체에서 사각지대였다 (배포 실측 18건, 최다 4,320 트리플).
        assert len(result["checks"]) == 25

    @patch("tools.kg_validation._load_graph")
    def test_error_handling(self, mock_load_graph):
        """그래프 로드 실패 시 에러 응답."""
        from tools.kg_validation import validate_kg

        mock_load_graph.side_effect = FileNotFoundError("T-Box not found")

        result = json.loads(validate_kg())
        assert result["success"] is False
        assert "error" in result

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_checks_have_name_and_passed(self, mock_exists, mock_load_graph):
        """각 검증 항목에 name, passed 필드 존재."""
        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        result = json.loads(validate_kg())
        for check in result["checks"]:
            assert "name" in check, f"Check missing 'name': {check}"
            assert "passed" in check, f"Check missing 'passed': {check}"


class TestDomainRangeFull:
    """Domain/Range 전수 검증 — OP 제한 및 트리플 제한 제거."""

    def test_detects_domain_violation(self):
        """OP의 subject 타입이 domain과 불일치 시 감지."""
        from rdflib import Namespace

        from tools.kg_validation import _check_domain_range_conformance

        STEEL_T = Namespace("https://w3id.org/steel/ontology/")
        INST = Namespace("https://w3id.org/steel/instance/")

        tbox = _new_graph()
        tbox.add((STEEL_T.hasEquipment, RDF.type, OWL.ObjectProperty))
        tbox.add((STEEL_T.hasEquipment, RDFS.domain, STEEL_T.Process))
        tbox.add((STEEL_T.hasEquipment, RDFS.range, STEEL_T.Equipment))
        tbox.add((STEEL_T.Process, RDF.type, OWL.Class))
        tbox.add((STEEL_T.Equipment, RDF.type, OWL.Class))
        tbox.add((STEEL_T.Sensor, RDF.type, OWL.Class))

        g = _new_graph()
        g.bind("steel", STEEL_T)
        g.bind("steel-inst", INST)
        g.add((INST.sensor_1, RDF.type, STEEL_T.Sensor))
        g.add((INST.eq_1, RDF.type, STEEL_T.Equipment))
        g.add((INST.sensor_1, STEEL_T.hasEquipment, INST.eq_1))  # domain violation: Sensor not Process

        result = _check_domain_range_conformance(g, tbox)
        assert result["passed"] is False
        assert len(result.get("violations", [])) > 0


class TestCheckClassInstanceCount:
    """클래스별 인스턴스 수 검증."""

    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("os.path.exists", return_value=False)
    def test_no_tbox_file(self, mock_exists):
        """T-Box 파일 없으면 tbox_classes 빈 상태로 동작."""
        from tools.kg_validation import _check_class_instance_count

        g = _new_graph()
        result = _check_class_instance_count(g)
        assert "name" in result
        assert "passed" in result


class TestCheckCardinalityConstraints:
    """카디널리티 제약 위반 검증 (Check 19)."""

    def test_no_restrictions_auto_pass(self):
        """카디널리티 제한 없으면 자동 통과."""
        from tools.kg_validation import _check_cardinality_constraints

        tbox = _make_tbox_graph()
        g = _make_abox_graph(tbox)
        result = _check_cardinality_constraints(g, tbox)
        assert result["passed"] is True
        assert result["checked_classes"] == 0
        assert result["violations"] == []

    def test_min_cardinality_violation(self):
        """minCardinality 위반 탐지 — 값 0개인데 min=1."""
        from rdflib import BNode

        from tools.kg_validation import _check_cardinality_constraints

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        eq_cls = URIRef(f"{ns}EquipmentMaster")
        has_tag = URIRef(f"{ns}hasTag")
        tbox.add((eq_cls, RDF.type, OWL.Class))
        tbox.add((has_tag, RDF.type, OWL.ObjectProperty))

        # restriction: EquipmentMaster subClassOf (hasTag min 1)
        restr = BNode()
        tbox.add((eq_cls, RDFS.subClassOf, restr))
        tbox.add((restr, OWL.onProperty, has_tag))
        tbox.add((restr, OWL.minCardinality, Literal(1)))

        g = _new_graph()
        for s, p, o in tbox:
            g.add((s, p, o))
        eq_inst = URIRef(f"{inst}EquipmentMaster_EQ001")
        g.add((eq_inst, RDF.type, eq_cls))
        # No hasTag triple → minCardinality=1 violated

        result = _check_cardinality_constraints(g, tbox)
        assert result["passed"] is False
        assert len(result["violations"]) >= 1
        assert result["violations"][0]["constraint"] == "minCardinality=1"
        assert result["violations"][0]["actual_count"] == 0

    def test_max_cardinality_pass(self):
        """maxCardinality 충족 시 통과."""
        from rdflib import BNode

        from tools.kg_validation import _check_cardinality_constraints

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        eq_cls = URIRef(f"{ns}EquipmentMaster")
        has_tag = URIRef(f"{ns}hasTag")
        tag_cls = URIRef(f"{ns}TagMaster")
        tbox.add((eq_cls, RDF.type, OWL.Class))
        tbox.add((tag_cls, RDF.type, OWL.Class))
        tbox.add((has_tag, RDF.type, OWL.ObjectProperty))

        restr = BNode()
        tbox.add((eq_cls, RDFS.subClassOf, restr))
        tbox.add((restr, OWL.onProperty, has_tag))
        tbox.add((restr, OWL.maxCardinality, Literal(2)))

        g = _new_graph()
        for s, p, o in tbox:
            g.add((s, p, o))
        eq_inst = URIRef(f"{inst}EquipmentMaster_EQ001")
        tag1 = URIRef(f"{inst}TagMaster_T001")
        g.add((eq_inst, RDF.type, eq_cls))
        g.add((tag1, RDF.type, tag_cls))
        g.add((eq_inst, has_tag, tag1))

        result = _check_cardinality_constraints(g, tbox)
        assert result["passed"] is True
        assert result["checked_instances"] >= 1

    def test_exact_cardinality_violation(self):
        """exactCardinality 위반 탐지 — 값 2개인데 exact=1."""
        from rdflib import BNode

        from tools.kg_validation import _check_cardinality_constraints

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        eq_cls = URIRef(f"{ns}EquipmentMaster")
        has_tag = URIRef(f"{ns}hasTag")
        tag_cls = URIRef(f"{ns}TagMaster")
        tbox.add((eq_cls, RDF.type, OWL.Class))
        tbox.add((tag_cls, RDF.type, OWL.Class))
        tbox.add((has_tag, RDF.type, OWL.ObjectProperty))

        restr = BNode()
        tbox.add((eq_cls, RDFS.subClassOf, restr))
        tbox.add((restr, OWL.onProperty, has_tag))
        tbox.add((restr, OWL.cardinality, Literal(1)))

        g = _new_graph()
        for s, p, o in tbox:
            g.add((s, p, o))
        eq_inst = URIRef(f"{inst}EquipmentMaster_EQ001")
        tag1 = URIRef(f"{inst}TagMaster_T001")
        tag2 = URIRef(f"{inst}TagMaster_T002")
        g.add((eq_inst, RDF.type, eq_cls))
        g.add((tag1, RDF.type, tag_cls))
        g.add((tag2, RDF.type, tag_cls))
        g.add((eq_inst, has_tag, tag1))
        g.add((eq_inst, has_tag, tag2))

        result = _check_cardinality_constraints(g, tbox)
        assert result["passed"] is False
        assert result["violations"][0]["constraint"] == "exactCardinality=1"
        assert result["violations"][0]["actual_count"] == 2


class TestCheckDisjointClassViolations:
    """AllDisjointClasses 위반 검증 (Check 20)."""

    def test_no_disjoint_groups_auto_pass(self):
        """AllDisjointClasses 선언 없으면 자동 통과."""
        from tools.kg_validation import _check_disjoint_class_violations

        tbox = _make_tbox_graph()
        g = _make_abox_graph(tbox)
        result = _check_disjoint_class_violations(g, tbox)
        assert result["passed"] is True
        assert result["disjoint_groups"] == 0
        assert result["violations"] == []

    def test_disjoint_violation_detected(self):
        """인스턴스가 서로소 클래스 2개에 동시 속하면 위반."""
        from rdflib import BNode
        from rdflib.collection import Collection

        from tools.kg_validation import _check_disjoint_class_violations

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        eq_cls = URIRef(f"{ns}EquipmentMaster")
        sensor_cls = URIRef(f"{ns}Sensor")
        tbox.add((eq_cls, RDF.type, OWL.Class))
        tbox.add((sensor_cls, RDF.type, OWL.Class))

        # AllDisjointClasses { EquipmentMaster, Sensor }
        dj = BNode()
        tbox.add((dj, RDF.type, OWL.AllDisjointClasses))
        members_list = BNode()
        Collection(tbox, members_list, [eq_cls, sensor_cls])
        tbox.add((dj, OWL.members, members_list))

        g = _new_graph()
        for s, p, o in tbox:
            g.add((s, p, o))
        # Instance typed as both EquipmentMaster and Sensor → violation
        bad_inst = URIRef(f"{inst}Bad_Instance")
        g.add((bad_inst, RDF.type, eq_cls))
        g.add((bad_inst, RDF.type, sensor_cls))

        result = _check_disjoint_class_violations(g, tbox)
        assert result["passed"] is False
        assert result["disjoint_groups"] == 1
        assert len(result["violations"]) >= 1
        assert set(result["violations"][0]["types"]) == {"EquipmentMaster", "Sensor"}

    def test_disjoint_no_violation(self):
        """인스턴스가 서로소 클래스 중 하나에만 속하면 통과."""
        from rdflib import BNode
        from rdflib.collection import Collection

        from tools.kg_validation import _check_disjoint_class_violations

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        eq_cls = URIRef(f"{ns}EquipmentMaster")
        sensor_cls = URIRef(f"{ns}Sensor")
        tbox.add((eq_cls, RDF.type, OWL.Class))
        tbox.add((sensor_cls, RDF.type, OWL.Class))

        dj = BNode()
        tbox.add((dj, RDF.type, OWL.AllDisjointClasses))
        members_list = BNode()
        Collection(tbox, members_list, [eq_cls, sensor_cls])
        tbox.add((dj, OWL.members, members_list))

        g = _new_graph()
        for s, p, o in tbox:
            g.add((s, p, o))
        eq_inst = URIRef(f"{inst}EquipmentMaster_EQ001")
        sensor_inst = URIRef(f"{inst}Sensor_S001")
        g.add((eq_inst, RDF.type, eq_cls))
        g.add((sensor_inst, RDF.type, sensor_cls))

        result = _check_disjoint_class_violations(g, tbox)
        assert result["passed"] is True
        assert result["disjoint_groups"] == 1
        assert result["checked_instances"] >= 2


# ── Closed-World (P3) ─────────────────────────────


class TestClosedWorldMasterOrphan:
    """_check_closed_world_master_orphan: master 인스턴스가 transaction에서 참조되는지."""

    def test_skip_when_no_master_file(self, tmp_path):
        from tools.kg_validation import _check_closed_world_master_orphan
        with patch("tools.kg_validation.MASTER_DATA_PATH", str(tmp_path / "nope.ttl")):
            g = _new_graph()
            result = _check_closed_world_master_orphan(g)
        assert result["passed"] is True
        assert result["master_total"] == 0

    def test_all_master_referenced(self, tmp_path):
        """master 2건 모두 transaction OP 객체로 등장 → pass."""
        from tools.kg_validation import _check_closed_world_master_orphan
        master = _new_graph()
        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS
        m1 = URIRef(f"{inst}EquipmentMaster_EQ001")
        m2 = URIRef(f"{inst}EquipmentMaster_EQ002")
        master.add((m1, RDF.type, URIRef(f"{ns}EquipmentMaster")))
        master.add((m2, RDF.type, URIRef(f"{ns}EquipmentMaster")))
        master_path = tmp_path / "master.ttl"
        master.serialize(destination=str(master_path), format="turtle")

        g = _new_graph()
        alarm = URIRef(f"{inst}Alarm_A1")
        g.add((alarm, RDF.type, URIRef(f"{ns}Alarm")))
        g.add((alarm, URIRef(f"{ns}triggeredBy"), m1))
        g.add((alarm, URIRef(f"{ns}triggeredBy"), m2))

        with patch("tools.kg_validation.MASTER_DATA_PATH", str(master_path)):
            result = _check_closed_world_master_orphan(g)
        assert result["passed"] is True
        assert result["master_total"] == 2
        assert result["orphan_count"] == 0

    def test_orphan_over_threshold_fails(self, tmp_path):
        """master 10건 중 6건 미참조 → rate 60% > 50% master 임계치 → fail."""
        from tools.kg_validation import _check_closed_world_master_orphan
        master = _new_graph()
        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS
        masters = [URIRef(f"{inst}EquipmentMaster_EQ{i:03d}") for i in range(10)]
        for m in masters:
            master.add((m, RDF.type, URIRef(f"{ns}EquipmentMaster")))
        master_path = tmp_path / "master.ttl"
        master.serialize(destination=str(master_path), format="turtle")

        g = _new_graph()
        alarm = URIRef(f"{inst}Alarm_A1")
        g.add((alarm, RDF.type, URIRef(f"{ns}Alarm")))
        # 4개만 참조 → 6개 고립 (60% orphan, master 50% threshold 초과)
        for m in masters[:4]:
            g.add((alarm, URIRef(f"{ns}triggeredBy"), m))

        with patch("tools.kg_validation.MASTER_DATA_PATH", str(master_path)):
            result = _check_closed_world_master_orphan(g)
        assert result["passed"] is False
        assert result["master_total"] == 10
        assert result["orphan_count"] == 6
        assert result["orphan_rate"] == 60.0


class TestClosedWorldFkUnresolved:
    """_check_closed_world_fk_unresolved: FK 객체가 master/transaction에 실재하는지."""

    def test_skip_when_no_master(self, tmp_path):
        from tools.kg_validation import _check_closed_world_fk_unresolved
        with patch("tools.kg_validation.MASTER_DATA_PATH", str(tmp_path / "nope.ttl")):
            tbox = _make_tbox_graph()
            g = _make_abox_graph(tbox)
            result = _check_closed_world_fk_unresolved(g, tbox)
        assert result["passed"] is True

    def test_all_fk_resolved(self, tmp_path):
        """모든 OP 객체가 master 혹은 typed subject로 해결 → pass."""
        from tools.kg_validation import _check_closed_world_fk_unresolved
        tbox = _make_tbox_graph()
        g = _make_abox_graph(tbox)

        master = _new_graph()
        for s in g.subjects(RDF.type, URIRef(f"{DOMAIN_NS}EquipmentMaster")):
            master.add((s, RDF.type, URIRef(f"{DOMAIN_NS}EquipmentMaster")))
        master_path = tmp_path / "master.ttl"
        master.serialize(destination=str(master_path), format="turtle")

        with patch("tools.kg_validation.MASTER_DATA_PATH", str(master_path)):
            result = _check_closed_world_fk_unresolved(g, tbox)
        assert result["passed"] is True
        assert result["unresolved_count"] == 0

    def test_unresolved_over_threshold(self, tmp_path):
        """OP 객체 절반이 미등록 URI → unresolved_rate 50% > 5% → fail."""
        from tools.kg_validation import _check_closed_world_fk_unresolved
        tbox = _make_tbox_graph()
        g = _new_graph()
        for s, p, o in tbox:
            g.add((s, p, o))
        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS
        eq = URIRef(f"{inst}EquipmentMaster_EQ001")
        g.add((eq, RDF.type, URIRef(f"{ns}EquipmentMaster")))
        # OP 객체 2개: 하나는 typed, 다른 하나는 미등록
        ghost = URIRef(f"{inst}EquipmentStatus_GHOST")
        real = URIRef(f"{inst}EquipmentStatus_S1")
        g.add((real, RDF.type, URIRef(f"{ns}EquipmentStatus")))
        g.add((eq, URIRef(f"{ns}hasEquipmentStatus"), real))
        g.add((eq, URIRef(f"{ns}hasEquipmentStatus"), ghost))

        master = _new_graph()
        master.add((eq, RDF.type, URIRef(f"{ns}EquipmentMaster")))
        master_path = tmp_path / "master.ttl"
        master.serialize(destination=str(master_path), format="turtle")

        with patch("tools.kg_validation.MASTER_DATA_PATH", str(master_path)):
            result = _check_closed_world_fk_unresolved(g, tbox)
        assert result["passed"] is False
        assert result["unresolved_count"] == 1
        assert result["total_fk_triples"] == 2
        assert result["unresolved_rate"] == 50.0
