"""tests for tools/abox_generation.py — FK 매칭, PK 감지, 클래스 존재 확인, Loss Manifest."""


from tools.abox_generation import (
    _build_loss_manifest,
    _class_exists_in_tbox,
    _detect_pk_value,
    _fk_column_to_class,
    _format_value,
    _is_numeric,
    _load_fk_patterns,
    _normalize_boolean,
    _table_to_class,
)


class TestLoadFkPatterns:
    """``_load_fk_patterns`` 는 3-tuple 을 반환한다.

    2026-07-04 (커밋 7c03526) 에 ``fk_value_transforms`` 가 3번째 반환값으로 추가됐는데
    이 테스트가 갱신되지 않아 ``ValueError: too many values to unpack`` 으로 6주간
    red 였다. 언패킹 개수를 계약으로 고정한다.
    """

    def test_returns_triple(self):
        patterns, suffix_rules, transforms = _load_fk_patterns()
        assert isinstance(patterns, dict)
        assert isinstance(suffix_rules, list)
        assert isinstance(transforms, dict), (
            "fk_value_transforms 가 3번째 반환값이어야 한다"
        )

    def test_known_patterns(self):
        patterns, _, _ = _load_fk_patterns()
        assert "equipmentid" in patterns
        assert patterns["equipmentid"] == "EquipmentMaster"

    def test_transforms_exclude_meta_keys(self):
        """``_comment`` 같은 메타 키는 변환 규칙으로 채택되지 않는다."""
        _p, _s, transforms = _load_fk_patterns()
        assert not [k for k in transforms if k.startswith("_")]
        assert all(isinstance(v, dict) for v in transforms.values())


class TestFkColumnToClass:
    def test_known_column(self):
        result = _fk_column_to_class("equipmentid")
        assert result == "EquipmentMaster"

    def test_unknown_column(self):
        result = _fk_column_to_class("randomxyz")
        # suffix rule로 폴백하거나 None
        assert result is None or isinstance(result, str)


class TestClassExistsInTbox:
    def test_known_class(self):
        assert _class_exists_in_tbox("EquipmentMaster") is True

    def test_unknown_class(self):
        assert _class_exists_in_tbox("NonexistentClass123") is False


class TestDetectPkValue:
    def test_id_column(self):
        row = {"Equipment_ID": "EQ001", "Name": "Test"}
        result = _detect_pk_value(row, "EquipmentMaster")
        assert result is not None

    def test_no_pk(self):
        row = {"Name": "Test", "Value": "123"}
        result = _detect_pk_value(row, "SomeClass")
        # PK 못 찾으면 None 또는 첫 번째 값
        assert result is None or isinstance(result, str)


class TestTableToClass:
    def test_basic(self):
        result = _table_to_class("Equipment_Master", {"classes": {}})
        assert result == "EquipmentMaster"

    def test_with_mapping(self):
        tbox_info = {"classes": {"equipmentmaster": "EquipmentMasterCustom"}}
        result = _table_to_class("Equipment_Master", tbox_info)
        assert result == "EquipmentMasterCustom"


class TestIsNumeric:
    def test_integer(self):
        assert _is_numeric("42") is True

    def test_float(self):
        assert _is_numeric("3.14") is True

    def test_string(self):
        assert _is_numeric("hello") is False

    def test_empty(self):
        assert _is_numeric("") is False


class TestNormalizeBoolean:
    def test_true(self):
        assert _normalize_boolean("true") == "true"
        assert _normalize_boolean("True") == "true"

    def test_false(self):
        assert _normalize_boolean("false") == "false"

    def test_non_boolean(self):
        assert _normalize_boolean("hello") is None


class TestFormatValueSentinels:
    """NULL 센티널 값 필터 테스트."""

    def test_na_filtered(self):
        assert _format_value("N/A", "status") is None

    def test_null_filtered(self):
        assert _format_value("null", "value") is None

    def test_dash_filtered(self):
        assert _format_value("-", "temperature") is None

    def test_none_string_filtered(self):
        assert _format_value("none", "pressure") is None

    def test_nan_filtered(self):
        assert _format_value("NaN", "rate") is None

    def test_korean_sentinel_filtered(self):
        assert _format_value("미정", "status") is None
        assert _format_value("해당없음", "category") is None

    def test_valid_value_preserved(self):
        result = _format_value("Active", "status")
        assert result is not None
        assert str(result) == "Active"

    def test_zero_not_filtered(self):
        """0은 유효한 값이므로 필터하지 않음."""
        result = _format_value("0", "temperature")
        assert result is not None


class TestFormatValueNumericKeyword:
    """_NUMERIC_KEYWORDS에서 'data' 제거 확인."""

    def test_data_suffix_not_numeric(self):
        """statusData 같은 컬럼이 숫자로 오인되지 않음."""
        result = _format_value("Active", "statusData")
        assert result is not None
        assert "decimal" not in str(result.datatype)

    def test_actual_numeric_still_works(self):
        """temperature 등 실제 수치 키워드는 정상 동작."""
        result = _format_value("123.5", "temperature")
        assert result is not None
        assert "decimal" in str(result.datatype)


class TestDetectPkDuplicate:
    """PK 중복 감지 테스트."""

    def test_same_pk_different_rows(self):
        """같은 클래스에서 동일 PK → 같은 URI 반환."""
        row1 = {"EquipmentID": "EQ001", "Name": "A"}
        row2 = {"EquipmentID": "EQ001", "Name": "B"}
        pk1 = _detect_pk_value(row1, "EquipmentMaster")
        pk2 = _detect_pk_value(row2, "EquipmentMaster")
        assert pk1 == pk2  # 같은 URI 생성 → 중복

    def test_different_pk_values(self):
        """다른 PK → 다른 URI."""
        row1 = {"EquipmentID": "EQ001", "Name": "A"}
        row2 = {"EquipmentID": "EQ002", "Name": "B"}
        pk1 = _detect_pk_value(row1, "EquipmentMaster")
        pk2 = _detect_pk_value(row2, "EquipmentMaster")
        assert pk1 != pk2

    def test_unicode_pk(self):
        """유니코드 PK 값이 안전하게 처리됨."""
        row = {"EquipmentID": "설비_001", "Name": "한글설비"}
        pk = _detect_pk_value(row, "EquipmentMaster")
        assert pk is not None
        # URI-safe 문자만 포함
        assert all(c.isalnum() or c in "_-" for c in pk)


# ── Loss Manifest 테스트 ─────────────────────────────


class TestLossManifest:
    def test_loss_manifest_contains_fk_failures(self):
        warnings = {
            "unverified_fk": {"EQUIPMENT_ID": {"target": "EquipmentMaster", "count": 3}},
            "failed_tables": [],
            "duplicate_pk_count": 0,
        }
        column_coverage = {"EquipmentMaster": {"mapped": 8, "unmapped": 2, "rate": 0.8}}
        manifest = _build_loss_manifest(warnings, column_coverage, total_rows=100, total_cols=50)
        assert "fk_referential_failures" in manifest["losses"]
        assert manifest["losses"]["fk_referential_failures"]["count"] == 3

    def test_loss_manifest_fidelity_score(self):
        warnings = {"unverified_fk": {}, "failed_tables": [], "duplicate_pk_count": 0}
        column_coverage = {"Cls": {"mapped": 10, "unmapped": 0, "rate": 1.0}}
        manifest = _build_loss_manifest(warnings, column_coverage, total_rows=100, total_cols=10)
        assert "fidelity_score" in manifest
        assert manifest["fidelity_score"]["column_coverage"] == 1.0

    def test_loss_manifest_unmapped_columns(self):
        warnings = {"unverified_fk": {}, "failed_tables": [], "duplicate_pk_count": 0}
        column_coverage = {"Cls": {"mapped": 8, "unmapped": 2, "rate": 0.8,
                                   "unmapped_columns": ["REMARKS", "NOTE"]}}
        manifest = _build_loss_manifest(warnings, column_coverage, total_rows=100, total_cols=10)
        assert manifest["losses"]["unmapped_columns"]["count"] == 2


# ── Cell-Level Provenance 테스트 ────────────────────


class TestCellLevelProvenance:
    def test_provenance_records_source_table(self):
        from tools.abox_generation import _collect_instance_provenance
        prov = _collect_instance_provenance(
            uri="steel-inst:EQ_001", table="EquipmentMaster",
            row_idx=42, pk_col="EQUIPMENT_ID", pk_val="EQ-001",
            properties={"hasName": {"source_column": "NAME", "raw_value": "고로1",
                                    "coercion": "xsd:string", "coercion_loss": False}},
            relationships={}
        )
        assert prov["source_table"] == "EquipmentMaster"
        assert prov["source_row"] == 42
        assert prov["pk_value"] == "EQ-001"
        assert "hasName" in prov["properties"]

    def test_provenance_records_fk_target_exists(self):
        from tools.abox_generation import _collect_instance_provenance
        prov = _collect_instance_provenance(
            uri="steel-inst:PR_001", table="ProductionResult",
            row_idx=10, pk_col="PROD_ID", pk_val="PR-001",
            properties={},
            relationships={"belongsToProcess": {
                "source_column": "PROCESS_ID", "fk_value": "PS-001",
                "target_exists": True, "resolution_method": "exact_fk_pattern"
            }}
        )
        assert prov["relationships"]["belongsToProcess"]["target_exists"] is True
