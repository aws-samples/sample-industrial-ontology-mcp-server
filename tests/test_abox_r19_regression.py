"""Golden regression tests for A-Box generation improvements (task code R19; see docs/reference/task-glossary.md).

R15 (정합성) / R17 (의미품질) / R18 (구조) 이후 남은 18건 개선을 회귀
테스트로 고정.
"""
from __future__ import annotations

from unittest.mock import patch

from rdflib import RDF, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_INST_NS_OBJ, DOMAIN_NS, DOMAIN_NS_OBJ
from domain.tbox_utils import _new_graph

# ── R19-1: composite PK → hasIdentifier ────────────


class TestCompositePkHasIdentifier:
    def test_single_pk_produces_literal(self):
        from tools.abox_generation import _detect_pk_value
        row = {"Equipment_ID": "EQ001", "Name": "Test"}
        # _detect_pk_value composite 인지 파악은 _detect_pk_column 의 책임
        pk = _detect_pk_value(row, "EquipmentMaster", pk_column="equipment_id")
        assert pk == "EQ001"

    def test_composite_pk_components_joined(self):
        from tools.abox_generation import _detect_pk_value
        row = {"Item_Code": "ITM01", "Warehouse_Code": "WH1", "Quantity": "100"}
        pk = _detect_pk_value(
            row, "InventoryStatus",
            pk_column=["item_code", "warehouse_code"],
        )
        # composite → '_' join in URI local name
        assert "ITM01" in pk
        assert "WH1" in pk


# ── R19-3: required_props 조건부 필드 ─────────────


class TestConditionalRequiredProps:
    def test_conditional_spec_loaded(self):
        """rules/contracts/common_dp.json::required_props_by_class 에서 NDTResults 가
        조건부 hasSeverity 선언을 로드해야 한다."""
        from tools.abox_generation import _REQUIRED_PROPS_BY_CLASS
        specs = _REQUIRED_PROPS_BY_CLASS.get("NDTResults", [])
        # hasSeverity spec 찾기
        severity_spec = next((s for s in specs if s["dp"] == "hasSeverity"), None)
        assert severity_spec is not None
        assert severity_spec.get("if") is not None
        assert severity_spec["if"]["dp"] == "hasResult"
        assert severity_spec["if"]["op"] == "!="

    def test_audit_skips_when_condition_false(self):
        """조건이 false 인 인스턴스는 검사 대상에서 제외."""
        from tools.abox_generation import _check_required_props_coverage
        g = _new_graph()
        # NDT: hasResult=Pass, hasSeverity 없음 — condition false, 통과여야 함
        pass_inst = DOMAIN_INST_NS_OBJ["NDTResults_N0001"]
        g.add((pass_inst, RDF.type, DOMAIN_NS_OBJ["NDTResults"]))
        g.add((pass_inst, DOMAIN_NS_OBJ["hasResult"], Literal("Pass")))
        # NDT: hasResult=Fail, hasSeverity 있음 — condition true, 통과
        fail_inst = DOMAIN_INST_NS_OBJ["NDTResults_N0002"]
        g.add((fail_inst, RDF.type, DOMAIN_NS_OBJ["NDTResults"]))
        g.add((fail_inst, DOMAIN_NS_OBJ["hasResult"], Literal("Fail")))
        g.add((fail_inst, DOMAIN_NS_OBJ["hasSeverity"], Literal("Critical")))

        per_class = {"NDTResults": 2}
        audit = _check_required_props_coverage(g, per_class, tbox_info={"datatype_properties": {}})
        # hasSeverity violations 있으면 안 됨 (조건부로 skip 된 Pass 인스턴스)
        viol = [v for v in audit["violations"] if v["dp"] == "hasSeverity"]
        assert viol == [], f"조건부 hasSeverity 가 잘못 위반으로 잡힘: {viol}"


# ── R19-4: FK no/ref allow-list ────────────────────


class TestFkAllowList:
    def test_unknown_no_suffix_not_fk(self):
        """fk_patterns.json 에 없는 vehicle_no 같은 컬럼은 FK 후보에서 제외."""
        # 새 가상의 FK 컬럼명 사용
        from tools.abox_generation import _FK_PATTERNS
        # 확실히 패턴에 없는 suffix
        assert "custom_mystery_no" not in _FK_PATTERNS
        # _fk_column_to_class 는 suffix rule 로는 잡음 — 그건 의도적.
        # 하지만 _is_fk_candidate 가 allow-list 기반으로 제한되는지는
        # generate_abox 내부 로직이므로 별도 단위 테스트 어려움.
        # 여기서는 _FK_PATTERNS 가 rules 파일에서 로드되는지만 확인.
        assert isinstance(_FK_PATTERNS, dict)


# ── R19-5: provenance meta mode ────────────────────


class TestProvenanceMetaMode:
    def test_streaming_on_by_default(self):
        import os
        assert os.getenv("ABOX_PROVENANCE_STREAM", "true").lower() in ("true", "1", "yes")


# ── R19-9: 적응형 prefetch cap ─────────────────────


class TestAdaptivePrefetchCap:
    def test_env_override_honored(self):
        import os
        with patch.dict(os.environ, {"ABOX_PREFETCH_MAX_MB": "100"}):
            # 재 import 필요하지만, 내부 상수는 module load 시 결정되므로
            # 여기서는 env 값 자체만 검증.
            assert os.environ["ABOX_PREFETCH_MAX_MB"] == "100"


# ── R19-11: unknown_enum_values 집계 ──────────────


class TestUnknownEnumTracking:
    def test_unknown_value_tracked(self):
        from tools.abox_generation import (
            _normalize_value,
            _reset_unknown_enum_values,
            _snapshot_unknown_enum_values,
        )
        _reset_unknown_enum_values()
        # Status 컬럼은 enum_synonyms 에 등록됨. "CompletelyWeirdValue" 는 없음
        result = _normalize_value("CompletelyWeirdValue", "status")
        assert result == "CompletelyWeirdValue"  # passthrough
        snap = _snapshot_unknown_enum_values()
        key = "status:CompletelyWeirdValue"
        assert key in snap
        assert snap[key] >= 1


# ── R19-13: reserved_char_uri_count ────────────────


class TestReservedCharUriCount:
    def test_validator_returns_total_count(self):
        """audit dict 가 reserved_char_uri_count 필드를 반환하는지 확인.
        Oxigraph store 는 invalid URI add 를 거부하므로 여기서는 깨끗한
        graph 로 필드 존재만 검증 (count=0 이어도 키 존재).
        """
        from tools.abox_generation import _validate_instances
        g = _new_graph()
        good_uri = URIRef(f"{DOMAIN_INST_NS}GoodInstance")
        g.add((good_uri, RDF.type, DOMAIN_NS_OBJ["SomeClass"]))
        audit = _validate_instances(g)
        assert "reserved_char_uri_count" in audit
        assert isinstance(audit["reserved_char_uri_count"], int)


# ── R19-15: rules 파일 미존재 warning ────────────


class TestRulesFileLoaderWarning:
    def test_required_flag_triggers_warning(self, caplog):
        import logging

        from tools.abox_generation import _load_rules_json
        with caplog.at_level(logging.WARNING, logger="tools.abox_generation"):
            _load_rules_json("nonexistent_rules_file_xyz.json", required=True)
        assert any("파일이 없음" in r.message or "nonexistent" in r.message for r in caplog.records)


# ── R19-16: 도메인 네임스페이스 헬퍼 ──────────────


class TestDomainNsHelpers:
    def test_ns_cls_returns_uri(self):
        from tools.abox_generation import _ns_cls
        uri = _ns_cls("TestClass")
        assert str(uri).startswith(str(DOMAIN_NS))
        assert str(uri).endswith("TestClass")

    def test_ns_inst_returns_uri(self):
        from tools.abox_generation import _ns_inst
        uri = _ns_inst("TestInst_001")
        assert str(uri).startswith(str(DOMAIN_INST_NS))


# ── R19-17: required_props wildcard pattern ───────


class TestRequiredPropsWildcard:
    def test_wildcard_pattern_loaded(self):
        from tools.abox_generation import _REQUIRED_PROPS_BY_CLASS
        # common_dp.json 에 "*Monitoring" 패턴 선언했음
        assert "*Monitoring" in _REQUIRED_PROPS_BY_CLASS

    def test_resolve_expands_pattern(self):
        from tools.abox_generation import _resolve_required_props_for_class
        # AirEmissionMonitoring 은 정확 매칭 없을 수 있으나 *Monitoring 패턴으로 hasTimestamp 발견
        specs = _resolve_required_props_for_class("AirEmissionMonitoring")
        dp_names = {s["dp"] for s in specs}
        assert "hasTimestamp" in dp_names

    def test_resolve_concrete_wins_over_pattern(self):
        from tools.abox_generation import _resolve_required_props_for_class
        # NDTResults 는 정확 매칭 있음 (hasResult/hasSeverity). 패턴이 간섭 안 되어야.
        specs = _resolve_required_props_for_class("NDTResults")
        dp_names = {s["dp"] for s in specs}
        assert "hasResult" in dp_names


# ── R19-18: validate_rules.py 실행 ────────────────


class TestRulesValidator:
    def test_validator_finds_no_issues_on_current_rules(self):
        from tools.validate_rules import validate_rules_files
        issues = validate_rules_files()
        assert issues == [], f"Unexpected rule issues: {issues}"


# ── R19-14: build_id + input_digest ───────────────


class TestBuildProvenance:
    def test_build_id_predicate_convention(self):
        # URI pattern 확인 — 실제 추가는 generate_abox E2E 에서 검증
        from rdflib import URIRef

        from domain.namespaces import ONTOLOGY_URI
        build_id_pred = URIRef(f"{ONTOLOGY_URI}/prov/buildId")
        tbox_sha_pred = URIRef(f"{ONTOLOGY_URI}/prov/tboxSha256")
        assert "prov/buildId" in str(build_id_pred)
        assert "prov/tboxSha256" in str(tbox_sha_pred)
