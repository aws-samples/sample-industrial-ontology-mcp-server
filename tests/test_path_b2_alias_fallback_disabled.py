"""Path B-2 — _COMMON_DP_BY_ALIAS 폴백 비활성화 + class-specific semantic 접미사 매칭 검증.

_col_to_prop 의 알고리즘 변화:
- class-specific 후보 (prefix + semantic suffix Value/Code/Type/Name) 까지 시도
- T-Box 에 class-specific DP 만 있고 컬럼명 != DP 이름이면 매칭됨
- alias 폴백 (timestamp → hasTimestamp, status → hasStatus) 은 제거됨
- class-specific 에서 못 찾으면 None (caller 가 on-demand 주입 또는 skip)
"""
from __future__ import annotations


def _make_tbox_info(class_props: dict, dp_ranges: dict | None = None):
    """Minimal tbox_info for _col_to_prop tests."""
    # flatten class_props to datatype_properties + dp_domains
    dt_props = {}
    dp_domains: dict[str, set[str]] = {}
    for cls, props in class_props.items():
        for key, name in props.items():
            dt_props[key] = name
            dp_domains.setdefault(key, set()).add(cls)
    return {
        "classes": {cls.lower(): cls for cls in class_props},
        "datatype_properties": dt_props,
        "object_properties": {},
        "class_props": class_props,
        "dp_ranges": dp_ranges or {},
        "dp_domains": dp_domains,
        "subclass_of": {},
    }


def test_col_to_prop_finds_class_specific_value_suffix():
    """CSV 컬럼 'Status' → T-Box 의 'equipmentStatusValue' 매칭 (Value 접미사 variant)."""
    from tools.abox_generation import _col_to_prop
    tbox_info = _make_tbox_info({
        "EquipmentStatus": {
            "equipmentstatusvalue": "equipmentStatusValue",
        },
    })
    # class-prefix semantic suffix variant: "equipmentStatus" + "StatusValue" 가 아니라
    # "equipmentStatus" 내 "status" 이미 있으니 "equipmentStatusValue" 로 매칭되어야
    result = _col_to_prop("Status", "EquipmentStatus", tbox_info, {})
    assert result == "equipmentStatusValue"


def test_col_to_prop_finds_class_specific_code_suffix():
    """'Type' 컬럼 → 'productTypeCode' 같은 Code 접미사 매칭."""
    from tools.abox_generation import _col_to_prop
    tbox_info = _make_tbox_info({
        "Product": {
            "producttypecode": "productTypeCode",
        },
    })
    result = _col_to_prop("Type", "Product", tbox_info, {})
    assert result == "productTypeCode"


def test_col_to_prop_alias_fallback_removed():
    """Path B-2: T-Box 에 hasTimestamp (domain 없음) 이 있어도 'Timestamp' 컬럼의
    class-specific 후보가 없으면 alias 폴백 안 함 — None 반환."""
    from tools.abox_generation import _col_to_prop
    # T-Box 에 hasTimestamp (domain empty) + EquipmentStatus 클래스 정의만
    tbox_info = {
        "classes": {"equipmentstatus": "EquipmentStatus"},
        "datatype_properties": {"hastimestamp": "hasTimestamp"},
        "object_properties": {},
        "class_props": {
            "EquipmentStatus": {}  # 이 클래스에 속한 DP 없음
        },
        "dp_ranges": {},
        # hasTimestamp 는 domain 없음 → 과거엔 class 아무거나 허용 → alias 매칭 OK
        # 지금은 class-specific 후보 없음 + alias 폴백 제거 → None
        "dp_domains": {"hastimestamp": set()},
        "subclass_of": {},
    }
    result = _col_to_prop("Timestamp", "EquipmentStatus", tbox_info, {})
    # alias fallback 제거됨 — class-specific 후보 없으면 None
    # (단, domain 없는 hasTimestamp 는 generic match 로 step 2 에서 잡힐 수 있음 —
    #  의도된 동작. 실제로는 P-B-3 에서 generic 선언 자체를 없앨 예정)
    # 현재 단계에서는 step 2 의 camel_lower 매칭이 되므로 "hasTimestamp" 반환
    # (domain empty 이면 호환 통과).
    # 따라서 이 테스트는 camel_lower 가 "timestamp" 이고 "hastimestamp" 와 다르므로 None.
    assert result is None


def test_col_to_prop_class_specific_wins_over_generic():
    """T-Box 에 generic 'status' + class-specific 'equipmentStatusValue' 둘 다 있으면
    class-specific 우선."""
    from tools.abox_generation import _col_to_prop
    # generic: status (domain EquipmentStatus), specific: equipmentStatusValue (EquipmentStatus)
    tbox_info = _make_tbox_info({
        "EquipmentStatus": {
            "status": "status",
            "equipmentstatusvalue": "equipmentStatusValue",
        },
    })
    result = _col_to_prop("Status", "EquipmentStatus", tbox_info, {})
    # class-prefix match step 이 먼저 시도됨 → equipmentStatusValue
    assert result == "equipmentStatusValue"


def test_col_to_prop_returns_none_when_no_class_match():
    """T-Box 에 해당 class 의 DP 가 전혀 없으면 None."""
    from tools.abox_generation import _col_to_prop
    tbox_info = _make_tbox_info({
        "UnknownClass": {},
    })
    result = _col_to_prop("Status", "UnknownClass", tbox_info, {})
    assert result is None


def test_col_to_prop_step2_generic_camel_match_preserved():
    """step 2 의 camel_lower 정확 매칭은 여전히 작동 (backward compat)."""
    from tools.abox_generation import _col_to_prop
    # DP 이름 그대로 "name" 이 T-Box 에 있고 EquipmentMaster 클래스의 DP 로 매핑
    tbox_info = _make_tbox_info({
        "EquipmentMaster": {"name": "name"},
    })
    result = _col_to_prop("Name", "EquipmentMaster", tbox_info, {})
    assert result == "name"
