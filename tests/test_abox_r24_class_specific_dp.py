"""R24 regression — class-specific DP prefix matching preference.

When T-Box declares both a generic DP (e.g. `energySourceId` with
domain=MaterialResource) and a class-specific DP (`energySourceMasterId` with
domain=EnergySourceMaster), `_col_to_prop` must prefer the class-specific
candidate so that completeness check does not FAIL with low coverage on the
specific DP.
"""
from __future__ import annotations

from tools.abox_generation import _col_to_prop


def _mk_tbox_info() -> dict:
    """Minimal tbox_info for _col_to_prop."""
    return {
        "datatype_properties": {
            "energysourceid": "energySourceId",
            "energysourcemasterid": "energySourceMasterId",
        },
        "dp_domains": {
            "energysourceid": {"MaterialResource"},
            "energysourcemasterid": {"EnergySourceMaster"},
        },
        "subclass_of": {
            "EnergySourceMaster": ["MaterialResource"],
        },
    }


def test_class_specific_dp_preferred_over_generic():
    """EnergySourceMaster.Energy_Source_ID → energySourceMasterId (NOT energySourceId)."""
    tbox_info = _mk_tbox_info()
    result = _col_to_prop("Energy_Source_ID", "EnergySourceMaster", tbox_info, {})
    assert result == "energySourceMasterId", (
        f"expected class-specific DP, got {result!r}. Generic `energySourceId` "
        f"was incorrectly chosen — completeness check would FAIL for the "
        f"specific DP."
    )


def test_generic_dp_still_used_when_no_specific_alternative():
    """범용 DP 는 class-specific alternative 가 없으면 여전히 선택되어야 한다."""
    tbox_info = {
        "datatype_properties": {"energysourceid": "energySourceId"},
        "dp_domains": {"energysourceid": {"MaterialResource"}},
        "subclass_of": {"FuelConsumption": ["MaterialResource"]},
    }
    result = _col_to_prop("Energy_Source_ID", "FuelConsumption", tbox_info, {})
    assert result == "energySourceId"


def test_class_prefix_shorter_token_still_works():
    """1-토큰 prefix (SoilMonitoring.Value → soilValue) R20 regression."""
    tbox_info = {
        "datatype_properties": {"soilvalue": "soilValue"},
        "dp_domains": {"soilvalue": {"SoilMonitoring"}},
        "subclass_of": {},
    }
    result = _col_to_prop("Value", "SoilMonitoring", tbox_info, {})
    assert result == "soilValue"


def test_override_mapping_still_highest_priority():
    """class_prop_mapping 오버라이드는 여전히 최우선."""
    tbox_info = {
        "datatype_properties": {
            "energysourceid": "energySourceId",
            "energysourcemasterid": "energySourceMasterId",
            "customid": "customId",
        },
        "dp_domains": {
            "energysourceid": {"MaterialResource"},
            "energysourcemasterid": {"EnergySourceMaster"},
            "customid": {"EnergySourceMaster"},
        },
        "subclass_of": {"EnergySourceMaster": ["MaterialResource"]},
    }
    # override 가 있으면 class-specific prefix 매칭보다도 우선
    override = {"EnergySourceMaster/energysourceid": "customId"}
    result = _col_to_prop("Energy_Source_ID", "EnergySourceMaster", tbox_info, override)
    assert result == "customId"


def test_no_matching_dp_returns_none():
    """매칭되는 DP 가 T-Box 에 없으면 None."""
    tbox_info = {
        "datatype_properties": {"something_else": "somethingElse"},
        "dp_domains": {"something_else": {"OtherClass"}},
        "subclass_of": {},
    }
    result = _col_to_prop("Energy_Source_ID", "EnergySourceMaster", tbox_info, {})
    assert result is None
