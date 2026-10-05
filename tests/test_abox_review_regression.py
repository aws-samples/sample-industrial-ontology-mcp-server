"""Golden regression tests for A-Box generation review improvements.

이 파일은 비판 리뷰에서 식별한 정합성/신뢰성 이슈가 다시 회귀하지 않도록
작은 단위 테스트를 모아둔다. 테스트 케이스는 세 가지:

1. Unique PK URI 에 timestamp 가 접미로 붙지 않음 (M1).
2. master_data.ttl 분리 시 transaction 인스턴스가 섞이지 않음 (M7).
3. FK 실패 시 disjoint 그룹 내 rdf:type 이 롤백됨 (M11).
4. _format_value 가 센티널(미정)과 실제 coercion failure 를 구분 (M4).
5. _fk_column_to_class 가 separator/underscore 폴백 적중 시 로그 기록 (M9).
6. unmapped column 판정이 fk_patterns.json 과 동기화 (M5).
"""
from __future__ import annotations

import logging

from rdflib import RDF, Graph, Literal

from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
from tools.abox_generation import (
    _FK_FALLBACK_LOGGED,
    _build_loss_manifest,
    _detect_pk_column,
    _detect_pk_value,
    _deterministic_fix_abox,
    _fk_column_to_class,
    _is_null_sentinel,
    _split_master_subgraph,
)

# ── M1: Unique PK URI 에는 timestamp 가 붙지 않음 ─────────────────────


class TestUniquePkNoTimestamp:
    def test_unique_pk_skips_timestamp(self):
        rows = [
            {"Transaction_ID": "TXN001", "Timestamp": "2025-09-01 12:00"},
            {"Transaction_ID": "TXN002", "Timestamp": "2025-09-01 13:00"},
        ]
        pk_col = _detect_pk_column(rows, "InventoryTransaction")
        # 전부 unique 이므로 단일 str PK
        assert isinstance(pk_col, str)
        pk_val = _detect_pk_value(rows[0], "InventoryTransaction", pk_column=pk_col)
        # timestamp 접미 없어야 함
        assert pk_val == "TXN001"
        assert "2025" not in pk_val

    def test_composite_pk_retains_timestamp_fallback(self):
        # 단일 PK 로 unique 가 안 되는 케이스 — 복합 키로 결정.
        # 이 경우 timestamp append 는 허용되는 폴백 경로.
        rows = [
            {"Item_Code": "ITM01", "Warehouse_Code": "WH1"},
            {"Item_Code": "ITM01", "Warehouse_Code": "WH2"},
            {"Item_Code": "ITM02", "Warehouse_Code": "WH1"},
        ]
        pk_col = _detect_pk_column(rows, "InventoryStatus")
        # 복합 키 리스트
        assert isinstance(pk_col, list)
        # 복합 PK 경로에서는 timestamp append 가 본래 생략되지만 — 값 자체가
        # 복합이므로 URI 충돌을 피한다.
        pk_val = _detect_pk_value(rows[0], "InventoryStatus", pk_column=pk_col)
        assert pk_val is not None
        assert "ITM01" in pk_val
        assert "WH1" in pk_val


# ── M7: master_data 에 transaction subject 없음 ────────────────────


class TestMasterDataIsolation:
    def test_transaction_incoming_not_in_master_subgraph(self):
        """master 인스턴스를 가리키는 transaction 연결은 master_data 에서 제외.

        R21-4 에서 `ABOX_MASTER_INCLUDE_INCOMING_OP` 옵트인 기능이 추가됐지만
        기본값은 여전히 R15 원 동작 (분리). Closed-World validate 보호 때문.
        """
        g = _new_graph_stub()
        eq_uri = DOMAIN_INST_NS_OBJ["EquipmentMaster_EQ001"]
        txn_uri = DOMAIN_INST_NS_OBJ["ProductionResult_PR001"]
        g.add((eq_uri, RDF.type, DOMAIN_NS_OBJ["EquipmentMaster"]))
        g.add((eq_uri, DOMAIN_NS_OBJ["hasName"], Literal("고로1")))
        g.add((txn_uri, RDF.type, DOMAIN_NS_OBJ["ProductionResult"]))
        g.add((txn_uri, DOMAIN_NS_OBJ["usesEquipment"], eq_uri))

        master_g, count, master_uris = _split_master_subgraph(g, {"EquipmentMaster"})

        # master 인스턴스는 subject 로 있다
        master_subjects = {str(s) for s in master_g.subjects()}
        assert str(eq_uri) in master_subjects
        # transaction 인스턴스는 subject 로 나타나선 안 된다 (R15 원 동작)
        assert str(txn_uri) not in master_subjects
        # outgoing 트리플은 보존 (eq → hasName → literal)
        assert (eq_uri, DOMAIN_NS_OBJ["hasName"], Literal("고로1")) in master_g

    def test_master_to_master_relationships_preserved(self):
        """master ↔ master 간 관계는 유지."""
        g = _new_graph_stub()
        w_uri = DOMAIN_INST_NS_OBJ["WarehouseMaster_W1"]
        i_uri = DOMAIN_INST_NS_OBJ["ItemMaster_I1"]
        g.add((w_uri, RDF.type, DOMAIN_NS_OBJ["WarehouseMaster"]))
        g.add((i_uri, RDF.type, DOMAIN_NS_OBJ["ItemMaster"]))
        g.add((w_uri, DOMAIN_NS_OBJ["stores"], i_uri))

        master_g, _, _ = _split_master_subgraph(
            g, {"WarehouseMaster", "ItemMaster"},
        )
        assert (w_uri, DOMAIN_NS_OBJ["stores"], i_uri) in master_g


# ── M11: Disjoint 가드 — FK 주입 type 롤백 ─────────────────────────


class TestDisjointGuardWithFkInjection:
    def test_fk_added_type_rolled_back_on_disjoint_conflict(self):
        g = _new_graph_stub()
        target_uri = DOMAIN_INST_NS_OBJ["ItemMaster_I1"]
        # 기존에 이미 ProductMaster type 이 있다 (잘못된 FK 매칭 시나리오)
        product_cls = DOMAIN_NS_OBJ["ProductMaster"]
        item_cls = DOMAIN_NS_OBJ["ItemMaster"]
        g.add((target_uri, RDF.type, product_cls))

        # FK 단계에서 ItemMaster 를 추가 주입했다고 가정
        g.add((target_uri, RDF.type, item_cls))
        pre_added_types = [(target_uri, item_cls)]

        disjoint_groups = [{str(product_cls), str(item_cls)}]

        stats = _deterministic_fix_abox(
            g, violations=[],
            disjoint_groups=disjoint_groups,
            pre_added_types=pre_added_types,
        )

        # ItemMaster 타입이 롤백되고 ProductMaster 만 남아야 함
        remaining_types = {str(o) for o in g.objects(target_uri, RDF.type)}
        assert str(item_cls) not in remaining_types
        assert str(product_cls) in remaining_types
        assert stats["disjoint_conflict_reverted"] >= 1


# ── M4: sentinel vs coercion failure 분리 ────────────────────────


class TestSentinelVsCoercion:
    def test_is_null_sentinel_detects_known_forms(self):
        assert _is_null_sentinel("미정")
        assert _is_null_sentinel("N/A")
        assert _is_null_sentinel("null")
        assert _is_null_sentinel("-")
        assert _is_null_sentinel("")
        assert _is_null_sentinel(" ")

    def test_is_null_sentinel_rejects_real_values(self):
        assert not _is_null_sentinel("Primary")
        assert not _is_null_sentinel("42")
        assert not _is_null_sentinel("Active")

    def test_loss_manifest_separates_counts(self):
        warnings = {"unverified_fk": {}, "failed_tables": [], "duplicate_pk_count": 0}
        manifest = _build_loss_manifest(
            warnings, column_coverage={},
            total_rows=100, total_cols=10,
            coercion_failures=5,
            sentinel_filtered=40,
        )
        losses = manifest["losses"]
        assert losses["type_coercion_failures"]["count"] == 5
        assert losses["sentinel_filtered"]["count"] == 40


# ── M9: FK fallback logging ───────────────────────────────────────


class TestFkFallbackLogging:
    def test_separator_fallback_logs_info(self, caplog):
        _FK_FALLBACK_LOGGED.clear()
        with caplog.at_level(logging.INFO, logger="tools.abox_generation"):
            target = _fk_column_to_class("item-code")
        # itemcode 는 fk_patterns.json 에 있으므로 separator 제거 폴백이 발동
        assert target == "ItemMaster"
        assert any("FK fallback" in r.message for r in caplog.records)

    def test_fallback_logged_only_once(self, caplog):
        _FK_FALLBACK_LOGGED.clear()
        with caplog.at_level(logging.INFO, logger="tools.abox_generation"):
            _fk_column_to_class("item-code")
            _fk_column_to_class("item-code")
        log_entries = [r for r in caplog.records if "FK fallback" in r.message]
        # 첫 호출만 로그
        assert len(log_entries) == 1


# ── M5: unmapped 판정이 fk_patterns 와 동기화 ──────────────────────


class TestUnmappedSyncWithFkPatterns:
    def test_known_fk_column_not_unmapped(self):
        """tagid 같은 FK 패턴 등록 컬럼은 unmapped 로 잡히지 않아야 한다.

        _fk_column_to_class 가 `tagid` → `TagMaster` 를 반환하므로 column
        coverage 계산에서 unmapped 에 포함되어서는 안 된다. 하드코딩된
        ("itemcode", "warehousecode") 예외 목록 제거의 근거.
        """
        target = _fk_column_to_class("tagid")
        assert target == "TagMaster"

    def test_unknown_column_returns_none(self):
        assert _fk_column_to_class("unmapped_mystery_col") is None


# ── 공통 ─────────────────────────────────────────────────────────


def _new_graph_stub() -> Graph:
    from domain.tbox_utils import _new_graph
    return _new_graph()
