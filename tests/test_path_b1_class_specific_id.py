"""Path B-1 — class-specific ID DP 탐지 로직 검증.

hasIdentifier 하드코딩을 class-specific DP ({classCamelLower}Id 등) + dcterms
fallback 으로 전환한 경로 회귀.
"""
from __future__ import annotations


def test_class_specific_id_dp_finds_exact_match():
    """class_name + 'id' 접미사가 T-Box 에 있으면 반환."""
    from tools.abox_generation import _class_specific_id_dp
    tbox_info = {
        "class_props": {
            "EquipmentMaster": {
                "equipmentmasterid": "equipmentMasterId",
                "name": "name",
            },
        },
    }
    assert _class_specific_id_dp("EquipmentMaster", tbox_info) == "equipmentMasterId"


def test_class_specific_id_dp_strips_master_suffix():
    """Master/Events 등 접미사 제거 후 재시도."""
    from tools.abox_generation import _class_specific_id_dp
    tbox_info = {
        "class_props": {
            "EquipmentMaster": {
                "equipmentid": "equipmentId",  # equipmentMaster stripped → equipment + id
            },
        },
    }
    assert _class_specific_id_dp("EquipmentMaster", tbox_info) == "equipmentId"


def test_class_specific_id_dp_prefers_code_suffix():
    """id 없으면 code/number/no 접미사도 허용."""
    from tools.abox_generation import _class_specific_id_dp
    tbox_info = {
        "class_props": {
            "Item": {
                "itemcode": "itemCode",
            },
        },
    }
    assert _class_specific_id_dp("Item", tbox_info) == "itemCode"


def test_class_specific_id_dp_returns_none_when_no_match():
    """ID 후보가 없으면 None → caller 가 dcterms fallback."""
    from tools.abox_generation import _class_specific_id_dp
    tbox_info = {
        "class_props": {
            "UnknownClass": {
                "name": "name",
                "description": "description",
            },
        },
    }
    assert _class_specific_id_dp("UnknownClass", tbox_info) is None


def test_class_specific_id_dp_handles_missing_class():
    """class_props 에 클래스 없으면 None."""
    from tools.abox_generation import _class_specific_id_dp
    tbox_info = {"class_props": {}}
    assert _class_specific_id_dp("NotInTBox", tbox_info) is None


def test_class_specific_id_dp_empty_class_name():
    """빈 class_name 방어."""
    from tools.abox_generation import _class_specific_id_dp
    assert _class_specific_id_dp("", {"class_props": {}}) is None
    assert _class_specific_id_dp(None, {"class_props": {}}) is None


def test_class_specific_id_dp_multiple_candidates_picks_shortest():
    """여러 id 후보 중 가장 짧은 이름 (base) 선호."""
    from tools.abox_generation import _class_specific_id_dp
    # Tag: tagId + tagMasterId + tagNumber — 가장 짧은 tagId 우선
    tbox_info = {
        "class_props": {
            "Tag": {
                "tagid": "tagId",
                "tagmasterid": "tagMasterId",
                "tagnumber": "tagNumber",
            },
        },
    }
    # 후보1 (tag+id) 먼저 매치해 "tagId" 반환
    assert _class_specific_id_dp("Tag", tbox_info) == "tagId"


def test_dcterms_namespace_imported():
    """_DCTERMS_NS fallback namespace 가 모듈에 import 되어 있음."""
    from tools.abox_generation import _DCTERMS_NS
    assert str(_DCTERMS_NS) == "http://purl.org/dc/terms/"
    assert str(_DCTERMS_NS.identifier) == "http://purl.org/dc/terms/identifier"
