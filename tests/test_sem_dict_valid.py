"""Tests for tools/semantic_dict_validation.py — dictionary quality validation."""

import json
from unittest.mock import patch

from tools.semantic_dict_validation import _validate, validate_semantic_dictionary


def _build_tbox_ttl() -> str:
    """Build a minimal T-Box TTL string for testing.

    Uses the actual project namespace from domain_config.json.
    """
    from domain.namespaces import DOMAIN_NS, NS_PREFIX
    return f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

{NS_PREFIX}:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en .

{NS_PREFIX}:EquipmentStatus a owl:Class ;
    rdfs:label "Equipment Status"@en .

{NS_PREFIX}:hasEquipmentStatus a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:EquipmentMaster ;
    rdfs:range {NS_PREFIX}:EquipmentStatus .
"""


def _build_complete_dict() -> dict:
    """Build a complete semantic dictionary that should pass validation."""
    return {
        "metadata": {"version": "1.0"},
        "classes": {
            "EquipmentMaster": {
                "label_ko": "설비 마스터",
                "label_en": "Equipment Master",
                "description_ko": "설비 기본 정보",
                "instance_count": 50,
            },
            "EquipmentStatus": {
                "label_ko": "설비 상태",
                "label_en": "Equipment Status",
                "description_ko": "설비 상태 정보",
                "instance_count": 50,
            },
        },
        "object_properties": {
            "hasEquipmentStatus": {
                "label_ko": "설비 상태",
                "domain": ["EquipmentMaster"],
                "range": ["EquipmentStatus"],
            },
        },
        "sparql_guide": {
            "engine_compatibility": {"note": "ok"},
            "anti_patterns": {"ap1": "avoid"},
            "common_patterns": {"cp1": "use"},
        },
        "common_mistakes": {"m1": "fix"},
        "question_templates": {f"qt{i}": f"q{i}" for i in range(10)},
        "class_quick_reference": {
            "EquipmentMaster": "EQ",
            "EquipmentStatus": "ES",
        },
        "process_flow": {"bf": "blast furnace"},
        "functional_properties": {},
    }


class TestValidateInternal:
    """_validate: internal validation logic."""

    def test_complete_dict_passes(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        abox_file = tmp_path / "a_box.ttl"
        abox_file.write_text("")  # no A-Box

        result = _validate(_build_complete_dict(), str(tbox_file), str(abox_file))
        assert result["passed"] is True
        assert result["summary"]["critical"] == 0
        assert result["summary"]["high"] == 0

    def test_missing_section_is_critical(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        del dict_data["metadata"]

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        assert result["passed"] is False
        missing = [i for i in result["issues"] if i["rule"] == "missing_section"]
        assert len(missing) >= 1

    def test_empty_section_is_warning(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        dict_data["metadata"] = {}  # empty but present

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        empty_warnings = [i for i in result["issues"] if i["rule"] == "empty_section"]
        assert len(empty_warnings) >= 1

    def test_missing_class_in_dict_detected(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        # Remove EquipmentStatus from dict
        del dict_data["classes"]["EquipmentStatus"]
        del dict_data["class_quick_reference"]["EquipmentStatus"]

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        assert result["passed"] is False
        missing_cls = [i for i in result["issues"]
                       if i["rule"] == "class_missing_in_dict"]
        assert len(missing_cls) >= 1
        assert "EquipmentStatus" in missing_cls[0]["message"]

    def test_extra_class_in_dict_is_warning(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        dict_data["classes"]["NonexistentClass"] = {"label_ko": "X", "label_en": "X", "description_ko": "X"}

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        extra = [i for i in result["issues"] if i["rule"] == "class_extra_in_dict"]
        assert len(extra) >= 1

    def test_missing_objprop_in_dict_detected(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        dict_data["object_properties"] = {}  # remove all OPs

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        missing_ops = [i for i in result["issues"]
                       if i["rule"] == "objprop_missing_in_dict"]
        assert len(missing_ops) >= 1

    def test_missing_label_ko_is_high(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        dict_data["classes"]["EquipmentMaster"]["label_ko"] = ""

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        label_issues = [i for i in result["issues"]
                        if i["rule"] == "missing_label_ko"]
        assert len(label_issues) >= 1

    def test_op_domain_not_in_classes(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_data = _build_complete_dict()
        dict_data["object_properties"]["hasEquipmentStatus"]["domain"] = ["Nonexistent"]

        result = _validate(dict_data, str(tbox_file), "/nonexistent")
        domain_issues = [i for i in result["issues"]
                         if i["rule"] == "op_domain_not_in_classes"]
        assert len(domain_issues) >= 1

    def test_coverage_rates(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        result = _validate(_build_complete_dict(), str(tbox_file), "/nonexistent")
        assert result["coverage"]["class_match_rate"] == 100.0
        assert result["coverage"]["op_match_rate"] == 100.0


class TestValidateSemanticDictionary:
    """validate_semantic_dictionary: MCP tool entry point."""

    def test_missing_dict_file(self, tmp_path):
        with patch(
            "tools.semantic_dict_validation.GENERATED_DIR",
            str(tmp_path),
        ):
            result = json.loads(
                validate_semantic_dictionary(dict_path="missing.json")
            )
        assert "error" in result
        assert "hint" in result

    def test_invalid_json(self, tmp_path):
        dict_file = tmp_path / "bad.json"
        dict_file.write_text("{not valid json")
        with patch(
            "tools.semantic_dict_validation.GENERATED_DIR",
            str(tmp_path),
        ):
            result = json.loads(
                validate_semantic_dictionary(dict_path=dict_file.name)
            )
        assert "error" in result

    def test_happy_path(self, tmp_path):
        tbox_file = tmp_path / "t_box.ttl"
        tbox_file.write_text(_build_tbox_ttl())
        dict_file = tmp_path / "dict.json"
        dict_file.write_text(json.dumps(_build_complete_dict()))
        with (
            patch("tools.semantic_dict_validation.GENERATED_DIR", str(tmp_path)),
            patch(
                "tools.semantic_dict_validation.GENERATED_TBOX_DIR",
                str(tmp_path),
            ),
            patch(
                "tools.semantic_dict_validation.GENERATED_ABOX_DIR",
                str(tmp_path),
            ),
        ):
            result = json.loads(validate_semantic_dictionary(
                dict_path=dict_file.name,
                tbox_path=tbox_file.name,
                abox_path="missing-abox.ttl",
            ))
        assert result["passed"] is True
