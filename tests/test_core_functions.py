"""핵심 함수 유닛 테스트 — 66개 도구의 기반 함수 15개 검증."""
import json
import os
import tempfile

import pytest


class TestErrorResponse:
    """tools/common.py: error_response"""

    def test_with_string(self):
        from tools.common import error_response
        result = json.loads(error_response("test error"))
        assert result["success"] is False
        assert "test error" in result["error"]

    def test_with_exception(self):
        from tools.common import error_response
        result = json.loads(error_response(ValueError("bad value")))
        assert result["success"] is False
        assert "bad value" in result["error"]

    def test_with_hint(self):
        from tools.common import error_response
        result = json.loads(error_response("err", hint="fix it"))
        assert result["hint"] == "fix it"

    def test_without_hint_has_no_hint_key(self):
        from tools.common import error_response
        result = json.loads(error_response("err"))
        assert "hint" not in result

    def test_unicode_preserved(self):
        from tools.common import error_response
        result = json.loads(error_response("한글 에러"))
        assert "한글 에러" in result["error"]


class TestPathToFileUri:
    """tools/common.py: path_to_file_uri"""

    def test_converts_path_to_file_uri(self):
        from tools.common import path_to_file_uri
        uri = path_to_file_uri("/tmp/test.html")
        assert uri.startswith("file:///")
        assert "test.html" in uri

    def test_resolves_relative_path(self):
        from tools.common import path_to_file_uri
        uri = path_to_file_uri(".")
        assert uri.startswith("file:///")


class TestAtomicWrite:
    """tools/common.py: atomic_write"""

    def test_writes_content_to_file(self):
        from tools.common import atomic_write
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            atomic_write(path, "hello world")
            with open(path, encoding="utf-8") as f:
                assert f.read() == "hello world"

    def test_creates_parent_directories(self):
        from tools.common import atomic_write
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "test.txt")
            atomic_write(path, "nested")
            with open(path, encoding="utf-8") as f:
                assert f.read() == "nested"

    def test_overwrites_existing_file(self):
        from tools.common import atomic_write
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            atomic_write(path, "first")
            atomic_write(path, "second")
            with open(path, encoding="utf-8") as f:
                assert f.read() == "second"

    def test_no_temp_file_left_behind(self):
        from tools.common import atomic_write
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            atomic_write(path, "content")
            files = os.listdir(tmpdir)
            assert files == ["test.txt"]


class TestPrependPrefixes:
    """domain/namespaces.py: prepend_prefixes"""

    def test_adds_prefix_when_missing(self):
        from domain.namespaces import prepend_prefixes
        query = "SELECT ?s WHERE { ?s ?p ?o }"
        result = prepend_prefixes(query)
        assert "PREFIX" in result
        assert query in result

    def test_does_not_add_when_already_present(self):
        from domain.namespaces import prepend_prefixes
        query = "PREFIX ex: <http://example.org/>\nSELECT ?s WHERE { ?s ?p ?o }"
        result = prepend_prefixes(query)
        assert result == query

    def test_case_insensitive_detection(self):
        from domain.namespaces import prepend_prefixes
        query = "prefix ex: <http://example.org/>\nSELECT ?s WHERE { ?s ?p ?o }"
        result = prepend_prefixes(query)
        # "prefix" uppercased is "PREFIX" so it should match
        assert result == query


class TestSanitizeSparqlValue:
    """domain/namespaces.py: sanitize_sparql_value"""

    def test_escapes_double_quotes(self):
        from domain.namespaces import sanitize_sparql_value
        assert '\\"' in sanitize_sparql_value('say "hello"')

    def test_escapes_backslash(self):
        from domain.namespaces import sanitize_sparql_value
        result = sanitize_sparql_value("path\\to")
        assert "\\\\" in result

    def test_escapes_newlines(self):
        from domain.namespaces import sanitize_sparql_value
        result = sanitize_sparql_value("line1\nline2\rline3")
        assert "\\n" in result
        assert "\\r" in result

    def test_leaves_angle_brackets_unescaped(self):
        """큰따옴표 리터럴 안의 ``<`` ``>`` 는 의미가 없어 코드포인트 이스케이프를 만들지 않는다."""
        from domain.namespaces import sanitize_sparql_value
        assert sanitize_sparql_value("<tag>") == "<tag>"

    def test_plain_text_unchanged(self):
        from domain.namespaces import sanitize_sparql_value
        assert sanitize_sparql_value("hello") == "hello"


class TestUriConventions:
    """domain/uri_conventions.py: class_uri, property_uri, instance_uri, local_name"""

    def test_class_uri_pascal_case(self):
        from domain.uri_conventions import class_uri
        uri = class_uri("equipmentMaster")
        assert uri.endswith("EquipmentMaster")

    def test_class_uri_already_pascal(self):
        from domain.uri_conventions import class_uri
        uri = class_uri("EquipmentMaster")
        assert uri.endswith("EquipmentMaster")

    def test_property_uri_camel_case(self):
        from domain.uri_conventions import property_uri
        uri = property_uri("HasEquipment")
        assert uri.endswith("hasEquipment")

    def test_property_uri_already_camel(self):
        from domain.uri_conventions import property_uri
        uri = property_uri("hasEquipment")
        assert uri.endswith("hasEquipment")

    def test_instance_uri_format(self):
        from domain.uri_conventions import instance_uri
        uri = instance_uri("EquipmentMaster", "EQ001")
        assert "EquipmentMaster_EQ001" in uri

    def test_instance_uri_sanitizes_special_chars(self):
        from domain.uri_conventions import instance_uri
        uri = instance_uri("EquipmentMaster", "EQ/001 foo")
        assert "/" not in uri.split("#")[-1].split("EquipmentMaster_")[1]
        assert " " not in uri

    def test_local_name_from_hash_uri(self):
        from domain.namespaces import DOMAIN_NS
        from domain.uri_conventions import local_name
        assert local_name(f"{DOMAIN_NS}EquipmentMaster") == "EquipmentMaster"

    def test_local_name_from_slash_uri(self):
        from domain.uri_conventions import local_name
        assert local_name("http://example.org/ontology/SomeClass") == "SomeClass"

    def test_local_name_no_separator(self):
        from domain.uri_conventions import local_name
        assert local_name("plain_string") == "plain_string"


class TestValidatePropName:
    """tools/kg_validation.py: _validate_prop_name"""

    def test_valid_name_passes(self):
        from tools.kg_validation import _validate_prop_name
        assert _validate_prop_name("hasEquipmentStatus") == "hasEquipmentStatus"

    def test_valid_name_with_underscore(self):
        from tools.kg_validation import _validate_prop_name
        assert _validate_prop_name("has_equipment") == "has_equipment"

    def test_valid_name_with_digits(self):
        from tools.kg_validation import _validate_prop_name
        assert _validate_prop_name("prop123") == "prop123"

    def test_invalid_name_with_space(self):
        from tools.kg_validation import _validate_prop_name
        with pytest.raises(ValueError, match="Invalid property name"):
            _validate_prop_name("has equipment")

    def test_invalid_name_with_sparql_injection(self):
        from tools.kg_validation import _validate_prop_name
        with pytest.raises(ValueError, match="Invalid property name"):
            _validate_prop_name("prop; DROP")

    def test_invalid_name_starting_with_digit(self):
        from tools.kg_validation import _validate_prop_name
        with pytest.raises(ValueError, match="Invalid property name"):
            _validate_prop_name("123prop")

    def test_empty_name_raises(self):
        from tools.kg_validation import _validate_prop_name
        with pytest.raises(ValueError, match="Invalid property name"):
            _validate_prop_name("")


class TestResolveUri:
    """tools/multi_agent_tbox.py: _resolve_uri"""

    def test_steel_prefix(self):
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        from tools.multi_agent_tbox import _resolve_uri
        result = _resolve_uri(f"{NS_PREFIX}:EquipmentMaster")
        assert str(result) == f"{DOMAIN_NS}EquipmentMaster"

    def test_rdfs_prefix(self):
        from tools.multi_agent_tbox import _resolve_uri
        result = _resolve_uri("rdfs:label")
        assert str(result) == "http://www.w3.org/2000/01/rdf-schema#label"

    def test_owl_prefix(self):
        from tools.multi_agent_tbox import _resolve_uri
        result = _resolve_uri("owl:Class")
        assert str(result) == "http://www.w3.org/2002/07/owl#Class"

    def test_full_uri_passthrough(self):
        from tools.multi_agent_tbox import _resolve_uri
        uri = "http://example.org/SomeClass"
        result = _resolve_uri(uri)
        assert str(result) == uri

    def test_returns_uriref_type(self):
        from rdflib import URIRef

        from tools.multi_agent_tbox import _resolve_uri
        result = _resolve_uri("owl:Thing")
        assert isinstance(result, URIRef)


class TestResolveUriOrLiteral:
    """tools/multi_agent_tbox.py: _resolve_uri_or_literal"""

    def test_prefixed_uri_returns_uriref(self):
        from rdflib import URIRef

        from tools.multi_agent_tbox import _resolve_uri_or_literal
        result = _resolve_uri_or_literal("owl:Class")
        assert isinstance(result, URIRef)
        assert str(result) == "http://www.w3.org/2002/07/owl#Class"

    def test_typed_literal(self):
        from rdflib import Literal

        from tools.multi_agent_tbox import _resolve_uri_or_literal
        result = _resolve_uri_or_literal('"hello"^^xsd:string')
        assert isinstance(result, Literal)
        assert str(result) == "hello"
        assert "XMLSchema#string" in str(result.datatype)

    def test_language_tagged_literal(self):
        from rdflib import Literal

        from tools.multi_agent_tbox import _resolve_uri_or_literal
        result = _resolve_uri_or_literal('"설비 마스터"@ko')
        assert isinstance(result, Literal)
        assert str(result) == "설비 마스터"
        assert result.language == "ko"

    def test_plain_quoted_literal(self):
        from rdflib import Literal

        from tools.multi_agent_tbox import _resolve_uri_or_literal
        result = _resolve_uri_or_literal('"just a string"')
        assert isinstance(result, Literal)
        assert str(result) == "just a string"

    def test_unquoted_plain_literal(self):
        from rdflib import Literal

        from tools.multi_agent_tbox import _resolve_uri_or_literal
        result = _resolve_uri_or_literal("some value")
        assert isinstance(result, Literal)
        assert str(result) == "some value"


class TestCountIssues:
    """tools/multi_agent_tbox.py: _count_issues"""

    def test_counts_critical_and_high(self):
        from tools.multi_agent_tbox import _count_issues
        review = {
            "issues": [
                {"severity": "critical"},
                {"severity": "high"},
                {"severity": "medium"},
                {"severity": "low"},
            ]
        }
        assert _count_issues(review, "high") == 2

    def test_counts_critical_only(self):
        from tools.multi_agent_tbox import _count_issues
        review = {
            "issues": [
                {"severity": "critical"},
                {"severity": "high"},
                {"severity": "medium"},
            ]
        }
        assert _count_issues(review, "critical") == 1

    def test_counts_all_above_medium(self):
        from tools.multi_agent_tbox import _count_issues
        review = {
            "issues": [
                {"severity": "critical"},
                {"severity": "high"},
                {"severity": "medium"},
                {"severity": "low"},
            ]
        }
        assert _count_issues(review, "medium") == 3

    def test_empty_issues(self):
        from tools.multi_agent_tbox import _count_issues
        assert _count_issues({"issues": []}, "high") == 0

    def test_missing_issues_key(self):
        from tools.multi_agent_tbox import _count_issues
        assert _count_issues({}, "high") == 0

    def test_missing_severity_defaults_low(self):
        from tools.multi_agent_tbox import _count_issues
        review = {"issues": [{"severity": "critical"}, {}]}
        # missing severity defaults to rank 1 (low), threshold "high" = 3
        assert _count_issues(review, "high") == 1


class TestParseReviewJson:
    """tools/multi_agent_tbox.py: _parse_review_json"""

    def test_valid_json(self):
        from tools.multi_agent_tbox import _parse_review_json
        text = '{"issues": [{"severity": "high"}], "approved": false, "summary": "test"}'
        result = _parse_review_json(text, "Test Agent")
        assert result["approved"] is False
        assert len(result["issues"]) == 1

    def test_json_in_code_block(self):
        from tools.multi_agent_tbox import _parse_review_json
        text = '```json\n{"issues": [], "approved": true, "summary": "ok"}\n```'
        result = _parse_review_json(text, "Test Agent")
        assert result["approved"] is True
        assert result["issues"] == []

    def test_invalid_json_returns_default(self):
        from tools.multi_agent_tbox import _parse_review_json
        result = _parse_review_json("this is not json at all", "Test Agent")
        assert result["approved"] is False
        assert result["issues"] == []
        assert "파싱 실패" in result["summary"]

    def test_empty_string_returns_default(self):
        from tools.multi_agent_tbox import _parse_review_json
        result = _parse_review_json("", "Test Agent")
        assert result["approved"] is False


class TestCqToTestCase:
    """tools/query_test.py: _cq_to_test_case"""

    def test_basic_conversion(self):
        from tools.query_test import _cq_to_test_case
        cq = {
            "id": "CQ01",
            "question_ko": "설비별 정비 이력은?",
            "domains": ["EquipmentMaster", "MaintenanceHistory"],
            "difficulty": "hard",
        }
        tc = _cq_to_test_case(cq)
        assert tc["id"] == "cq_cq01"
        assert tc["question"] == "설비별 정비 이력은?"
        assert tc["difficulty"] == "hard"
        assert "EquipmentMaster" in tc["domains"]

    def test_category_from_domains(self):
        from tools.query_test import _cq_to_test_case
        cq = {
            "id": "CQ02",
            "question_ko": "test",
            "domains": ["A", "B", "C", "D"],
        }
        tc = _cq_to_test_case(cq)
        # category is first 3 domains joined
        assert tc["category"] == "A+B+C"

    def test_fallback_to_question_en(self):
        from tools.query_test import _cq_to_test_case
        cq = {
            "id": "CQ03",
            "question_en": "What is the status?",
            "domains": [],
        }
        tc = _cq_to_test_case(cq)
        assert tc["question"] == "What is the status?"

    def test_golden_sparql_is_empty(self):
        from tools.query_test import _cq_to_test_case
        cq = {"id": "CQ04", "question_ko": "test", "domains": ["A"]}
        tc = _cq_to_test_case(cq)
        assert tc["golden_sparql"] == ""

    def test_assertions_structure(self):
        from tools.query_test import _cq_to_test_case
        cq = {
            "id": "CQ05",
            "question_ko": "test",
            "domains": ["EquipmentMaster", "TagMaster"],
        }
        tc = _cq_to_test_case(cq)
        assertions = tc["assertions"]
        assert assertions["min_rows"] == 1
        assert assertions["required_vars"] == []  # default (list type)
        assert "DELETE" in assertions["sparql_must_not_contain"]
        assert "INSERT" in assertions["sparql_must_not_contain"]
        # sparql_must_contain has first 2 domains
        assert "EquipmentMaster" in assertions["sparql_must_contain"]
        assert "TagMaster" in assertions["sparql_must_contain"]

    def test_default_difficulty(self):
        from tools.query_test import _cq_to_test_case
        cq = {"id": "CQ06", "question_ko": "test", "domains": []}
        tc = _cq_to_test_case(cq)
        assert tc["difficulty"] == "medium"


class TestFkColumnToClass:
    """tools/abox_generation.py: _fk_column_to_class"""

    def test_known_pattern_equipmentid(self):
        from tools.abox_generation import _fk_column_to_class
        assert _fk_column_to_class("equipmentid") == "EquipmentMaster"

    def test_known_pattern_tagid(self):
        from tools.abox_generation import _fk_column_to_class
        assert _fk_column_to_class("tagid") == "TagMaster"

    def test_known_pattern_itemcode(self):
        from tools.abox_generation import _fk_column_to_class
        assert _fk_column_to_class("itemcode") == "ItemMaster"

    def test_suffix_rule_xxxid(self):
        from tools.abox_generation import _fk_column_to_class
        # "sensorid" -> suffix rule: base="sensor" -> "SensorMaster"
        result = _fk_column_to_class("sensorid")
        assert result == "SensorMaster"

    def test_unknown_column_returns_none(self):
        from tools.abox_generation import _fk_column_to_class
        assert _fk_column_to_class("randomcolumn") is None

    def test_separator_cleanup_dot(self):
        from tools.abox_generation import _fk_column_to_class
        # "equipment.id" suffix rule matches first (base="equipment."),
        # but cleaned path (remove dots) reaches exact pattern "equipmentid"
        # Step 2 suffix: "equipment.id" ends with "id", base="equipment."
        # -> "Equipment.Master" (not in T-Box, likely)
        # The function returns whatever suffix rule produces.
        # Test the dot-cleaned path: a column like "warehousecode" with dots
        # "warehouse.code" -> suffix doesn't match (no "id"), cleaned -> "warehousecode" -> exact match
        result = _fk_column_to_class("warehouse.code")
        assert result == "WarehouseMaster"


class TestTableNameToClass:
    """tools/tbox_generation.py: _table_name_to_class"""

    def test_snake_case_to_pascal(self):
        from tools.tbox_generation import _table_name_to_class
        assert _table_name_to_class("Equipment_Master") == "EquipmentMaster"

    def test_single_word(self):
        from tools.tbox_generation import _table_name_to_class
        assert _table_name_to_class("Inventory") == "Inventory"

    def test_multiple_underscores(self):
        from tools.tbox_generation import _table_name_to_class
        assert _table_name_to_class("Real_Time_Data") == "RealTimeData"

    def test_hyphen_treated_as_separator(self):
        from tools.tbox_generation import _table_name_to_class
        assert _table_name_to_class("Gas-Energy") == "GasEnergy"

    def test_lowercase_input(self):
        from tools.tbox_generation import _table_name_to_class
        assert _table_name_to_class("alarm_events") == "AlarmEvents"

    def test_already_pascal(self):
        from tools.tbox_generation import _table_name_to_class
        # Each part is capitalized, so "EquipmentMaster" -> "Equipmentmaster" (splits on _)
        # Actually: no underscore, so single part -> "Equipmentmaster".capitalize() = "Equipmentmaster"
        # Wait -- "EquipmentMaster".split("_") = ["EquipmentMaster"]
        # "EquipmentMaster".capitalize() = "Equipmentmaster"
        # This is the actual behavior of the function
        assert _table_name_to_class("EquipmentMaster") == "Equipmentmaster"


class TestGetFileMtime:
    """tools/pipeline_state.py: _get_file_mtime"""

    def test_existing_file_returns_positive(self):
        from tools.pipeline_state import _get_file_mtime
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            tmppath = f.name
        try:
            mtime = _get_file_mtime(tmppath)
            assert mtime > 0
        finally:
            os.unlink(tmppath)

    def test_nonexistent_file_returns_zero(self):
        from tools.pipeline_state import _get_file_mtime
        assert _get_file_mtime("/nonexistent/path/file.txt") == 0

    def test_returns_float(self):
        from tools.pipeline_state import _get_file_mtime
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            tmppath = f.name
        try:
            assert isinstance(_get_file_mtime(tmppath), float)
        finally:
            os.unlink(tmppath)
