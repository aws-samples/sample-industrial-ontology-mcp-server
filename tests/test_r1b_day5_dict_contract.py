"""기능 단계 — generate_abox vocabulary contract 참조 검증.

dict_contract 로더 + _contract_match_dp 조회 + _col_to_prop 우선 경로 + 통합
(contract 제공 시 해당 DP 이름 우선, 없으면 T-Box fallback).
"""
from __future__ import annotations

import json


def test_load_dict_contract_returns_empty_on_missing_file(tmp_path):
    """파일 없으면 빈 dict."""
    from tools.abox_generation import _load_dict_contract
    missing = tmp_path / "does_not_exist.json"
    assert _load_dict_contract(str(missing)) == {}


def test_load_dict_contract_returns_empty_on_invalid_json(tmp_path):
    """손상된 JSON → 빈 dict, 파이프라인 중단 없음."""
    from tools.abox_generation import _load_dict_contract
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert _load_dict_contract(str(bad)) == {}


def test_load_dict_contract_extracts_class_dp_mapping(tmp_path):
    """정상 딕셔너리에서 {class: {dp_lower: dp_name}} 추출."""
    from tools.abox_generation import _load_dict_contract
    d = {
        "classes": {
            "EquipmentStatus": {
                "datatype_properties": {
                    "equipmentStatusValue": {"range": "xsd:string"},
                    "equipmentStatusTimestamp": {"range": "xsd:dateTime"},
                },
            },
            "AlarmEvents": {
                "datatype_properties": {
                    "alarmEventsSeverity": {"range": "xsd:string"},
                },
            },
        }
    }
    p = tmp_path / "dict.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    contract = _load_dict_contract(str(p))

    assert "EquipmentStatus" in contract
    assert contract["EquipmentStatus"]["equipmentstatusvalue"] == "equipmentStatusValue"
    assert contract["EquipmentStatus"]["equipmentstatustimestamp"] == "equipmentStatusTimestamp"
    assert "AlarmEvents" in contract
    assert contract["AlarmEvents"]["alarmeventsseverity"] == "alarmEventsSeverity"


def test_load_dict_contract_skips_class_without_dps(tmp_path):
    """datatype_properties 섹션 없는 class 는 empty dict 로 등록."""
    from tools.abox_generation import _load_dict_contract
    d = {"classes": {"EmptyClass": {"label_ko": "비어있음"}}}
    p = tmp_path / "dict.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    contract = _load_dict_contract(str(p))
    assert "EmptyClass" in contract
    assert contract["EmptyClass"] == {}


def test_contract_match_dp_exact_camel_match():
    """camelLower 정확 매칭."""
    from tools.abox_generation import _contract_match_dp
    contract = {
        "EquipmentStatus": {
            "equipmentstatusvalue": "equipmentStatusValue",
        },
    }
    # CSV 컬럼 "equipmentStatusValue" 와 동일 이름
    result = _contract_match_dp("equipmentStatusValue", "EquipmentStatus", contract)
    assert result == "equipmentStatusValue"


def test_contract_match_dp_class_prefix_combine():
    """CSV 컬럼 "Value" → class prefix 결합으로 equipmentStatusValue 매칭."""
    from tools.abox_generation import _contract_match_dp
    contract = {
        "EquipmentStatus": {
            "equipmentstatusvalue": "equipmentStatusValue",
        },
    }
    # snake_case "Value" → "equipmentStatus" + "Value" = equipmentStatusValue
    result = _contract_match_dp("Value", "EquipmentStatus", contract)
    assert result == "equipmentStatusValue"


def test_contract_match_dp_category_prefix_drop():
    """class 가 Category prefix (Process/Inventory 등) 를 포함하면 드롭 후 재시도.

    Architect 가 `Process_Blast_Furnace` CSV 를 ObjectClass 로 변환할 때
    class 이름은 `ProcessBlastFurnace` 이지만 DP 이름은 `blastFurnaceHotAirTempC`
    (Process 카테고리 접두어 드롭) 을 사용하는 경우가 있다. Contract matcher 는
    이 variant 를 자동 감지해야 한다.
    """
    from tools.abox_generation import _contract_match_dp
    contract = {
        "ProcessBlastFurnace": {
            # 실제 DP 이름은 Process 가 드롭된 blastFurnaceHotAirTempC
            "blastfurnacehotairtempc": "blastFurnaceHotAirTempC",
        },
    }
    # CSV 컬럼 Hot_Air_Temp_C + class ProcessBlastFurnace → blastFurnaceHotAirTempC
    result = _contract_match_dp("Hot_Air_Temp_C", "ProcessBlastFurnace", contract)
    assert result == "blastFurnaceHotAirTempC"


def test_contract_match_dp_category_prefix_drop_production():
    """ProductionResult → Result (Production 드롭) 같은 variant 도 지원."""
    from tools.abox_generation import _contract_match_dp
    contract = {
        "ProductionResult": {
            "resultyieldrate": "resultYieldRate",
        },
    }
    # Architect 가 Production 카테고리를 드롭한 경우
    result = _contract_match_dp("Yield_Rate", "ProductionResult", contract)
    assert result == "resultYieldRate"


def test_contract_match_dp_preserves_original_when_no_prefix():
    """Category prefix 없는 class 는 기존 동작 유지."""
    from tools.abox_generation import _contract_match_dp
    contract = {
        "AlarmEvents": {
            "alarmeventstimestamp": "alarmEventsTimestamp",
        },
    }
    result = _contract_match_dp("Timestamp", "AlarmEvents", contract)
    assert result == "alarmEventsTimestamp"


def test_contract_match_dp_expands_chemical_element_abbreviations():
    """CSV `C_Percent` + ChemicalAnalysis → chemicalAnalysisCarbonPercent.

    Architect 가 C/Si/Mn 등 원소 축약을 풀네임으로 번역한 경우 matcher 도
    자동 확장해서 매칭해야 A-Box 가 해당 triple 을 생성 가능.
    """
    from tools.abox_generation import _contract_match_dp
    contract = {
        "ChemicalAnalysis": {
            "chemicalanalysiscarbonpercent": "chemicalAnalysisCarbonPercent",
            "chemicalanalysissiliconpercent": "chemicalAnalysisSiliconPercent",
            "chemicalanalysismanganesepercent": "chemicalAnalysisManganesePercent",
            "chemicalanalysisphosphoruspercent": "chemicalAnalysisPhosphorusPercent",
            "chemicalanalysissulfurpercent": "chemicalAnalysisSulfurPercent",
            "chemicalanalysischromiumpercent": "chemicalAnalysisChromiumPercent",
            "chemicalanalysisnickelpercent": "chemicalAnalysisNickelPercent",
        },
    }
    assert _contract_match_dp("C_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisCarbonPercent"
    assert _contract_match_dp("Si_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisSiliconPercent"
    assert _contract_match_dp("Mn_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisManganesePercent"
    assert _contract_match_dp("P_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisPhosphorusPercent"
    assert _contract_match_dp("S_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisSulfurPercent"
    assert _contract_match_dp("Cr_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisChromiumPercent"
    assert _contract_match_dp("Ni_Percent", "ChemicalAnalysis", contract) \
        == "chemicalAnalysisNickelPercent"


def test_contract_match_dp_returns_none_when_class_missing():
    """contract 에 class 없음 → None."""
    from tools.abox_generation import _contract_match_dp
    contract = {"OtherClass": {"foo": "foo"}}
    assert _contract_match_dp("bar", "MissingClass", contract) is None


def test_contract_match_dp_returns_none_when_col_not_in_class():
    """class 존재하지만 DP 매칭 안 됨 → None (T-Box fallback 유도)."""
    from tools.abox_generation import _contract_match_dp
    contract = {"EquipmentStatus": {"statusvalue": "statusValue"}}
    result = _contract_match_dp("irrelevantColumn", "EquipmentStatus", contract)
    assert result is None


def test_contract_match_dp_empty_contract_returns_none():
    """contract=None 또는 {} → None."""
    from tools.abox_generation import _contract_match_dp
    assert _contract_match_dp("anything", "AnyClass", None) is None
    assert _contract_match_dp("anything", "AnyClass", {}) is None


def _make_tbox_info(class_props: dict):
    """최소 tbox_info (Path B tests 와 동일 helper)."""
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
        "dp_ranges": {},
        "dp_domains": dp_domains,
        "subclass_of": {},
    }


def test_col_to_prop_contract_overrides_tbox_lookup():
    """contract 매칭이 T-Box direct lookup 보다 우선."""
    from tools.abox_generation import _col_to_prop
    # T-Box 에 statusValue + equipmentStatusValue 둘 다 있음
    tbox_info = _make_tbox_info({
        "EquipmentStatus": {
            "statusvalue": "statusValue",
            "equipmentstatusvalue": "equipmentStatusValue",
        },
    })
    # contract 는 equipmentStatusValue 로 고정
    contract = {"EquipmentStatus": {"equipmentstatusvalue": "equipmentStatusValue"}}
    result = _col_to_prop("Value", "EquipmentStatus", tbox_info, {}, dict_contract=contract)
    assert result == "equipmentStatusValue"


def test_col_to_prop_falls_back_to_tbox_when_contract_missing_entry():
    """contract 에 class 의 DP 없으면 T-Box fallback."""
    from tools.abox_generation import _col_to_prop
    tbox_info = _make_tbox_info({
        "EquipmentStatus": {"equipmentstatusvalue": "equipmentStatusValue"},
    })
    contract = {"OtherClass": {"foo": "foo"}}  # EquipmentStatus 없음
    # class-prefix suffix 매칭 (Path B-2) 에 의해 equipmentStatusValue 여전히 반환
    result = _col_to_prop("Value", "EquipmentStatus", tbox_info, {}, dict_contract=contract)
    assert result == "equipmentStatusValue"


def test_col_to_prop_none_contract_preserves_path_b_behavior():
    """dict_contract=None 이면 기존 Path B 동작 (backward compat)."""
    from tools.abox_generation import _col_to_prop
    tbox_info = _make_tbox_info({
        "EquipmentStatus": {"equipmentstatusvalue": "equipmentStatusValue"},
    })
    result = _col_to_prop("Value", "EquipmentStatus", tbox_info, {}, dict_contract=None)
    assert result == "equipmentStatusValue"


def test_col_to_prop_contract_skipped_if_not_in_tbox():
    """contract 에 있지만 T-Box 에 실제 선언 없으면 fallback (domain guard)."""
    from tools.abox_generation import _col_to_prop
    # T-Box 에는 DP 선언 없음
    tbox_info = _make_tbox_info({"EquipmentStatus": {}})
    # contract 는 참조하지만 실제 T-Box 에 없음
    contract = {"EquipmentStatus": {"equipmentstatusvalue": "equipmentStatusValue"}}
    # fallback 모두 실패 → None
    result = _col_to_prop("Value", "EquipmentStatus", tbox_info, {}, dict_contract=contract)
    assert result is None


def test_generate_abox_accepts_use_dict_contract_kwarg():
    """generate_abox(use_dict_contract=...) 인자 지원 — API 일관성."""
    import inspect

    from tools import abox_generation as ag
    sig = inspect.signature(ag.generate_abox)
    assert "use_dict_contract" in sig.parameters
    # default True — 기본 활성
    assert sig.parameters["use_dict_contract"].default is True
