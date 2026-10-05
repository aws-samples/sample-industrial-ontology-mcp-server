"""Tests for tools/inference.py — OWL RL 추론 도구 테스트."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_INST_NS_OBJ, DOMAIN_NS, DOMAIN_NS_OBJ
from domain.tbox_utils import _new_graph

# ── 헬퍼 ──────────────────────────────────────────


def _make_minimal_graph():
    """추론 테스트용 최소 그래프."""
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS

    eq_cls = URIRef(f"{ns}EquipmentMaster")
    g.add((eq_cls, RDF.type, OWL.Class))

    eq1 = URIRef(f"{inst}EquipmentMaster_EQ001")
    g.add((eq1, RDF.type, eq_cls))
    g.add((eq1, URIRef(f"{ns}equipmentID"), Literal("EQ001")))
    return g


# ── _extract_inferred ────────────────────────────


class TestExtractInferred:
    """추론된 트리플 필터링."""

    def test_filters_steel_namespace_only(self):
        from tools.inference import _extract_inferred

        g = _new_graph()
        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        # steel 네임스페이스 트리플
        steel_triple = (URIRef(f"{ns}TestClass"), RDF.type, OWL.Class)
        g.add(steel_triple)

        # steel-inst 네임스페이스 트리플
        inst_triple = (URIRef(f"{inst}Test_001"), RDF.type, URIRef(f"{ns}TestClass"))
        g.add(inst_triple)

        # 외부 네임스페이스 트리플
        external = (URIRef("http://example.org/X"), RDF.type, OWL.Thing)
        g.add(external)

        result = _extract_inferred(0, g)
        # steel/steel-inst 트리플만 포함
        result_subjects = {str(s) for s, _, _ in result}
        assert f"{ns}TestClass" in result_subjects
        assert f"{inst}Test_001" in result_subjects
        assert "http://example.org/X" not in result_subjects

    def test_preserves_namespace_bindings(self):
        from tools.inference import _extract_inferred

        g = _new_graph()
        g.bind("steel", DOMAIN_NS)
        g.add((URIRef(f"{DOMAIN_NS}X"), RDF.type, OWL.Class))

        result = _extract_inferred(0, g)
        # 네임스페이스 바인딩 유지
        ns_dict = dict(result.namespaces())
        assert "steel" in ns_dict


# ── _apply_owl_restrictions ───────────────────────


class TestApplyOwlRestrictions:
    """OWL restriction 후처리."""

    def test_has_value_restriction(self):
        """owl:hasValue 패턴: 프로퍼티 값이 특정 값이면 타입 부여."""
        from tools.inference import _apply_owl_restrictions

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        running_cls = URIRef(f"{ns}RunningEquipment")
        status_prop = URIRef(f"{ns}operatingStatus")
        tbox.add((running_cls, RDF.type, OWL.Class))

        # equivalentClass → Restriction
        restriction = URIRef(f"{ns}_restriction_1")
        tbox.add((running_cls, OWL.equivalentClass, restriction))
        tbox.add((restriction, RDF.type, OWL.Restriction))
        tbox.add((restriction, OWL.onProperty, status_prop))
        tbox.add((restriction, OWL.hasValue, Literal("Running")))

        # A-Box
        g = _new_graph()
        eq1 = URIRef(f"{inst}Equipment_001")
        g.add((eq1, status_prop, Literal("Running")))
        g.add((eq1, RDF.type, URIRef(f"{ns}EquipmentMaster")))

        stats = _apply_owl_restrictions(g, tbox)
        assert stats["hasValue"] >= 1
        assert (eq1, RDF.type, running_cls) in g

    def test_some_values_from_restriction(self):
        """owl:someValuesFrom 패턴."""
        from tools.inference import _apply_owl_restrictions

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        equipped_cls = URIRef(f"{ns}EquippedFacility")
        has_equip = URIRef(f"{ns}hasEquipment")
        equip_cls = URIRef(f"{ns}Equipment")

        tbox.add((equipped_cls, RDF.type, OWL.Class))
        restriction = URIRef(f"{ns}_restriction_2")
        tbox.add((equipped_cls, OWL.equivalentClass, restriction))
        tbox.add((restriction, RDF.type, OWL.Restriction))
        tbox.add((restriction, OWL.onProperty, has_equip))
        tbox.add((restriction, OWL.someValuesFrom, equip_cls))

        g = _new_graph()
        facility = URIRef(f"{inst}Facility_001")
        equipment = URIRef(f"{inst}Equipment_001")
        g.add((facility, has_equip, equipment))
        g.add((equipment, RDF.type, equip_cls))

        stats = _apply_owl_restrictions(g, tbox)
        assert stats["someValuesFrom"] >= 1
        assert (facility, RDF.type, equipped_cls) in g

    def test_min_cardinality_restriction(self):
        """owl:minCardinality 패턴."""
        from tools.inference import _apply_owl_restrictions

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        tbox = _new_graph()
        multi_alarm = URIRef(f"{ns}MultiAlarmEquipment")
        has_alarm = URIRef(f"{ns}hasAlarm")

        tbox.add((multi_alarm, RDF.type, OWL.Class))
        restriction = URIRef(f"{ns}_restriction_3")
        tbox.add((multi_alarm, OWL.equivalentClass, restriction))
        tbox.add((restriction, RDF.type, OWL.Restriction))
        tbox.add((restriction, OWL.onProperty, has_alarm))
        tbox.add((restriction, OWL.minCardinality, Literal(3, datatype=XSD.integer)))

        g = _new_graph()
        eq = URIRef(f"{inst}Equipment_001")
        for i in range(3):
            g.add((eq, has_alarm, URIRef(f"{inst}Alarm_{i}")))

        stats = _apply_owl_restrictions(g, tbox)
        assert stats["minCardinality"] >= 1
        assert (eq, RDF.type, multi_alarm) in g

    def test_no_restrictions(self):
        """restriction 없으면 stats 0."""
        from tools.inference import _apply_owl_restrictions

        tbox = _new_graph()
        g = _new_graph()
        stats = _apply_owl_restrictions(g, tbox)
        assert stats["total"] == 0

    def test_checks_all_instances_not_limited(self):
        """50건 상한 없이 전수 검증."""
        from rdflib import BNode, Namespace

        from tools.inference import _apply_owl_restrictions

        EX = Namespace("http://example.org/")

        tbox = _new_graph()
        # someValuesFrom restriction: Equipment must have at least one Process
        restriction = BNode()
        tbox.add((EX.Equipment, RDF.type, OWL.Class))
        tbox.add((EX.Process, RDF.type, OWL.Class))
        tbox.add((EX.hasProcess, RDF.type, OWL.ObjectProperty))
        tbox.add((EX.Equipment, RDFS.subClassOf, restriction))
        tbox.add((restriction, RDF.type, OWL.Restriction))
        tbox.add((restriction, OWL.onProperty, EX.hasProcess))
        tbox.add((restriction, OWL.someValuesFrom, EX.Process))

        g = _new_graph()
        # Create 60 Equipment instances (exceeds old 50 cap)
        for i in range(60):
            g.add((EX[f"eq_{i}"], RDF.type, EX.Equipment))
        # Only eq_0 has a hasProcess link
        g.add((EX.eq_0, EX.hasProcess, EX.proc_1))
        g.add((EX.proc_1, RDF.type, EX.Process))

        result = _apply_owl_restrictions(g, tbox)
        # Should DETECT 59 violations (eq_1 through eq_59 have no hasProcess).
        # 전수 검출은 count 로 보장한다. 리스트 자체는 응답 비대화(MCP stdio
        # -32000) 방지를 위해 50건으로 샘플 cap 되므로 count 로 검증.
        assert result.get("restriction_violation_count", 0) >= 59  # 전수 검출
        violations = result.get("restriction_violations", [])
        assert len(violations) <= 50  # 응답 샘플은 cap
        assert result.get("restriction_violations_truncated") is True


# ── _load_and_merge ───────────────────────────────


class TestLoadAndMerge:
    """T-Box + A-Box + 암묵지 병합."""

    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("domain.tbox_utils.load_graph")
    @patch("os.path.exists", return_value=True)
    def test_happy_path(self, mock_exists, mock_load, mock_invalidate):
        from tools.inference import _load_and_merge

        mock_graph = _make_minimal_graph()
        mock_load.return_value = (mock_graph, 2)

        g, tacit_count = _load_and_merge()
        assert len(g) > 0
        assert tacit_count == 2
        mock_invalidate.assert_called_once()

    @patch("os.path.exists", return_value=False)
    def test_missing_tbox_raises(self, mock_exists):
        """T-Box 경로가 존재하지 않으면 FileNotFoundError."""
        from tools.inference import _load_and_merge

        with pytest.raises(FileNotFoundError):
            _load_and_merge(tbox_path="/nonexistent/tbox.ttl")


# ── run_owl_rl_inference MCP 도구 ─────────────────


class TestRunOwlRlInference:
    """OWL RL 추론 MCP 도구."""

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    def test_happy_path(
        self, mock_merge, mock_closure, mock_getsize, mock_exists,
        mock_invalidate, mock_write, mock_quality,
    ):
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _make_minimal_graph()
        mock_merge.return_value = (g, 1)
        mock_exists.return_value = True

        # PyReasoner 모킹 — reason() 이 빈 closure 반환
        mock_closure_instance = MagicMock()
        mock_closure_instance.reason.return_value = []
        mock_closure.return_value = mock_closure_instance

        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        # Graph.parse를 모킹하여 T-Box 파싱 성공
        with patch.object(Graph, "parse"):
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True
        assert "triples_before" in result
        assert "triples_after" in result
        assert "timing" in result

    @patch("tools.inference._load_and_merge")
    def test_missing_tbox_error(self, mock_merge):
        """T-Box 없으면 에러 응답."""
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        mock_merge.side_effect = FileNotFoundError("T-Box not found")

        result = json.loads(run_owl_rl_inference(force=True))
        assert result["success"] is False
        assert "error" in result
        assert "T-Box" in result.get("hint", "")

    @patch("tools.inference._load_and_merge")
    def test_generic_exception_error(self, mock_merge):
        """일반 예외 시 에러 응답."""
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        mock_merge.side_effect = RuntimeError("Unexpected error")

        result = json.loads(run_owl_rl_inference(force=True))
        assert result["success"] is False
        assert "error" in result


# ── _categorize_inferred_triples ──────────────────


class TestCategorizeInferredTriples:
    """추론 트리플 유형 분류."""

    def test_categorizes_rdf_type(self):
        from tools.inference import _categorize_inferred_triples

        pre = _new_graph()
        post = _new_graph()

        # 추론된 rdf:type
        new_type = (URIRef(f"{DOMAIN_INST_NS}X"), RDF.type, URIRef(f"{DOMAIN_NS}MyClass"))
        post.add(new_type)

        # 추론된 subClassOf
        new_sub = (URIRef(f"{DOMAIN_NS}A"), RDFS.subClassOf, URIRef(f"{DOMAIN_NS}B"))
        post.add(new_sub)

        categories = _categorize_inferred_triples(pre, post)
        assert len(categories["rdf_type"]) == 1
        assert len(categories["subclass"]) == 1

    def test_ignores_pre_existing_triples(self):
        from tools.inference import _categorize_inferred_triples

        pre = _new_graph()
        post = _new_graph()

        existing = (URIRef(f"{DOMAIN_INST_NS}X"), RDF.type, URIRef(f"{DOMAIN_NS}MyClass"))
        pre.add(existing)
        post.add(existing)

        categories = _categorize_inferred_triples(pre, post)
        assert len(categories["rdf_type"]) == 0


# ── _detect_type_pollution — contradiction audit trail ─────


def _build_disjoint_scenario():
    """AllDisjointClasses 위반을 유발하는 pre/post/tbox 그래프를 생성한다.

    Returns:
        (pre, post, tbox) — pre에는 원래 타입만, post에는 disjoint 위반 타입 추가.
    """
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS

    cls_a = URIRef(f"{ns}ProcessBlastFurnace")
    cls_b = URIRef(f"{ns}ElectricalConsumption")
    URIRef(f"{ns}hasEquipment")

    # T-Box: AllDisjointClasses {ProcessBlastFurnace, ElectricalConsumption}
    tbox = _new_graph()
    tbox.bind("steel", DOMAIN_NS_OBJ)
    tbox.add((cls_a, RDF.type, OWL.Class))
    tbox.add((cls_b, RDF.type, OWL.Class))
    disjoint_bnode = URIRef(f"{ns}_disjoint_1")
    tbox.add((disjoint_bnode, RDF.type, OWL.AllDisjointClasses))
    # rdflib Collection for owl:members
    from rdflib import BNode
    list_head = BNode()
    list_mid = BNode()
    tbox.add((disjoint_bnode, OWL.members, list_head))
    tbox.add((list_head, RDF.first, cls_a))
    tbox.add((list_head, RDF.rest, list_mid))
    tbox.add((list_mid, RDF.first, cls_b))
    tbox.add((list_mid, RDF.rest, RDF.nil))

    # Pre: 인스턴스에 cls_a 타입만 존재
    inst1 = URIRef(f"{inst}BF_001")
    pre = _new_graph()
    pre.bind("steel", DOMAIN_NS_OBJ)
    pre.bind("steel-inst", DOMAIN_INST_NS_OBJ)
    pre.add((inst1, RDF.type, cls_a))

    # Post: 추론으로 cls_b가 추가됨 (disjoint 위반)
    post = _new_graph()
    post.bind("steel", DOMAIN_NS_OBJ)
    post.bind("steel-inst", DOMAIN_INST_NS_OBJ)
    post.add((inst1, RDF.type, cls_a))
    post.add((inst1, RDF.type, cls_b))  # 오염된 타입

    return pre, post, tbox


class TestDetectTypePollutionAuditTrail:
    """_detect_type_pollution 반환값에 감사 추적용 추가 키 포함 확인."""

    def test_returns_conflicting_types(self):
        """conflicting_types 키가 존재하고 URI 목록을 담고 있어야 한다."""
        from tools.inference import _detect_type_pollution

        pre, post, tbox = _build_disjoint_scenario()
        records = _detect_type_pollution(pre, post, tbox)

        assert len(records) >= 1
        rec = records[0]
        assert "conflicting_types" in rec
        assert isinstance(rec["conflicting_types"], list)
        assert len(rec["conflicting_types"]) >= 1

    def test_returns_resolution_field(self):
        """resolution 키가 'removed_polluted'여야 한다."""
        from tools.inference import _detect_type_pollution

        pre, post, tbox = _build_disjoint_scenario()
        records = _detect_type_pollution(pre, post, tbox)

        assert len(records) >= 1
        rec = records[0]
        assert rec.get("resolution") == "removed_polluted"

    def test_returns_removed_type(self):
        """removed_type 키가 polluted_type과 동일해야 한다."""
        from tools.inference import _detect_type_pollution

        pre, post, tbox = _build_disjoint_scenario()
        records = _detect_type_pollution(pre, post, tbox)

        assert len(records) >= 1
        rec = records[0]
        assert "removed_type" in rec
        assert rec["removed_type"] == rec["polluted_type"]

    def test_returns_kept_type(self):
        """kept_type 키가 original_types와 동일해야 한다."""
        from tools.inference import _detect_type_pollution

        pre, post, tbox = _build_disjoint_scenario()
        records = _detect_type_pollution(pre, post, tbox)

        assert len(records) >= 1
        rec = records[0]
        assert "kept_type" in rec
        assert rec["kept_type"] == rec["original_types"]

    def test_all_audit_keys_present(self):
        """기존 키 + 신규 4개 키 모두 존재해야 한다."""
        from tools.inference import _detect_type_pollution

        pre, post, tbox = _build_disjoint_scenario()
        records = _detect_type_pollution(pre, post, tbox)

        expected_keys = {
            "instance", "original_types", "polluted_type", "reason",
            "conflicting_types", "resolution", "removed_type", "kept_type",
        }
        assert len(records) >= 1
        assert set(records[0].keys()) == expected_keys

    def test_no_pollution_returns_empty(self):
        """disjoint 위반이 없으면 빈 리스트."""
        from tools.inference import _detect_type_pollution

        pre = _new_graph()
        post = _new_graph()
        tbox = _new_graph()
        records = _detect_type_pollution(pre, post, tbox)
        assert records == []


class TestContradictionAuditTrailFile:
    """run_owl_rl_inference가 inference_contradictions.json을 올바르게 저장하는지 확인."""

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write_json")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    @patch("tools.inference._detect_type_pollution")
    def test_audit_json_written_when_pollution_found(
        self, mock_detect, mock_merge, mock_closure, mock_getsize,
        mock_exists, mock_invalidate, mock_write, mock_write_json, mock_quality,
    ):
        """오염 레코드가 있으면 inference_contradictions.json이 atomic_write_json으로 저장된다."""
        from tools.inference import INFERRED_PATH
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _make_minimal_graph()
        mock_merge.return_value = (g, 0)
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 1},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        mock_detect.return_value = [
            {
                "instance": "BF_001",
                "original_types": ["ProcessBlastFurnace"],
                "polluted_type": "ElectricalConsumption",
                "reason": "domain/range 추론이 AllDisjointClasses 멤버 간 교차 타입을 생성",
                "conflicting_types": ["ProcessBlastFurnace", "ElectricalConsumption"],
                "resolution": "removed_polluted",
                "removed_type": "ElectricalConsumption",
                "kept_type": ["ProcessBlastFurnace"],
            }
        ]

        with patch.object(Graph, "parse"):
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True

        # atomic_write_json 호출 중 contradictions JSON 경로가 있는지 확인
        contradictions_path = os.path.join(
            os.path.dirname(INFERRED_PATH), "inference_contradictions.json"
        )
        write_calls = mock_write_json.call_args_list
        contradiction_calls = [
            c for c in write_calls if c[0][0] == contradictions_path
        ]
        # ⚠️ 호출 **횟수**를 고정하지 않는다. 이 경로에는 감사 기록 1회가 항상 오고,
        # 직렬화 뒤 출력 지문 각인이 **파일이 이미 존재할 때만** 1회 더 온다
        # (add_source_artifacts 는 없는 파일을 건너뛴다). 그래서 배포 트리 상태에
        # 따라 1 또는 2 다 — 실제로 `== 1` 과 `== 2` 양쪽으로 한 번씩 깨졌다.
        # 2단 병합 계약은 tests/test_sidecar_lineage.py 가 실제 파일로 검증한다.
        assert len(contradiction_calls) >= 1, (
            f"감사 기록이 최소 1회 있어야 함, "
            f"실제 호출: {[c[0][0] for c in write_calls]}"
        )

        # 2번째 인자는 dict 자체 — 구조 검증
        written_data = contradiction_calls[0][0][1]
        assert "generated_at" in written_data
        assert written_data["total_contradictions"] == 1
        assert len(written_data["records"]) == 1
        assert written_data["records"][0]["resolution"] == "removed_polluted"
        # 1회차에 각인되는 것은 **입력** 지문이다 (출력은 아직 이전 세대다)
        assert set(written_data["_source"]["artifacts"]) == {"tbox", "abox"}
        # 2회차가 출력 지문을 덧붙인다 — 이것이 세대 대조의 근거다
        # 각인 패스가 왔다면 그것은 출력 지문을 덧붙이고 기존 키를 덮지 않는다.
        if len(contradiction_calls) > 1:
            stamped = contradiction_calls[1][0][1]
            assert "inferred" in stamped["_source"]["artifacts"]
            assert {"total_contradictions", "records"} <= set(stamped)

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write_json")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    @patch("tools.inference._detect_type_pollution")
    def test_no_audit_json_when_no_pollution(
        self, mock_detect, mock_merge, mock_closure, mock_getsize,
        mock_exists, mock_invalidate, mock_write, mock_write_json, mock_quality,
    ):
        """오염 레코드가 없으면 inference_contradictions.json을 기록하지 않는다."""
        from tools.inference import INFERRED_PATH
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _make_minimal_graph()
        mock_merge.return_value = (g, 0)
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        mock_detect.return_value = []

        with patch.object(Graph, "parse"):
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True

        contradictions_path = os.path.join(
            os.path.dirname(INFERRED_PATH), "inference_contradictions.json"
        )
        write_calls = mock_write_json.call_args_list
        contradiction_calls = [
            c for c in write_calls if c[0][0] == contradictions_path
        ]
        assert len(contradiction_calls) == 0, (
            "오염 없으면 inference_contradictions.json을 기록하지 않아야 함"
        )


# ── _build_justifications ────────────────────────


class TestBuildJustifications:
    """추론 트리플별 정당화(justification) 추적."""

    def test_domain_inference_justified(self):
        """(inst, hasProp, val) + (hasProp domain ClassA) → rule='rdfs_domain'."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        cls_a = URIRef(f"{ns}ClassA")
        has_prop = URIRef(f"{ns}hasProp")
        inst1 = URIRef(f"{inst}Inst_001")
        val1 = URIRef(f"{inst}Val_001")

        # tbox: hasProp domain ClassA
        tbox = _new_graph()
        tbox.add((has_prop, RDFS.domain, cls_a))

        # pre: (inst1, hasProp, val1) — 프로퍼티 사용
        pre_triples = {(inst1, has_prop, val1)}

        # delta: (inst1, rdf:type, ClassA) — domain 추론
        delta_triples = {(inst1, RDF.type, cls_a)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "rdfs_domain"
        assert results[0]["triple"] == (inst1, RDF.type, cls_a)
        assert len(results[0]["prerequisites"]) >= 1
        assert results[0]["confidence"] == "high"

    def test_inverse_inference_justified(self):
        """(parent, hasChild, child) + (hasChild inverseOf isChildOf)
        → (child, isChildOf, parent) → rule='inverse_of'."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        has_child = URIRef(f"{ns}hasChild")
        is_child_of = URIRef(f"{ns}isChildOf")
        parent = URIRef(f"{inst}Parent_001")
        child = URIRef(f"{inst}Child_001")

        # tbox: hasChild inverseOf isChildOf
        tbox = _new_graph()
        tbox.add((has_child, OWL.inverseOf, is_child_of))

        # pre: (parent, hasChild, child)
        pre_triples = {(parent, has_child, child)}

        # delta: (child, isChildOf, parent) — inverse 추론
        delta_triples = {(child, is_child_of, parent)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "inverse_of"
        assert results[0]["triple"] == (child, is_child_of, parent)
        assert results[0]["confidence"] == "high"

    def test_subclass_inference_justified(self):
        """(inst, type, Sub) + (Sub subClassOf Super)
        → (inst, type, Super) → rule='subclass_chain'."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        sub_cls = URIRef(f"{ns}SubClass")
        super_cls = URIRef(f"{ns}SuperClass")
        inst1 = URIRef(f"{inst}Inst_001")

        # tbox: Sub subClassOf Super
        tbox = _new_graph()
        tbox.add((sub_cls, RDFS.subClassOf, super_cls))

        # pre: (inst1, rdf:type, Sub)
        pre_triples = {(inst1, RDF.type, sub_cls)}

        # delta: (inst1, rdf:type, Super) — subclass 추론
        delta_triples = {(inst1, RDF.type, super_cls)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "subclass_chain"
        assert results[0]["triple"] == (inst1, RDF.type, super_cls)
        assert results[0]["confidence"] == "high"

    def test_unjustified_triple_flagged(self):
        """빈 tbox + 임의 트리플 → rule='unknown'."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        inst1 = URIRef(f"{inst}Inst_001")
        some_cls = URIRef(f"{ns}SomeClass")

        tbox = _new_graph()
        pre_triples = set()
        delta_triples = {(inst1, RDF.type, some_cls)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "unknown"
        assert results[0]["confidence"] == "low"

    def test_transitive_closure_justified(self):
        """(s, P, y) + (y, P, z) → (s, P, z) when P is TransitiveProperty."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        followed_by = URIRef(f"{ns}followedBy")
        a = URIRef(f"{inst}Process_A")
        b = URIRef(f"{inst}Process_B")
        c = URIRef(f"{inst}Process_C")

        tbox = _new_graph()
        tbox.add((followed_by, RDF.type, OWL.TransitiveProperty))

        pre_triples = {(a, followed_by, b), (b, followed_by, c)}
        delta_triples = {(a, followed_by, c)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "transitive_closure"
        assert results[0]["confidence"] == "high"

    def test_symmetric_property_justified(self):
        """P(x,y) + P is SymmetricProperty → P(y,x) → rule='symmetric'."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        adjacent_to = URIRef(f"{ns}adjacentTo")
        zone_a = URIRef(f"{inst}Zone_A")
        zone_b = URIRef(f"{inst}Zone_B")

        tbox = _new_graph()
        tbox.add((adjacent_to, RDF.type, OWL.SymmetricProperty))

        pre_triples = {(zone_a, adjacent_to, zone_b)}
        delta_triples = {(zone_b, adjacent_to, zone_a)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "symmetric"
        assert results[0]["triple"] == (zone_b, adjacent_to, zone_a)
        assert results[0]["confidence"] == "high"
        assert any("SymmetricProperty" in p for p in results[0]["prerequisites"])

    def test_range_inference_justified(self):
        """(_, hasProp, inst) + (hasProp range ClassB) → (inst, type, ClassB)."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        cls_b = URIRef(f"{ns}ClassB")
        has_prop = URIRef(f"{ns}hasProp")
        inst1 = URIRef(f"{inst}Inst_001")
        inst2 = URIRef(f"{inst}Inst_002")

        tbox = _new_graph()
        tbox.add((has_prop, RDFS.range, cls_b))

        pre_triples = {(inst2, has_prop, inst1)}
        delta_triples = {(inst1, RDF.type, cls_b)}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "rdfs_range"
        assert results[0]["confidence"] == "high"

    def test_already_exists_flagged(self):
        """delta에 있지만 pre에도 있는 트리플 → rule='already_exists'."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        inst1 = URIRef(f"{inst}Inst_001")
        cls_a = URIRef(f"{ns}ClassA")

        tbox = _new_graph()
        triple = (inst1, RDF.type, cls_a)
        pre_triples = {triple}
        delta_triples = {triple}

        results = _build_justifications(pre_triples, delta_triples, tbox)
        assert len(results) == 1
        assert results[0]["rule"] == "already_exists"

    def test_multiple_deltas_mixed_rules(self):
        """여러 delta 트리플이 각각 올바른 rule로 분류되는지 확인."""
        from tools.inference import _build_justifications

        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        cls_a = URIRef(f"{ns}ClassA")
        super_cls = URIRef(f"{ns}SuperClass")
        has_prop = URIRef(f"{ns}hasProp")
        inst1 = URIRef(f"{inst}Inst_001")
        val1 = URIRef(f"{inst}Val_001")

        tbox = _new_graph()
        tbox.add((has_prop, RDFS.domain, cls_a))
        tbox.add((cls_a, RDFS.subClassOf, super_cls))

        pre_triples = {
            (inst1, has_prop, val1),
            (inst1, RDF.type, cls_a),
        }
        delta_triples = {
            (inst1, RDF.type, cls_a),   # domain 추론 (but also in pre → already_exists)
            (inst1, RDF.type, super_cls),  # subclass 추론
        }

        results = _build_justifications(pre_triples, delta_triples, tbox)
        rules = {r["rule"] for r in results}
        assert "already_exists" in rules
        assert "subclass_chain" in rules


# ── analyze_inference_quality ─────────────────────


class TestAnalyzeInferenceQuality:
    """추론 품질 분석."""

    @patch("tools.inference._load_and_merge")
    @patch("tools.inference.os.path.exists", return_value=False)
    def test_no_inferred_file(self, mock_exists, mock_merge):
        """추론 결과 파일 없으면 에러."""
        from tools.inference import analyze_inference_quality

        mock_merge.return_value = (_new_graph(), 0)

        result = json.loads(analyze_inference_quality())
        assert result["success"] is False
        assert "error" in result


# ── #4 inferred_delta 활용 ─────────────────────────


class TestInferredDelta:
    """inferred_delta.ttl 소비 — read_inferred_delta + analyze_inference_quality 통합."""

    def test_summarize_inferred_delta(self, tmp_path):
        from domain.namespaces import DOMAIN_NS
        from tools.inference import _summarize_inferred_delta

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
steel:x rdf:type steel:A .
steel:A rdfs:subClassOf steel:B .
steel:hasY owl:inverseOf steel:isYOf .
"""
        delta_path = tmp_path / "inferred_delta.ttl"
        delta_path.write_text(ttl, encoding="utf-8")
        s = _summarize_inferred_delta(str(delta_path))
        assert s["total_inferred_triples"] == 3
        assert s["by_predicate_category"]["rdf_type"] == 1
        assert s["by_predicate_category"]["subclass"] == 1
        assert s["by_predicate_category"]["inverse_of"] == 1

    def test_read_inferred_delta_missing(self):
        from tools.inference import read_inferred_delta

        with patch("tools.inference.INFERRED_PATH",
                   "/nonexistent/all_inferred.ttl"):
            result = json.loads(read_inferred_delta())
            assert result["success"] is False

    def test_read_inferred_delta_samples(self, tmp_path):
        from domain.namespaces import DOMAIN_NS
        from tools.inference import read_inferred_delta

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:x rdf:type steel:A .
steel:y rdf:type steel:B .
"""
        main_path = tmp_path / "all_inferred.ttl"
        main_path.write_text("", encoding="utf-8")
        delta_path = tmp_path / "inferred_delta.ttl"
        delta_path.write_text(ttl, encoding="utf-8")

        with patch("tools.inference.INFERRED_PATH", str(main_path)):
            result = json.loads(read_inferred_delta(limit=10))
            assert result["success"] is True
            assert result["total_inferred_triples"] == 2
            assert len(result["samples"]) == 2
