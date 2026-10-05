"""Tests for domain/uri_conventions.py — pure unit tests."""

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.uri_conventions import (
    class_uri,
    instance_uri,
    local_name,
    property_uri,
    transaction_instance_uri,
)


class TestClassUri:
    def test_normal_input(self):
        assert class_uri("EquipmentMaster") == f"{DOMAIN_NS}EquipmentMaster"

    def test_forces_pascal_case(self):
        result = class_uri("equipmentMaster")
        assert result == f"{DOMAIN_NS}EquipmentMaster"

    def test_already_pascal(self):
        result = class_uri("TagMaster")
        assert result == f"{DOMAIN_NS}TagMaster"

    def test_single_char(self):
        result = class_uri("a")
        assert result == f"{DOMAIN_NS}A"


class TestPropertyUri:
    def test_normal_input(self):
        assert property_uri("hasEquipmentStatus") == f"{DOMAIN_NS}hasEquipmentStatus"

    def test_forces_camel_case(self):
        result = property_uri("HasEquipmentStatus")
        assert result == f"{DOMAIN_NS}hasEquipmentStatus"

    def test_already_camel(self):
        result = property_uri("equipmentID")
        assert result == f"{DOMAIN_NS}equipmentID"

    def test_single_char(self):
        result = property_uri("A")
        assert result == f"{DOMAIN_NS}a"


class TestInstanceUri:
    def test_normal_pk(self):
        result = instance_uri("EquipmentMaster", "EQ001")
        assert result == f"{DOMAIN_INST_NS}EquipmentMaster_EQ001"

    def test_special_chars_sanitized(self):
        # B2 fix: unicode/special chars are now percent-encoded instead of replaced with _
        result = instance_uri("EquipmentMaster", "EQ/001 test")
        assert result == f"{DOMAIN_INST_NS}EquipmentMaster_EQ%2F001%20test"

    def test_numeric_pk(self):
        result = instance_uri("TagMaster", 12345)
        assert result == f"{DOMAIN_INST_NS}TagMaster_12345"

    def test_hyphen_preserved(self):
        result = instance_uri("ItemMaster", "ITEM-001")
        assert result == f"{DOMAIN_INST_NS}ItemMaster_ITEM-001"


class TestTransactionInstanceUri:
    def test_normal(self):
        result = transaction_instance_uri("AlarmEvents", "EQ001", "20260101120000")
        assert result == f"{DOMAIN_INST_NS}AlarmEvents_EQ001_20260101120000"

    def test_special_chars_in_pk(self):
        result = transaction_instance_uri("AlarmEvents", "EQ 001", "20260101120000")
        assert result == f"{DOMAIN_INST_NS}AlarmEvents_EQ%20001_20260101120000"


class TestLocalName:
    def test_hash_uri(self):
        assert local_name("http://example.org/test-ontology#EquipmentMaster") == "EquipmentMaster"

    def test_slash_uri(self):
        assert local_name("https://spec.industrialontologies.org/ontology/core/Core/Entity") == "Entity"

    def test_no_separator(self):
        assert local_name("EquipmentMaster") == "EquipmentMaster"

    def test_multiple_hashes(self):
        assert local_name("http://example.com#foo#bar") == "bar"

    def test_empty_string(self):
        assert local_name("") == ""
