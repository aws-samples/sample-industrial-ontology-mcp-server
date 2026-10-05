"""tests for A-Box normalization — 값 오버랩 FK 감지 + unverified FK 승격."""

from rdflib import URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
from domain.tbox_utils import _new_graph

# ── _detect_fk_by_value_overlap 테스트 ──────────────


class TestDetectFkByValueOverlap:
    """값 오버랩 기반 FK 타겟 클래스 추정 테스트."""

    def setup_method(self):
        from tools.abox_generation import _detect_fk_by_value_overlap
        self.detect = _detect_fk_by_value_overlap

    def test_perfect_overlap_returns_class(self):
        """100% 오버랩 → 타겟 클래스 반환."""
        col_values = {"EQ001", "EQ002", "EQ003"}
        pk_index = {"EquipmentMaster": {"EQ001", "EQ002", "EQ003", "EQ004"}}
        result = self.detect(col_values, pk_index)
        assert result == "EquipmentMaster"

    def test_above_threshold_returns_class(self):
        """80% 이상 오버랩 → 타겟 클래스 반환."""
        col_values = {"EQ001", "EQ002", "EQ003", "EQ004", "EQ005"}
        # 4/5 = 80% 매칭
        pk_index = {"EquipmentMaster": {"EQ001", "EQ002", "EQ003", "EQ004"}}
        result = self.detect(col_values, pk_index)
        assert result == "EquipmentMaster"

    def test_below_threshold_returns_none(self):
        """80% 미만 오버랩 → None."""
        col_values = {"EQ001", "EQ002", "EQ003", "EQ004", "EQ005"}
        # 3/5 = 60% 매칭
        pk_index = {"EquipmentMaster": {"EQ001", "EQ002", "EQ003"}}
        result = self.detect(col_values, pk_index)
        assert result is None

    def test_custom_threshold(self):
        """커스텀 임계값 적용."""
        col_values = {"EQ001", "EQ002", "EQ003", "EQ004", "EQ005"}
        # 3/5 = 60% — threshold=0.5면 통과
        pk_index = {"EquipmentMaster": {"EQ001", "EQ002", "EQ003"}}
        result = self.detect(col_values, pk_index, threshold=0.5)
        assert result == "EquipmentMaster"

    def test_empty_col_values_returns_none(self):
        """빈 컬럼 값 집합 → None."""
        pk_index = {"EquipmentMaster": {"EQ001"}}
        result = self.detect(set(), pk_index)
        assert result is None

    def test_empty_pk_index_returns_none(self):
        """빈 PK 인덱스 → None."""
        col_values = {"EQ001", "EQ002"}
        result = self.detect(col_values, {})
        assert result is None

    def test_empty_pk_values_skipped(self):
        """PK 값이 빈 클래스는 건너뜀."""
        col_values = {"EQ001", "EQ002"}
        pk_index = {"EquipmentMaster": set(), "ProcessMaster": {"EQ001", "EQ002"}}
        result = self.detect(col_values, pk_index)
        assert result == "ProcessMaster"

    def test_best_overlap_wins(self):
        """여러 클래스 중 오버랩이 가장 높은 클래스 선택."""
        col_values = {"EQ001", "EQ002", "EQ003", "EQ004", "EQ005"}
        pk_index = {
            "EquipmentMaster": {"EQ001", "EQ002", "EQ003", "EQ004"},  # 80%
            "ProcessMaster": {"EQ001", "EQ002", "EQ003", "EQ004", "EQ005"},  # 100%
        }
        result = self.detect(col_values, pk_index)
        assert result == "ProcessMaster"

    def test_no_overlap_returns_none(self):
        """오버랩 0% → None."""
        col_values = {"A", "B", "C"}
        pk_index = {"EquipmentMaster": {"X", "Y", "Z"}}
        result = self.detect(col_values, pk_index)
        assert result is None

    def test_exact_threshold_boundary(self):
        """정확히 threshold와 같은 비율 → 통과."""
        col_values = {"EQ001", "EQ002", "EQ003", "EQ004", "EQ005"}
        # 4/5 = 0.8 = threshold
        pk_index = {"EquipmentMaster": {"EQ001", "EQ002", "EQ003", "EQ004"}}
        result = self.detect(col_values, pk_index, threshold=0.8)
        assert result == "EquipmentMaster"


# ── _should_promote_unverified_fk 테스트 ────────────


class TestShouldPromoteUnverifiedFk:
    """Unverified FK master_data 기반 승격 판정 테스트."""

    def setup_method(self):
        from tools.abox_generation import _should_promote_unverified_fk
        self.promote = _should_promote_unverified_fk

    def test_target_exists_in_master(self):
        """타겟 URI가 master_instance_uris에 있으면 승격."""
        master = {f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"}
        assert self.promote(f"{DOMAIN_INST_NS}EquipmentMaster_EQ001", master) is True

    def test_target_not_in_master(self):
        """타겟 URI가 master_instance_uris에 없으면 비승격."""
        master = {f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"}
        assert self.promote(f"{DOMAIN_INST_NS}EquipmentMaster_EQ999", master) is False

    def test_empty_master_set(self):
        """빈 master set → 항상 False."""
        assert self.promote(f"{DOMAIN_INST_NS}EquipmentMaster_EQ001", set()) is False

    def test_different_class_uri(self):
        """다른 클래스의 인스턴스 URI → False."""
        master = {f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"}
        assert self.promote(f"{DOMAIN_INST_NS}ProcessMaster_PS001", master) is False


# ── generate_abox 통합: promoted_fk_count 반환 테스트 ──


class TestPromotedFkInGenerateAbox:
    """generate_abox 반환 JSON에 promoted_fk_count가 포함되는지 통합 테스트.

    실제 generate_abox는 CSV/T-Box 의존이 크므로, unverified FK 승격 로직의
    핵심 경로만 단위 테스트로 검증한다.
    """

    def test_promotion_flow(self):
        """master_data에 존재하는 타겟 → 트리플 생성 + promoted_fk_count 증가 시뮬레이션."""
        from tools.abox_generation import _should_promote_unverified_fk

        # master_data에 EquipmentMaster_EQ001 존재
        master_uris = {f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"}

        # unverified FK 항목 시뮬레이션
        fk_item = ("__unverified__", "usesEquipment",
                    URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"),
                    "EquipmentMaster")

        target_uri_str = str(fk_item[2])
        promoted = _should_promote_unverified_fk(target_uri_str, master_uris)
        assert promoted is True

        # 승격 시 트리플 생성 시뮬레이션
        g = _new_graph()
        inst_uri = DOMAIN_INST_NS_OBJ["ProcessData_PD001"]
        if promoted:
            g.add((inst_uri, DOMAIN_NS_OBJ[fk_item[1]], fk_item[2]))

        triples = list(g.triples((inst_uri, DOMAIN_NS_OBJ["usesEquipment"], None)))
        assert len(triples) == 1

    def test_non_promotion_flow(self):
        """master_data에 없는 타겟 → 트리플 미생성."""
        from tools.abox_generation import _should_promote_unverified_fk

        master_uris = {f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"}

        fk_item = ("__unverified__", "usesEquipment",
                    URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_EQ999"),
                    "EquipmentMaster")

        target_uri_str = str(fk_item[2])
        promoted = _should_promote_unverified_fk(target_uri_str, master_uris)
        assert promoted is False

        # 비승격 시 트리플 미생성
        g = _new_graph()
        inst_uri = DOMAIN_INST_NS_OBJ["ProcessData_PD001"]
        if promoted:
            g.add((inst_uri, DOMAIN_NS_OBJ[fk_item[1]], fk_item[2]))

        triples = list(g.triples((inst_uri, DOMAIN_NS_OBJ["usesEquipment"], None)))
        assert len(triples) == 0
