"""Tests for M6 Adaptive Quality Gates — 티어 기반 적응형 품질 게이트.

_classify_class_tiers() 분류 로직과, 4개 검증 함수의 class_tiers 파라미터 동작을 검증한다.
"""

import json
from unittest.mock import patch

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph

# ── 헬퍼 ──────────────────────────────────────────


def _make_tbox_with_classes(*class_names: str) -> Graph:
    """지정된 클래스명으로 T-Box 그래프 생성."""
    g = _new_graph()
    for name in class_names:
        g.add((URIRef(f"{DOMAIN_NS}{name}"), RDF.type, OWL.Class))
    return g


def _make_abox_with_instances(tbox: Graph, class_instances: dict[str, int]) -> Graph:
    """클래스별 인스턴스 수를 지정하여 A-Box 그래프 생성.

    class_instances: {"EquipmentMaster": 3, "AlarmEvents": 600}
    """
    g = _new_graph()
    for s, p, o in tbox:
        g.add((s, p, o))
    from domain.namespaces import bind_namespaces
    bind_namespaces(g)

    for cls_name, count in class_instances.items():
        cls_uri = URIRef(f"{DOMAIN_NS}{cls_name}")
        for i in range(count):
            inst = URIRef(f"{DOMAIN_INST_NS}{cls_name}_{i:04d}")
            g.add((inst, RDF.type, cls_uri))
    return g


def _make_abox_stats(per_class: dict[str, int]) -> dict:
    """테스트용 abox_stats.json 구조 생성.

    per_class: {"EquipmentMaster": 100, "AlarmEvents": 600, "InferredClass": 0}
    """
    return {
        "per_class": {
            name: {"instance_count": count, "op_triples": count * 2}
            for name, count in per_class.items()
        }
    }


# ── _classify_class_tiers ─────────────────────────


class TestClassifyClassTiers:
    """클래스 티어 분류 테스트."""

    def test_zero_instances_inferred(self):
        """instance_count == 0 → inferred 티어."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({"InferredClass": 0})
        tiers = _classify_class_tiers(stats)
        assert tiers["InferredClass"] == "inferred"

    def test_small_count_master(self):
        """instance_count <= 500 → master 티어."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({"EquipmentMaster": 100})
        tiers = _classify_class_tiers(stats)
        assert tiers["EquipmentMaster"] == "master"

    def test_boundary_500_master(self):
        """instance_count == 500 → master 티어 (경계값)."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({"Boundary": 500})
        tiers = _classify_class_tiers(stats)
        assert tiers["Boundary"] == "master"

    def test_large_count_transaction(self):
        """instance_count > 500 → transaction 티어."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({"AlarmEvents": 601})
        tiers = _classify_class_tiers(stats)
        assert tiers["AlarmEvents"] == "transaction"

    def test_mixed_tiers(self):
        """여러 클래스가 서로 다른 티어로 분류."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({
            "EquipmentMaster": 50,
            "AlarmEvents": 1000,
            "InferredClass": 0,
        })
        tiers = _classify_class_tiers(stats)
        assert tiers == {
            "EquipmentMaster": "master",
            "AlarmEvents": "transaction",
            "InferredClass": "inferred",
        }

    def test_txn_like_suffix_overrides_to_transaction(self):
        """R7: History/Plan/Map 접미사는 instance 수 무관하게 transaction tier."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({
            "MaintenanceHistory": 102,
            "ProductionPlan": 157,
            "ItemSupplierMap": 98,
            "EquipmentMaster": 50,  # master 접미사는 master 유지
        })
        tiers = _classify_class_tiers(stats)
        assert tiers["MaintenanceHistory"] == "transaction"
        assert tiers["ProductionPlan"] == "transaction"
        assert tiers["ItemSupplierMap"] == "transaction"
        assert tiers["EquipmentMaster"] == "master"

    def test_empty_stats(self):
        """per_class가 비어있으면 빈 dict 반환."""
        from tools.kg_validation import _classify_class_tiers

        stats = _make_abox_stats({})
        tiers = _classify_class_tiers(stats)
        assert tiers == {}

    def test_missing_per_class_key(self):
        """per_class 키가 없으면 빈 dict 반환."""
        from tools.kg_validation import _classify_class_tiers

        tiers = _classify_class_tiers({})
        assert tiers == {}


# ── TIER_THRESHOLDS 상수 ──────────────────────────


class TestTierThresholds:
    """TIER_THRESHOLDS 상수 구조 검증."""

    def test_all_tiers_present(self):
        from tools.kg_validation import TIER_THRESHOLDS

        assert set(TIER_THRESHOLDS.keys()) == {"master", "catalog", "transaction", "inferred"}

    def test_master_strictest_dangling(self):
        """master가 가장 엄격한 dangling_rate."""
        from tools.kg_validation import TIER_THRESHOLDS

        assert TIER_THRESHOLDS["master"]["dangling_rate"] < TIER_THRESHOLDS["transaction"]["dangling_rate"]
        assert TIER_THRESHOLDS["transaction"]["dangling_rate"] < TIER_THRESHOLDS["inferred"]["dangling_rate"]

    def test_master_highest_coverage(self):
        """master가 가장 높은 property_coverage 요구."""
        from tools.kg_validation import TIER_THRESHOLDS

        assert TIER_THRESHOLDS["master"]["property_coverage"] > TIER_THRESHOLDS["transaction"]["property_coverage"]
        assert TIER_THRESHOLDS["transaction"]["property_coverage"] > TIER_THRESHOLDS["inferred"]["property_coverage"]

    def test_inferred_allows_no_instance(self):
        """inferred만 no_instance_allowed=True."""
        from tools.kg_validation import TIER_THRESHOLDS

        assert TIER_THRESHOLDS["inferred"]["no_instance_allowed"] is True
        assert TIER_THRESHOLDS["master"]["no_instance_allowed"] is False
        assert TIER_THRESHOLDS["transaction"]["no_instance_allowed"] is False


# ── _check_class_instance_count + class_tiers ─────


class TestCheckClassInstanceCountAdaptive:
    """클래스별 인스턴스 수 검증 — 적응형 게이트."""

    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("os.path.exists", return_value=False)
    def test_backward_compat_none(self, mock_exists):
        """class_tiers=None이면 기존 로직 유지."""
        from tools.kg_validation import _check_class_instance_count

        g = _new_graph()
        result = _check_class_instance_count(g, class_tiers=None)
        assert "name" in result
        assert "passed" in result

    def test_inferred_classes_excluded(self, tmp_path):
        """inferred 티어 클래스는 no_instance 목록에서 제외."""
        from tools.kg_validation import _check_class_instance_count

        # T-Box에 3개 클래스 정의
        tbox = _make_tbox_with_classes("EquipmentMaster", "AlarmEvents", "InferredClass")
        tbox_path = tmp_path / "tbox.ttl"
        tbox.serialize(str(tbox_path), format="turtle")

        # A-Box: EquipmentMaster, AlarmEvents만 인스턴스 있음
        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)
        g.add((URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_001"), RDF.type, URIRef(f"{DOMAIN_NS}EquipmentMaster")))
        g.add((URIRef(f"{DOMAIN_INST_NS}AlarmEvents_001"), RDF.type, URIRef(f"{DOMAIN_NS}AlarmEvents")))

        # InferredClass는 inferred 티어 → no_instance에서 제외
        class_tiers = {"EquipmentMaster": "master", "AlarmEvents": "transaction", "InferredClass": "inferred"}

        with patch("tools.kg_validation.TBOX_PATH", str(tbox_path)), patch("os.path.exists", return_value=True):
            result = _check_class_instance_count(g, class_tiers=class_tiers)

        # InferredClass가 no_instance_classes에서 제외되어 통과
        assert "InferredClass" not in result["no_instance_classes"]
        assert result["passed"] is True

    def test_without_tiers_inferred_counted(self, tmp_path):
        """class_tiers 없으면 InferredClass도 no_instance에 포함."""
        from tools.kg_validation import _check_class_instance_count

        tbox = _make_tbox_with_classes("EquipmentMaster", "InferredClass")
        tbox_path = tmp_path / "tbox.ttl"
        tbox.serialize(str(tbox_path), format="turtle")

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)
        g.add((URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_001"), RDF.type, URIRef(f"{DOMAIN_NS}EquipmentMaster")))

        with patch("tools.kg_validation.TBOX_PATH", str(tbox_path)), patch("os.path.exists", return_value=True):
            result = _check_class_instance_count(g, class_tiers=None)

        # class_tiers 없으면 InferredClass가 no_instance에 포함
        assert "InferredClass" in result["no_instance_classes"]


# ── _check_property_coverage + class_tiers ────────


class TestCheckPropertyCoverageAdaptive:
    """프로퍼티 커버리지 — 적응형 임계값."""

    def test_backward_compat_none(self):
        """class_tiers=None이면 기존 임계값(50%) 사용."""
        from tools.kg_validation import _check_property_coverage

        tbox = _new_graph()
        g = _new_graph()
        result = _check_property_coverage(g, tbox, class_tiers=None)
        assert "name" in result
        assert "passed" in result

    def test_inferred_tier_lowers_threshold(self):
        """inferred 티어만 있으면 임계값이 30%로 낮아짐."""
        from tools.kg_validation import _check_property_coverage

        tbox = _new_graph()
        ns = DOMAIN_NS

        # 10개 프로퍼티 정의, 4개만 사용 (40% 커버리지)
        cls_uri = URIRef(f"{ns}TestClass")
        tbox.add((cls_uri, RDF.type, OWL.Class))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        for i in range(10):
            prop = URIRef(f"{ns}prop{i}")
            tbox.add((prop, RDF.type, OWL.DatatypeProperty))
            tbox.add((prop, RDFS.domain, cls_uri))

        # 4개 프로퍼티만 사용 (40%)
        inst = URIRef(f"{DOMAIN_INST_NS}TestClass_001")
        g.add((inst, RDF.type, cls_uri))
        for i in range(4):
            g.add((inst, URIRef(f"{ns}prop{i}"), Literal(f"val{i}")))

        # class_tiers=None → 50% 기준 → 40%는 FAIL
        result_no_tiers = _check_property_coverage(g, tbox, class_tiers=None)
        assert result_no_tiers["passed"] is False

        # inferred 티어만 → 30% 기준 → 40%는 PASS
        class_tiers = {"TestClass": "inferred"}
        result_with_tiers = _check_property_coverage(g, tbox, class_tiers=class_tiers)
        assert result_with_tiers["passed"] is True

    def test_mixed_tiers_uses_minimum(self):
        """혼합 티어 시 최소 임계값 사용 (inferred 30%)."""
        from tools.kg_validation import _check_property_coverage

        tbox = _new_graph()
        ns = DOMAIN_NS
        cls1 = URIRef(f"{ns}MasterClass")
        cls2 = URIRef(f"{ns}InferredClass")
        tbox.add((cls1, RDF.type, OWL.Class))
        tbox.add((cls2, RDF.type, OWL.Class))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        # 10 프로퍼티, 4개 사용 (40%)
        for i in range(10):
            prop = URIRef(f"{ns}prop{i}")
            tbox.add((prop, RDF.type, OWL.DatatypeProperty))
            tbox.add((prop, RDFS.domain, cls1))

        inst = URIRef(f"{DOMAIN_INST_NS}MasterClass_001")
        g.add((inst, RDF.type, cls1))
        for i in range(4):
            g.add((inst, URIRef(f"{ns}prop{i}"), Literal(f"val{i}")))

        # master(80) + inferred(30) → min=30 → 40%는 PASS
        class_tiers = {"MasterClass": "master", "InferredClass": "inferred"}
        result = _check_property_coverage(g, tbox, class_tiers=class_tiers)
        assert result["passed"] is True


# ── _check_dangling_references + class_tiers ──────


class TestCheckDanglingReferencesAdaptive:
    """댕글링 참조 — 적응형 임계값."""

    def test_backward_compat_none(self):
        """class_tiers=None이면 기존 임계값(5%) 사용."""
        from tools.kg_validation import _check_dangling_references

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)
        result = _check_dangling_references(g, class_tiers=None)
        assert "name" in result
        assert "passed" in result

    def test_master_tier_strict_threshold(self):
        """master 티어만 → 1% 임계값 적용."""
        from tools.kg_validation import _check_dangling_references

        ns = DOMAIN_NS
        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        cls_uri = URIRef(f"{ns}TestClass")
        prop_uri = URIRef(f"{ns}hasRef")

        # 100 OP 트리플 생성, 3개 댕글링 (3%)
        for i in range(100):
            subj = URIRef(f"{DOMAIN_INST_NS}TestClass_{i:04d}")
            target = URIRef(f"{DOMAIN_INST_NS}Target_{i:04d}")
            g.add((subj, RDF.type, cls_uri))
            g.add((subj, prop_uri, target))
            if i >= 3:  # 처음 3개는 타입 없음 (댕글링)
                g.add((target, RDF.type, cls_uri))

        # class_tiers=None → 5% 기준 → 3%는 PASS
        result_no_tiers = _check_dangling_references(g, class_tiers=None)
        assert result_no_tiers["passed"] is True

        # master만 → 1% 기준 → 3%는 FAIL
        class_tiers = {"TestClass": "master"}
        result_master = _check_dangling_references(g, class_tiers=class_tiers)
        assert result_master["passed"] is False

    def test_strictest_tier_applied(self):
        """혼합 티어 시 가장 엄격한 임계값 적용 (master=1%)."""
        from tools.kg_validation import _check_dangling_references

        ns = DOMAIN_NS
        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        cls_uri = URIRef(f"{ns}TestClass")
        prop_uri = URIRef(f"{ns}hasRef")

        # 100 OP, 3 댕글링 (3%)
        for i in range(100):
            subj = URIRef(f"{DOMAIN_INST_NS}TestClass_{i:04d}")
            target = URIRef(f"{DOMAIN_INST_NS}Target_{i:04d}")
            g.add((subj, RDF.type, cls_uri))
            g.add((subj, prop_uri, target))
            if i >= 3:
                g.add((target, RDF.type, cls_uri))

        # master(1%) + transaction(5%) → strictest=1% → 3% FAIL
        class_tiers = {"TestClass": "master", "Other": "transaction"}
        result = _check_dangling_references(g, class_tiers=class_tiers)
        assert result["passed"] is False


# ── _check_string_patterns + class_tiers ──────────


class TestCheckStringPatternsAdaptive:
    """문자열 패턴 — 적응형 집중도 임계값."""

    def test_backward_compat_none(self):
        """class_tiers=None이면 기존 임계값(0.9) 사용."""
        from tools.kg_validation import _check_string_patterns

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)
        result = _check_string_patterns(g, class_tiers=None)
        assert "name" in result
        assert "passed" in result

    def test_master_tier_strictest_concentration(self):
        """master 티어 → 95% 집중도 임계값 (가장 관대).

        단일값 집중은 이제 '경고(warnings)' 로만 기록되고 PASS 판정에는
        포함되지 않는다. 대신 warning_count 로 티어별 임계값 적용을 확인.
        """
        from tools.kg_validation import _check_string_patterns

        ns = DOMAIN_NS
        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        prop_uri = URIRef(f"{ns}statusCode")

        # 100개 중 92개 동일 (92% 집중도)
        for i in range(100):
            inst = URIRef(f"{DOMAIN_INST_NS}Item_{i:04d}")
            val = "ACTIVE" if i < 92 else f"OTHER_{i}"
            g.add((inst, prop_uri, Literal(val, datatype=XSD.string)))

        # class_tiers=None → 90% 기준 → 92% 는 경고로 기록되지만 PASS
        # (단일값 집중은 invalid 문자열이 아니므로 PASS)
        result_no_tiers = _check_string_patterns(g, class_tiers=None)
        assert result_no_tiers["passed"] is True
        assert result_no_tiers["warning_count"] >= 1  # 경고는 기록됨
        assert result_no_tiers["invalid_warning_count"] == 0

        # master 티어 → max(0.95) → 92%는 PASS (아예 warning 도 미발생)
        class_tiers = {"Item": "master"}
        result_master = _check_string_patterns(g, class_tiers=class_tiers)
        assert result_master["passed"] is True
        assert result_master["warning_count"] == 0

    def test_mixed_tiers_uses_max(self):
        """혼합 티어 시 가장 관대한 임계값 사용 (master=0.95)."""
        from tools.kg_validation import _check_string_patterns

        ns = DOMAIN_NS
        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        prop_uri = URIRef(f"{ns}statusCode")

        # 100개 중 92개 동일 (92%)
        for i in range(100):
            inst = URIRef(f"{DOMAIN_INST_NS}Item_{i:04d}")
            val = "ACTIVE" if i < 92 else f"OTHER_{i}"
            g.add((inst, prop_uri, Literal(val, datatype=XSD.string)))

        # transaction(0.85) + master(0.95) → max=0.95 → 92%는 PASS
        class_tiers = {"ClassA": "transaction", "ClassB": "master"}
        result = _check_string_patterns(g, class_tiers=class_tiers)
        assert result["passed"] is True


# ── validate_kg 통합 (class_tiers 전달) ───────────


class TestValidateKgAdaptiveTiersWiring:
    """validate_kg()에서 class_tiers가 4개 검증에 전달되는지 검증."""

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists", return_value=False)
    def test_without_abox_stats_still_works(self, mock_exists, mock_load_graph):
        """abox_stats.json 없어도 기존처럼 동작 (class_tiers=None 폴백)."""
        from tools.kg_validation import validate_kg

        mock_load_graph.return_value = _new_graph()
        result = json.loads(validate_kg(use_inferred=False))
        assert result["success"] is True
        assert len(result["checks"]) == 23

    @patch("tools.kg_validation._load_abox_stats")
    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists", return_value=False)
    def test_abox_stats_loaded_and_classified(
        self, mock_exists, mock_load_graph, mock_load_stats
    ):
        """abox_stats.json이 있으면 _classify_class_tiers 호출."""
        from tools.kg_validation import validate_kg

        mock_load_graph.return_value = _new_graph()
        mock_load_stats.return_value = _make_abox_stats({
            "EquipmentMaster": 100,
            "AlarmEvents": 600,
        })

        result = json.loads(validate_kg(use_inferred=False))
        assert result["success"] is True
        mock_load_stats.assert_called_once()

    @patch("tools.kg_validation._load_abox_stats")
    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists", return_value=False)
    def test_abox_stats_exception_graceful_fallback(
        self, mock_exists, mock_load_graph, mock_load_stats
    ):
        """abox_stats 로드 실패 시 class_tiers=None으로 폴백."""
        from tools.kg_validation import validate_kg

        mock_load_graph.return_value = _new_graph()
        mock_load_stats.side_effect = Exception("file corrupt")

        result = json.loads(validate_kg(use_inferred=False))
        assert result["success"] is True
        assert len(result["checks"]) == 23


# ── _check_tbox_fitness + class_tiers ────────────


class TestCheckTboxFitnessAdaptive:
    """T-Box Fitness — 적응형 임계값."""

    def test_backward_compat_none(self):
        """class_tiers=None이면 기존 임계값(50%) 사용."""
        from tools.kg_validation import _check_tbox_fitness

        tbox = _new_graph()
        result = _check_tbox_fitness(tbox, class_tiers=None)
        assert "name" in result
        assert "passed" in result

    @patch("tools.kg_validation.SOURCE_RAWDATA_DIR", "/nonexistent")
    @patch("tools.kg_validation.SOURCE_TACIT_DIR", "/nonexistent")
    def test_inferred_tier_lowers_threshold(self):
        """inferred 티어만 있으면 임계값이 30%로 낮아짐."""
        from tools.kg_validation import _check_tbox_fitness

        tbox = _new_graph()
        # 10개 클래스 정의 (CSV 소스 없으므로 fitness=0%)
        for i in range(10):
            tbox.add((URIRef(f"{DOMAIN_NS}Class{i}"), RDF.type, OWL.Class))

        # class_tiers=None → 50% 기준 → 0%는 FAIL
        result_no_tiers = _check_tbox_fitness(tbox, class_tiers=None)
        assert result_no_tiers["passed"] is False

        # inferred 티어만 → 30% 기준 → 0%는 여전히 FAIL
        class_tiers = {"Class0": "inferred"}
        result_inferred = _check_tbox_fitness(tbox, class_tiers=class_tiers)
        assert result_inferred["passed"] is False

    @patch("tools.kg_validation.SOURCE_RAWDATA_DIR", "/nonexistent")
    @patch("tools.kg_validation.SOURCE_TACIT_DIR", "/nonexistent")
    def test_threshold_changes_with_tiers(self):
        """master(80) vs inferred(30) — TIER_THRESHOLDS 값 확인."""
        from tools.kg_validation import TIER_THRESHOLDS, _check_tbox_fitness

        # master=80, inferred=30 확인
        assert TIER_THRESHOLDS["master"]["property_coverage"] == 80
        assert TIER_THRESHOLDS["inferred"]["property_coverage"] == 30

        tbox = _new_graph()
        tbox.add((URIRef(f"{DOMAIN_NS}TestClass"), RDF.type, OWL.Class))

        # fitness=0% (소스 없음) — 모든 티어에서 FAIL
        result_master = _check_tbox_fitness(
            tbox, class_tiers={"TestClass": "master"}
        )
        assert result_master["passed"] is False

        result_inferred = _check_tbox_fitness(
            tbox, class_tiers={"TestClass": "inferred"}
        )
        assert result_inferred["passed"] is False

    @patch("tools.kg_validation.SOURCE_RAWDATA_DIR", "/nonexistent")
    @patch("tools.kg_validation.SOURCE_TACIT_DIR", "/nonexistent")
    def test_mixed_tiers_uses_minimum(self):
        """혼합 티어 시 최소 임계값 사용 (inferred 30%)."""
        from tools.kg_validation import _check_tbox_fitness

        tbox = _new_graph()
        tbox.add((URIRef(f"{DOMAIN_NS}TestClass"), RDF.type, OWL.Class))

        # master(80) + inferred(30) → min=30
        class_tiers = {"MasterClass": "master", "InferredClass": "inferred"}
        result = _check_tbox_fitness(tbox, class_tiers=class_tiers)
        # fitness=0% < 30% → FAIL
        assert result["passed"] is False

    def test_empty_tbox_always_passes(self):
        """T-Box에 클래스 없으면 항상 통과 (티어 무관)."""
        from tools.kg_validation import _check_tbox_fitness

        tbox = _new_graph()
        result = _check_tbox_fitness(
            tbox, class_tiers={"Something": "master"}
        )
        assert result["passed"] is True
        assert result["fitness"] == 0


# ── _check_property_completeness + class_tiers ───


class TestCheckPropertyCompletenessAdaptive:
    """프로퍼티별 완전성 — 적응형 임계값."""

    def test_backward_compat_none(self):
        """class_tiers=None이면 기존 임계값(50%) 사용."""
        from tools.kg_validation import _check_property_completeness

        tbox = _new_graph()
        g = _new_graph()
        result = _check_property_completeness(g, tbox, class_tiers=None)
        assert "name" in result
        assert "passed" in result

    def test_inferred_tier_lowers_threshold(self, monkeypatch):
        """inferred 티어만 → 30% 기준 → 20% 완전성은 PASS, 기본 50%는 FAIL.

        Default threshold is 30% (env PROP_COMPLETENESS_MIN_PCT); override to
        50% to exercise the 'no-tier baseline' FAIL path, then confirm the
        inferred-tier 30% path still PASSes.
        """
        from tools.kg_validation import _check_property_completeness

        ns = DOMAIN_NS
        tbox = _new_graph()
        cls_uri = URIRef(f"{ns}TestClass")
        tbox.add((cls_uri, RDF.type, OWL.Class))

        # 1개 DP 정의
        prop_uri = URIRef(f"{ns}testProp")
        tbox.add((prop_uri, RDF.type, OWL.DatatypeProperty))
        tbox.add((prop_uri, RDFS.domain, cls_uri))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        # 10개 인스턴스, 4개만 값 있음 (40%)
        for i in range(10):
            inst = URIRef(f"{DOMAIN_INST_NS}TestClass_{i:04d}")
            g.add((inst, RDF.type, cls_uri))
            if i < 4:
                g.add((inst, prop_uri, Literal(f"val{i}")))

        # Force stricter baseline so 40% is FAIL without tier override.
        monkeypatch.setenv("PROP_COMPLETENESS_MIN_PCT", "50")
        result_no_tiers = _check_property_completeness(g, tbox, class_tiers=None)
        assert result_no_tiers["passed"] is False

        # inferred 티어만 → 30% 기준 → 40%는 PASS
        class_tiers = {"TestClass": "inferred"}
        result_inferred = _check_property_completeness(
            g, tbox, class_tiers=class_tiers
        )
        assert result_inferred["passed"] is True

    def test_master_tier_raises_threshold(self):
        """master 티어 → 80% 기준 → 60% 완전성은 FAIL."""
        from tools.kg_validation import _check_property_completeness

        ns = DOMAIN_NS
        tbox = _new_graph()
        cls_uri = URIRef(f"{ns}TestClass")
        tbox.add((cls_uri, RDF.type, OWL.Class))

        prop_uri = URIRef(f"{ns}testProp")
        tbox.add((prop_uri, RDF.type, OWL.DatatypeProperty))
        tbox.add((prop_uri, RDFS.domain, cls_uri))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        # 10개 인스턴스, 6개 값 있음 (60%)
        for i in range(10):
            inst = URIRef(f"{DOMAIN_INST_NS}TestClass_{i:04d}")
            g.add((inst, RDF.type, cls_uri))
            if i < 6:
                g.add((inst, prop_uri, Literal(f"val{i}")))

        # class_tiers=None → 50% 기준 → 60%는 PASS
        result_no_tiers = _check_property_completeness(g, tbox, class_tiers=None)
        assert result_no_tiers["passed"] is True

        # master 티어만 → 80% 기준 → 60%는 FAIL
        class_tiers = {"TestClass": "master"}
        result_master = _check_property_completeness(
            g, tbox, class_tiers=class_tiers
        )
        assert result_master["passed"] is False

    def test_mixed_tiers_uses_minimum(self):
        """혼합 티어 시 최소 임계값 사용 (inferred 30%)."""
        from tools.kg_validation import _check_property_completeness

        ns = DOMAIN_NS
        tbox = _new_graph()
        cls_uri = URIRef(f"{ns}TestClass")
        tbox.add((cls_uri, RDF.type, OWL.Class))

        prop_uri = URIRef(f"{ns}testProp")
        tbox.add((prop_uri, RDF.type, OWL.DatatypeProperty))
        tbox.add((prop_uri, RDFS.domain, cls_uri))

        g = _new_graph()
        from domain.namespaces import bind_namespaces
        bind_namespaces(g)

        # 10개 인스턴스, 4개 값 있음 (40%)
        for i in range(10):
            inst = URIRef(f"{DOMAIN_INST_NS}TestClass_{i:04d}")
            g.add((inst, RDF.type, cls_uri))
            if i < 4:
                g.add((inst, prop_uri, Literal(f"val{i}")))

        # master(80) + inferred(30) → min=30 → 40%는 PASS
        class_tiers = {"MasterClass": "master", "InferredClass": "inferred"}
        result = _check_property_completeness(g, tbox, class_tiers=class_tiers)
        assert result["passed"] is True

    def test_no_dp_always_passes(self):
        """T-Box에 DP 없으면 항상 통과 (티어 무관)."""
        from tools.kg_validation import _check_property_completeness

        tbox = _new_graph()
        g = _new_graph()
        result = _check_property_completeness(
            g, tbox, class_tiers={"Something": "master"}
        )
        assert result["passed"] is True
