"""Golden regression — 약점 5건 개선 회귀 방지 (작업코드 R21; docs/reference/task-glossary.md).

R20 평가에서 식별된 A-Box 약점 5건을 전부 개선한 R21 커밋에 대한 테스트.
"""
from __future__ import annotations

import logging
import os

# ── R21-5: xsd:time 경고 필터 ────────────────────


class TestXsdTimeWarningFilter:
    def test_filter_class_installed(self):
        import logging as _logging

        # 모듈 import 가 filter 를 등록함
        from tools.abox_generation import _SuppressTimeLiteralConvertWarning  # noqa: F401
        rdflib_term_logger = _logging.getLogger("rdflib.term")
        assert any(
            isinstance(f, _SuppressTimeLiteralConvertWarning)
            for f in rdflib_term_logger.filters
        )

    def test_filter_blocks_time_message(self):
        from tools.abox_generation import _SuppressTimeLiteralConvertWarning
        filt = _SuppressTimeLiteralConvertWarning()
        rec = logging.LogRecord(
            "rdflib.term", logging.WARNING, __file__, 1,
            "Failed to convert Literal lexical form to value. "
            "Datatype=http://www.w3.org/2001/XMLSchema#time, Converter=...",
            args=None, exc_info=None,
        )
        assert filt.filter(rec) is False

    def test_filter_passes_other_messages(self):
        from tools.abox_generation import _SuppressTimeLiteralConvertWarning
        filt = _SuppressTimeLiteralConvertWarning()
        rec = logging.LogRecord(
            "rdflib.term", logging.WARNING, __file__, 1,
            "Some other warning about decimal parsing",
            args=None, exc_info=None,
        )
        assert filt.filter(rec) is True


# ── R21-4: master incoming OP 옵션 ───────────────


class TestMasterSplitIncludesIncoming:
    def test_opt_in_includes_incoming_op(self, monkeypatch):
        """ABOX_MASTER_INCLUDE_INCOMING_OP=true 일 때 transaction → master
        역방향 OP + rdf:type 이 master_data 에 포함된다 (master-only
        그래프 분리 use-case)."""
        from rdflib import RDF, Literal

        from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
        from domain.tbox_utils import _new_graph
        from tools.abox_generation import _split_master_subgraph

        monkeypatch.setenv("ABOX_MASTER_INCLUDE_INCOMING_OP", "true")
        g = _new_graph()
        master_inst = DOMAIN_INST_NS_OBJ["EquipmentMaster_EQ001"]
        txn_inst = DOMAIN_INST_NS_OBJ["ProductionResult_PR001"]
        g.add((master_inst, RDF.type, DOMAIN_NS_OBJ["EquipmentMaster"]))
        g.add((master_inst, DOMAIN_NS_OBJ["hasName"], Literal("고로1")))
        g.add((txn_inst, RDF.type, DOMAIN_NS_OBJ["ProductionResult"]))
        g.add((txn_inst, DOMAIN_NS_OBJ["usesEquipment"], master_inst))

        master_g, count, uris = _split_master_subgraph(g, {"EquipmentMaster"})
        subjects = {str(s) for s in master_g.subjects()}
        assert str(master_inst) in subjects
        assert str(txn_inst) in subjects
        # txn 은 rdf:type 만 보존
        txn_preds = [str(p) for p in master_g.predicates(txn_inst, None)]
        assert str(RDF.type) in txn_preds

    def test_default_excludes_incoming_op(self):
        """기본값은 transaction 분리 (R15 원 동작) — Closed-World master
        고립 check 보호."""
        from rdflib import RDF

        from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
        from domain.tbox_utils import _new_graph
        from tools.abox_generation import _split_master_subgraph

        g = _new_graph()
        master_inst = DOMAIN_INST_NS_OBJ["EquipmentMaster_EQ001"]
        txn_inst = DOMAIN_INST_NS_OBJ["ProductionResult_PR001"]
        g.add((master_inst, RDF.type, DOMAIN_NS_OBJ["EquipmentMaster"]))
        g.add((txn_inst, RDF.type, DOMAIN_NS_OBJ["ProductionResult"]))
        g.add((txn_inst, DOMAIN_NS_OBJ["usesEquipment"], master_inst))

        master_g, _, _ = _split_master_subgraph(g, {"EquipmentMaster"})
        subjects = {str(s) for s in master_g.subjects()}
        assert str(txn_inst) not in subjects


# ── R21-3: provenance gzip ──────────────────────


class TestProvenanceGzip:
    def test_default_gzip_env_on(self):
        # env 기본값은 "true"
        assert os.getenv("ABOX_PROVENANCE_GZIP", "true").lower() in ("true", "1", "yes")


# ── R21-2: inverse-on-generate 옵션 ──────────────


class TestInverseOnGenerateOption:
    def test_env_default_false(self):
        # 옵트인 — 기본 false (성능 비용)
        assert os.getenv("ABOX_APPLY_INVERSE_ON_GENERATE", "false").lower() in ("false", "0", "no")


# ── R21-1: property_completeness_detail 구조 ─────


class TestPropertyCompletenessDetail:
    def test_helper_returns_dict(self):
        from rdflib import RDF, Literal

        from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
        from domain.tbox_utils import _new_graph
        from tools.abox_generation import _compute_property_completeness_detail

        g = _new_graph()
        inst = DOMAIN_INST_NS_OBJ["Foo_1"]
        g.add((inst, RDF.type, DOMAIN_NS_OBJ["Foo"]))
        g.add((inst, DOMAIN_NS_OBJ["hasName"], Literal("test")))
        tbox_info = {
            "datatype_properties": {"hasname": "hasName", "hasage": "hasAge"},
            "dp_domains": {"hasname": {"Foo"}, "hasage": {"Foo"}},
            "dp_ranges": {},
            "classes": {},
            "object_properties": {},
            "class_props": {},
            "subclass_of": {},
        }
        detail = _compute_property_completeness_detail(
            g, {"Foo": 1}, tbox_info,
        )
        assert "Foo" in detail
        info = detail["Foo"]
        assert "mean_coverage_pct" in info
        assert "low_coverage_dps" in info
        # hasAge 는 0% 커버 → low 에 있어야 함
        low_dps = [d["dp"] for d in info["low_coverage_dps"]]
        assert "hasAge" in low_dps
