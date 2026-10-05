"""tests for tools/rules_init.py — Phase 1 프로그래매틱 생성 검증."""



from tools.rules_init import (
    _gen_domain_config,
    _gen_fk_patterns,
    _gen_table_class_mapping,
    _gen_tbox_generation_config,
    _gen_value_ranges,
    _normalize_col,
    _parse_llm_response,
    _scan_csv_tables,
    _to_pascal,
)

# ── 유틸 함수 ────────────────────────────────────────────────────────────


class TestToPascal:
    def test_basic(self):
        assert _to_pascal("Process_Blast_Furnace") == "ProcessBlastFurnace"

    def test_single_word(self):
        assert _to_pascal("Transportation") == "Transportation"

    def test_two_words(self):
        assert _to_pascal("Item_Master") == "ItemMaster"


class TestNormalizeCol:
    def test_basic(self):
        assert _normalize_col("Equipment_ID") == "equipmentid"

    def test_no_underscore(self):
        assert _normalize_col("TagID") == "tagid"


# ── CSV 스캔 ─────────────────────────────────────────────────────────────


class TestScanCsvTables:
    def test_returns_dict(self):
        tables = _scan_csv_tables()
        assert isinstance(tables, dict)
        assert len(tables) > 0

    def test_columns_are_lists(self):
        tables = _scan_csv_tables()
        for cols in tables.values():
            assert isinstance(cols, list)
            assert len(cols) > 0


# ── domain_config.json ───────────────────────────────────────────────────


class TestGenDomainConfig:
    def test_structure(self):
        cfg = _gen_domain_config(
            "Test Domain", "테스트 도메인",
            "http://test.com/ontology", "test",
            "testing", "테스트",
        )
        assert cfg["domain"]["name"] == "Test Domain"
        assert cfg["namespace"]["prefix"] == "test"
        assert cfg["namespace"]["class_ns"] == "http://test.com/ontology#"
        assert cfg["namespace"]["instance_ns"] == "http://test.com/ontology/instances#"

    def test_default_license_matches_repository_license(self):
        cfg = _gen_domain_config(
            "Test Domain", "테스트 도메인",
            "http://test.com/ontology", "test",
            "testing", "테스트",
        )
        assert cfg["metadata"]["license"] == "https://spdx.org/licenses/MIT-0.html"

    def test_trailing_hash_stripped(self):
        cfg = _gen_domain_config(
            "X", "X", "http://x.com/ont#", "x", "x", "x",
        )
        assert cfg["namespace"]["class_ns"] == "http://x.com/ont#"
        assert not cfg["namespace"]["ontology_uri"].endswith("#")


# ── tbox_generation_config.json ──────────────────────────────────────────


class TestGenTboxGenerationConfig:
    def test_has_required_keys(self):
        cfg = _gen_tbox_generation_config()
        assert "max_tokens" in cfg
        assert "max_retries" in cfg
        assert cfg["max_tokens"] == 24000


# ── table_class_mapping.json ─────────────────────────────────────────────


class TestGenTableClassMapping:
    def test_mapping(self):
        tables = {"Process_Blast_Furnace": ["col1"], "Item_Master": ["col1"]}
        result = _gen_table_class_mapping(tables, "steel")
        m = result["table_class_mapping"]
        assert m["Process_Blast_Furnace"] == "steel:ProcessBlastFurnace"
        assert m["Item_Master"] == "steel:ItemMaster"

    def test_empty_class_prop_mapping(self):
        result = _gen_table_class_mapping({"A": ["c"]}, "x")
        assert result["class_prop_mapping"] == {}


# ── fk_patterns.json ─────────────────────────────────────────────────────


class TestGenFkPatterns:
    def test_basic_fk_detection(self):
        tables = {
            "Equipment_Master": ["Equipment_ID", "Name"],
            "Maintenance_History": ["Maintenance_ID", "Equipment_ID", "Date"],
        }
        fk = _gen_fk_patterns(tables)
        assert "equipmentid" in fk["patterns"]
        assert fk["patterns"]["equipmentid"] == "EquipmentMaster"

    def test_pk_skipped(self):
        """첫 번째 컬럼(PK)은 FK로 잡히지 않아야 함."""
        tables = {
            "Equipment_Master": ["Equipment_ID", "Name"],
        }
        fk = _gen_fk_patterns(tables)
        # Equipment_Master의 PK인 Equipment_ID만 있으므로 FK 없음
        assert "equipmentid" not in fk["patterns"]

    def test_short_base_not_matched(self):
        """짧은 base(5자 미만)는 폴백 매칭에서 제외."""
        tables = {
            "Real_Time_Data": ["Tag_ID", "Timestamp", "Value", "Quality_Code"],
            "Surface_Quality": ["Sample_ID", "Product_ID"],
        }
        fk = _gen_fk_patterns(tables)
        assert "qualitycode" not in fk["patterns"]

    def test_suffix_rules_present(self):
        fk = _gen_fk_patterns({"A": ["A_ID"]})
        assert len(fk["suffix_rules"]) > 0

    def test_real_csv_fk_count(self):
        """실제 CSV에서 주요 FK가 모두 탐지되는지."""
        tables = _scan_csv_tables()
        fk = _gen_fk_patterns(tables)
        expected = {"equipmentid", "productid", "supplierid", "itemcode"}
        assert expected.issubset(set(fk["patterns"].keys()))


# ── value_ranges.json ────────────────────────────────────────────────────


class TestGenValueRanges:
    def test_temperature_detected(self):
        tables = {"Process": ["Timestamp", "Hot_Air_Temp_C", "Pressure_kPa"]}
        vr = _gen_value_ranges(tables)
        assert "temperatureC" in vr["ranges"]
        assert "pressureKpa" in vr["ranges"]

    def test_percent_detected(self):
        tables = {"Data": ["ID", "Yield_Rate_Percent"]}
        vr = _gen_value_ranges(tables)
        assert "percentValue" in vr["ranges"]

    def test_fallback_generic(self):
        tables = {"Data": ["ID", "Name"]}
        vr = _gen_value_ranges(tables)
        assert "genericNumeric" in vr["ranges"]

    def test_real_csv_ranges(self):
        tables = _scan_csv_tables()
        vr = _gen_value_ranges(tables)
        assert len(vr["ranges"]) >= 5


# ── LLM 응답 파싱 ────────────────────────────────────────────────────────


class TestParseLlmResponse:
    def test_parse_all_tags(self):
        text = """
<table_labels>
{"A": "에이"}
</table_labels>

<disjoint_groups>
{"groups": []}
</disjoint_groups>

<design_patterns>
{"description": "test"}
</design_patterns>
"""
        result = _parse_llm_response(text)
        assert "table_labels" in result
        assert "disjoint_groups" in result
        assert "design_patterns" in result
        assert result["table_labels"]["A"] == "에이"

    def test_parse_with_code_fence(self):
        text = """
<table_labels>
```json
{"A": "에이"}
```
</table_labels>
"""
        result = _parse_llm_response(text)
        assert result["table_labels"]["A"] == "에이"

    def test_parse_missing_tag(self):
        text = "<table_labels>{}</table_labels>"
        result = _parse_llm_response(text)
        assert "table_labels" in result
        assert "disjoint_groups" not in result

    def test_parse_invalid_json(self):
        text = "<table_labels>not json</table_labels>"
        result = _parse_llm_response(text)
        assert "table_labels" not in result
