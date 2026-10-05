"""Tests for _prune_inference_noise — 추론 노이즈 제거."""

import json
from unittest.mock import MagicMock, patch

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph

# ── 헬퍼 ──────────────────────────────────────────


def _make_graph_with_noise():
    """노이즈 패턴 4종을 모두 포함하는 테스트 그래프를 생성한다."""
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS

    # 정상 트리플 (보존되어야 함)
    cls_a = URIRef(f"{ns}ClassA")
    cls_b = URIRef(f"{ns}ClassB")
    inst1 = URIRef(f"{inst}Inst_001")
    inst2 = URIRef(f"{inst}Inst_002")

    g.add((cls_a, RDF.type, OWL.Class))
    g.add((cls_b, RDF.type, OWL.Class))
    g.add((inst1, RDF.type, cls_a))
    g.add((inst1, OWL.sameAs, inst2))  # 유효한 sameAs (A != B)
    g.add((cls_a, RDFS.subClassOf, cls_b))  # 유효한 subClassOf
    g.add((cls_a, OWL.equivalentClass, cls_b))  # 유효한 equivalentClass (A != B)

    # Pattern 1: self sameAs — (X, owl:sameAs, X)
    g.add((inst1, OWL.sameAs, inst1))
    g.add((inst2, OWL.sameAs, inst2))

    # Pattern 2: self equivalentClass — (X, owl:equivalentClass, X)
    g.add((cls_a, OWL.equivalentClass, cls_a))

    # Pattern 3: implicit subClassOf owl:Thing / rdfs:Resource
    g.add((cls_a, RDFS.subClassOf, OWL.Thing))
    g.add((cls_b, RDFS.subClassOf, RDFS.Resource))

    # Pattern 4: BNode self subClassOf
    bnode1 = BNode()
    bnode2 = BNode()
    g.add((bnode1, RDFS.subClassOf, bnode1))
    g.add((bnode2, RDFS.subClassOf, bnode2))

    return g


# ── _prune_inference_noise 단위 테스트 ──────────────


class TestPruneInferenceNoise:
    """_prune_inference_noise 함수의 4가지 노이즈 패턴 제거 검증."""

    def test_removes_self_sameAs(self):
        """Pattern 1: (X, owl:sameAs, X) 제거."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        inst1 = URIRef(f"{DOMAIN_INST_NS}Inst_001")
        inst2 = URIRef(f"{DOMAIN_INST_NS}Inst_002")
        g.add((inst1, OWL.sameAs, inst1))  # 노이즈
        g.add((inst1, OWL.sameAs, inst2))  # 유효

        stats = _prune_inference_noise(g)

        assert stats["self_sameAs_removed"] == 1
        assert (inst1, OWL.sameAs, inst1) not in g
        assert (inst1, OWL.sameAs, inst2) in g  # 유효한 것은 보존

    def test_removes_self_equivalentClass(self):
        """Pattern 2: (X, owl:equivalentClass, X) 제거."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        cls_a = URIRef(f"{DOMAIN_NS}ClassA")
        cls_b = URIRef(f"{DOMAIN_NS}ClassB")
        g.add((cls_a, OWL.equivalentClass, cls_a))  # 노이즈
        g.add((cls_a, OWL.equivalentClass, cls_b))  # 유효

        stats = _prune_inference_noise(g)

        assert stats["self_equivalentClass_removed"] == 1
        assert (cls_a, OWL.equivalentClass, cls_a) not in g
        assert (cls_a, OWL.equivalentClass, cls_b) in g

    def test_removes_implicit_subClassOf_thing(self):
        """Pattern 3: (X, rdfs:subClassOf, owl:Thing) 제거 (URIRef만)."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        cls_a = URIRef(f"{DOMAIN_NS}ClassA")
        cls_b = URIRef(f"{DOMAIN_NS}ClassB")
        g.add((cls_a, RDFS.subClassOf, OWL.Thing))  # 노이즈
        g.add((cls_b, RDFS.subClassOf, RDFS.Resource))  # 노이즈
        g.add((cls_a, RDFS.subClassOf, cls_b))  # 유효

        stats = _prune_inference_noise(g)

        assert stats["implicit_subClassOf_removed"] == 2
        assert (cls_a, RDFS.subClassOf, OWL.Thing) not in g
        assert (cls_b, RDFS.subClassOf, RDFS.Resource) not in g
        assert (cls_a, RDFS.subClassOf, cls_b) in g

    def test_removes_bnode_self_subClassOf(self):
        """Pattern 4: BNode self subClassOf 제거."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        bnode = BNode()
        cls_a = URIRef(f"{DOMAIN_NS}ClassA")
        g.add((bnode, RDFS.subClassOf, bnode))  # 노이즈
        g.add((cls_a, RDFS.subClassOf, cls_a))  # URIRef self ref → Pattern 3/4 미해당

        stats = _prune_inference_noise(g)

        assert stats["bnode_self_ref_removed"] == 1
        assert (bnode, RDFS.subClassOf, bnode) not in g

    def test_does_not_remove_bnode_subClassOf_thing(self):
        """Pattern 3은 URIRef만 대상 — BNode subClassOf owl:Thing은 제거하지 않는다."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        bnode = BNode()
        g.add((bnode, RDFS.subClassOf, OWL.Thing))

        stats = _prune_inference_noise(g)

        # BNode은 Pattern 3 대상 아님 (isinstance(s, URIRef) 조건)
        assert stats["implicit_subClassOf_removed"] == 0
        assert (bnode, RDFS.subClassOf, OWL.Thing) in g

    def test_removes_dp_range_mismatch(self):
        """Pattern 6a: DP range 와 literal datatype 이 다르면 제거.

        관찰된 reasoner 오염: GasEnergy 인스턴스에 decimal literal 이
        xsd:string-ranged gasId 로 주입됨 (실제 값은 ProcessRolling 의
        stripWidthMm 에서 유입).
        """
        from rdflib import XSD

        from tools.inference import _prune_inference_noise

        g = _new_graph()
        GE_CLS = URIRef(f"{DOMAIN_NS}GasEnergy")
        gasId = URIRef(f"{DOMAIN_NS}gasId")
        g.add((GE_CLS, RDF.type, OWL.Class))
        g.add((gasId, RDF.type, OWL.DatatypeProperty))
        g.add((gasId, RDFS.range, XSD.string))

        ge_inst = URIRef(f"{DOMAIN_INST_NS}GasEnergy_GS0001")
        g.add((ge_inst, RDF.type, GE_CLS))
        g.add((ge_inst, gasId, Literal("GS0001", datatype=XSD.string)))
        # Foreign: decimal literal on string-typed DP
        g.add((ge_inst, gasId, Literal("1220.26", datatype=XSD.decimal)))

        stats = _prune_inference_noise(g)

        assert stats["dp_range_mismatch_removed"] >= 1
        assert (ge_inst, gasId, Literal("GS0001", datatype=XSD.string)) in g
        assert (ge_inst, gasId, Literal("1220.26", datatype=XSD.decimal)) not in g

    def test_removes_op_literal_object(self):
        """Pattern 6b: ObjectProperty 의 object 가 literal 이면 제거.

        관찰된 reasoner 오염: hasEquipmentStatus OP 의 object 에 decimal
        literal 이 섞임 (실제 원본: 다른 DP 값).
        """
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        EQ_CLS = URIRef(f"{DOMAIN_NS}EquipmentMaster")
        ES_CLS = URIRef(f"{DOMAIN_NS}EquipmentStatus")
        hes = URIRef(f"{DOMAIN_NS}hasEquipmentStatus")
        g.add((EQ_CLS, RDF.type, OWL.Class))
        g.add((ES_CLS, RDF.type, OWL.Class))
        g.add((hes, RDF.type, OWL.ObjectProperty))

        eq_inst = URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_EQ001")
        es_inst = URIRef(f"{DOMAIN_INST_NS}EquipmentStatus_ES001")
        g.add((eq_inst, RDF.type, EQ_CLS))
        g.add((es_inst, RDF.type, ES_CLS))
        g.add((eq_inst, hes, es_inst))                 # legitimate
        g.add((eq_inst, hes, Literal("1210.48")))      # foreign literal

        stats = _prune_inference_noise(g)

        assert stats["op_literal_object_removed"] >= 1
        assert (eq_inst, hes, es_inst) in g
        assert (eq_inst, hes, Literal("1210.48")) not in g

    def test_all_four_patterns_combined(self):
        """4가지 패턴을 동시에 포함한 그래프에서 모두 제거."""
        from tools.inference import _prune_inference_noise

        g = _make_graph_with_noise()
        size_before = len(g)

        stats = _prune_inference_noise(g)

        assert stats["self_sameAs_removed"] == 2
        assert stats["self_equivalentClass_removed"] == 1
        assert stats["implicit_subClassOf_removed"] == 2
        assert stats["bnode_self_ref_removed"] == 2
        assert stats["total_pruned"] == 7
        assert stats["graph_size_before"] == size_before
        assert stats["graph_size_after"] == size_before - 7
        assert stats["reduction_pct"] == round(7 / size_before * 100, 2)

    def test_empty_graph(self):
        """빈 그래프에서 아무것도 제거하지 않는다."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        stats = _prune_inference_noise(g)

        assert stats["total_pruned"] == 0
        assert stats["graph_size_before"] == 0
        assert stats["graph_size_after"] == 0
        assert stats["reduction_pct"] == 0.0

    def test_no_noise_graph(self):
        """노이즈 없는 그래프에서 아무것도 제거하지 않는다."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        cls_a = URIRef(f"{DOMAIN_NS}ClassA")
        cls_b = URIRef(f"{DOMAIN_NS}ClassB")
        inst1 = URIRef(f"{DOMAIN_INST_NS}Inst_001")
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((inst1, RDF.type, cls_a))
        g.add((cls_a, RDFS.subClassOf, cls_b))

        size_before = len(g)
        stats = _prune_inference_noise(g)

        assert stats["total_pruned"] == 0
        assert len(g) == size_before

    def test_in_place_modification(self):
        """그래프가 in-place로 수정되는지 확인."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        inst1 = URIRef(f"{DOMAIN_INST_NS}Inst_001")
        g.add((inst1, OWL.sameAs, inst1))

        original_id = id(g)
        _prune_inference_noise(g)

        assert id(g) == original_id  # 같은 객체
        assert len(g) == 0  # 노이즈만 있었으므로 빈 그래프

    def test_preserves_valid_triples(self):
        """유효한 트리플은 하나도 제거하지 않는다."""
        from tools.inference import _prune_inference_noise

        g = _new_graph()
        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS

        # 유효한 트리플들
        valid_triples = [
            (URIRef(f"{inst}A"), OWL.sameAs, URIRef(f"{inst}B")),
            (URIRef(f"{ns}X"), OWL.equivalentClass, URIRef(f"{ns}Y")),
            (URIRef(f"{ns}Sub"), RDFS.subClassOf, URIRef(f"{ns}Super")),
            (URIRef(f"{inst}I"), RDF.type, URIRef(f"{ns}C")),
        ]
        for t in valid_triples:
            g.add(t)

        stats = _prune_inference_noise(g)

        assert stats["total_pruned"] == 0
        assert len(g) == len(valid_triples)
        for t in valid_triples:
            assert t in g


# ── run_owl_rl_inference 통합 — noise_pruning 키 확인 ──


class TestInferenceDietIntegration:
    """run_owl_rl_inference 반환 JSON에 noise_pruning 키가 포함되는지 확인."""

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    def test_noise_pruning_in_result(
        self, mock_merge, mock_closure, mock_getsize, mock_exists,
        mock_invalidate, mock_write, mock_quality,
    ):
        """noise_pruning 키가 결과 JSON에 존재하고 올바른 구조를 갖는다."""
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _new_graph()
        ns = DOMAIN_NS
        inst = DOMAIN_INST_NS
        cls_a = URIRef(f"{ns}ClassA")
        inst1 = URIRef(f"{inst}Inst_001")
        g.add((cls_a, RDF.type, OWL.Class))
        g.add((inst1, RDF.type, cls_a))
        # 추론 후 추가될 노이즈를 시뮬레이션: expand()가 그래프에 추가
        g.add((inst1, OWL.sameAs, inst1))  # self sameAs

        mock_merge.return_value = (g, 0)
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        with patch.object(Graph, "parse"):
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True
        assert "noise_pruning" in result
        pruning = result["noise_pruning"]
        assert "total_pruned" in pruning
        assert "self_sameAs_removed" in pruning
        assert "self_equivalentClass_removed" in pruning
        assert "implicit_subClassOf_removed" in pruning
        assert "bnode_self_ref_removed" in pruning
        assert "graph_size_before" in pruning
        assert "graph_size_after" in pruning
        assert "reduction_pct" in pruning

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    def test_zero_noise_still_has_key(
        self, mock_merge, mock_closure, mock_getsize, mock_exists,
        mock_invalidate, mock_write, mock_quality,
    ):
        """노이즈가 0건이어도 noise_pruning 키는 존재해야 한다."""
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _new_graph()
        cls_a = URIRef(f"{DOMAIN_NS}ClassA")
        g.add((cls_a, RDF.type, OWL.Class))

        mock_merge.return_value = (g, 0)
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        with patch.object(Graph, "parse"):
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True
        assert "noise_pruning" in result
        assert result["noise_pruning"]["total_pruned"] == 0
