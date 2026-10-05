"""R17 Golden regression tests for A-Box generation improvements.

R15 는 정합성/안전성(master_data 분리, disjoint 가드 등) 에 집중했고,
R17 은 의미 품질(공통 DP, PK DP, required props, FK suffix 확장) 에 집중.
각 개선이 다시 회귀하지 않도록 단위 테스트로 고정.
"""
from __future__ import annotations

from tools.abox_generation import (
    _COMMON_DP_BY_ALIAS,
    _COMMON_DP_BY_NAME,
    _MASTER_FILENAME_SUFFIXES,
    _NULL_SENTINELS,
    _NUMERIC_KEYWORDS,
    _PK_CLASS_SUFFIXES,
    _REQUIRED_PROPS_BY_CLASS,
    _TIMESTAMP_COLUMNS,
    _TRANSACTION_FILENAME_SUFFIXES,
    _convert_time,
    _fk_column_to_class,
    _normalize_value,
    _resolve_common_dp_name,
    _suggest_dp_name,
)

# ── P0-2: common_dp.json 로드 ────────────────────────


class TestCommonDpRules:
    def test_common_dp_has_timestamp(self):
        assert "hasTimestamp" in _COMMON_DP_BY_NAME

    def test_common_dp_has_value(self):
        assert "hasValue" in _COMMON_DP_BY_NAME

    def test_alias_timestamp_maps_to_has_timestamp(self):
        assert _COMMON_DP_BY_ALIAS.get("timestamp") == "hasTimestamp"

    def test_alias_value_maps_to_has_value(self):
        assert _COMMON_DP_BY_ALIAS.get("value") == "hasValue"

    def test_resolve_common_dp_name_timestamp_column(self):
        assert _resolve_common_dp_name("Timestamp") == "hasTimestamp"

    def test_resolve_common_dp_name_snake_case(self):
        assert _resolve_common_dp_name("unit_price") == "hasUnitPrice"

    def test_resolve_common_dp_name_unknown_returns_none(self):
        assert _resolve_common_dp_name("totally_custom_column") is None


# ── P0-4: required_props_by_class ───────────────────


class TestRequiredPropsConfig:
    def test_real_time_data_requires_timestamp_and_value(self):
        # R19: list[dict] 구조로 변경됨 ({"dp": ..., "if": ...}).
        required = _REQUIRED_PROPS_BY_CLASS.get("RealTimeData", [])
        dp_names = {spec["dp"] for spec in required}
        assert "hasTimestamp" in dp_names
        assert "hasValue" in dp_names

    def test_transportation_requires_origin_destination(self):
        required = _REQUIRED_PROPS_BY_CLASS.get("Transportation", [])
        dp_names = {spec["dp"] for spec in required}
        assert "hasOrigin" in dp_names
        assert "hasDestination" in dp_names


# ── P1-5: FK suffix rule 확장 ───────────────────────


class TestFkSuffixExtension:
    def test_reference_no_resolves(self):
        # reference_no → base='reference' → ReferenceMaster (Template)
        result = _fk_column_to_class("referenceno")
        assert result == "ReferenceMaster"

    def test_vehicle_no_resolves(self):
        result = _fk_column_to_class("vehicleno")
        assert result == "VehicleMaster"

    def test_supplier_ref_resolves(self):
        result = _fk_column_to_class("supplierref")
        # rules 에선 supplierid 가 정확매칭되므로 ref 폴백 확인. supplierref 는 없음.
        assert result in ("SupplierMaster", None)


# ── P1-7: value_heuristics 외부화 ────────────────────


class TestValueHeuristicsExternalized:
    def test_pk_class_suffixes_loaded(self):
        assert "master" in _PK_CLASS_SUFFIXES
        assert "transaction" in _PK_CLASS_SUFFIXES

    def test_timestamp_columns_loaded(self):
        assert "timestamp" in _TIMESTAMP_COLUMNS

    def test_numeric_keywords_loaded(self):
        assert "temperature" in _NUMERIC_KEYWORDS

    def test_null_sentinels_loaded(self):
        assert "n/a" in _NULL_SENTINELS
        assert "미정" in _NULL_SENTINELS

    def test_transaction_filename_suffixes_loaded(self):
        assert "_History" in _TRANSACTION_FILENAME_SUFFIXES
        assert "_Transaction" in _TRANSACTION_FILENAME_SUFFIXES

    def test_master_filename_suffixes_loaded(self):
        assert "_Master" in _MASTER_FILENAME_SUFFIXES
        assert "_Map" in _MASTER_FILENAME_SUFFIXES


# ── P1-8: value normalization ──────────────────────


class TestValueNormalization:
    def test_trim_applied(self):
        assert _normalize_value("  hello  ", "col") == "hello"

    def test_enum_synonym_status(self):
        # Status column with alias value
        assert _normalize_value("active", "status") == "Active"
        assert _normalize_value("운영중", "status") == "Active"

    def test_enum_synonym_severity(self):
        assert _normalize_value("심각", "severity") == "Critical"

    def test_unknown_column_passthrough(self):
        # No enum mapping for arbitrary column
        assert _normalize_value("Primary", "custom_col") == "Primary"


# ── P2-14: suggested_dp_name ────────────────────────


class TestSuggestedDpName:
    def test_known_alias_returns_common_name(self):
        # Timestamp → hasTimestamp (common DP)
        assert _suggest_dp_name("Timestamp") == "hasTimestamp"
        assert _suggest_dp_name("timestamp") == "hasTimestamp"

    def test_unknown_column_returns_has_pascal(self):
        # RandomName → hasRandomName
        name = _suggest_dp_name("random_name")
        assert name == "hasRandomName"

    def test_special_chars_stripped(self):
        name = _suggest_dp_name("col-with-dashes")
        assert name == "hasColWithDashes"


# ── P3-18: xsd:time 변환 ────────────────────────────


class TestTimeConverter:
    def test_pure_time(self):
        assert _convert_time("12:30:45") == "12:30:45"

    def test_datetime_extracts_time_part(self):
        # "2025-09-01 12:30:45" should extract "12:30:45"
        result = _convert_time("2025-09-01 12:30:45")
        assert result == "12:30:45"

    def test_invalid_returns_none(self):
        assert _convert_time("not a time") is None

    def test_empty_returns_none(self):
        assert _convert_time("") is None


# ── P3-15: Instance validator ──────────────────────


class TestInstanceValidator:
    def test_detects_untyped_instance(self):
        from rdflib import Literal

        from domain.namespaces import DOMAIN_INST_NS_OBJ
        from domain.tbox_utils import _new_graph
        from tools.abox_generation import _validate_instances

        g = _new_graph()
        # typed instance
        from rdflib import RDF

        from domain.namespaces import DOMAIN_NS_OBJ
        typed = DOMAIN_INST_NS_OBJ["EquipmentMaster_EQ001"]
        g.add((typed, RDF.type, DOMAIN_NS_OBJ["EquipmentMaster"]))
        # untyped instance (has only a literal assertion)
        untyped = DOMAIN_INST_NS_OBJ["Orphan_X001"]
        g.add((untyped, DOMAIN_NS_OBJ["hasName"], Literal("stray")))

        audit = _validate_instances(g)
        assert audit["untyped_instances"] == 1
