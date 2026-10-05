"""``canonical_hints.json`` 의 DP 참조가 T-Box 재생성으로 낡는 것을 막는 가드.

**배경 (2026-07-26 실측)**: hints 는 SME 검증 집계 규칙을 **DP 이름** 으로 기록한다:

    "압연 소재처리량 측정": {
        "property":        "processResultDWeightCol3",
        "filter_property": "processResultDWeightCol2",
        "filter_rule":     "processResultDWeightCol2 != 0",
        ...
    }

그런데 DP 이름은 LLM 이 작명하므로 **S2 재생성마다 바뀐다**. 규칙 파일의 property
참조 9건이 전부 옛 이름이었고 (``processResultDWeightCol2`` →
``processResultDCoilWeight``), 그 결과 "산출물 중량 0 제외" 라는 핵심 규칙이 딕셔너리에
텍스트로는 있지만 **가리키는 DP 가 존재하지 않아 사용 불가** 했다. CQ01 을 이 규칙 없이
질의해 약 430건 (정답과 소수 건 차이) 오답이 나왔다.

해결: hints 는 이미 컬럼 코드(``"filter_column": "WEIGHT_COL_2"``) 를 함께 갖고 있고,
모든 DP 는 ``source_columns`` 를 갖는다. **컬럼은 Oracle 스키마에서 오므로 안정적** 이라
컬럼 기준으로 현재 DP 이름을 결정적으로 재해석할 수 있다.
"""
from __future__ import annotations

import json
import os

import pytest

from tools.semantic_dictionary import _resolve_canonical_designations


def _classes(spec: dict) -> dict:
    """``{class: {datatype_properties: {dp: {source_columns: [...]}}}}`` 조립."""
    return {
        cls: {"datatype_properties": {
            dp: {"source_columns": cols} for dp, cols in dps.items()
        }}
        for cls, dps in spec.items()
    }


def test_stale_dp_name_is_resolved_by_column():
    """DP 이름이 낡았어도 컬럼이 맞으면 현재 이름으로 재해석한다."""
    canonical = {
        "압연 소재처리량 측정": {
            "class": "ProcessResultD",
            "property": "processResultDWeightCol3",      # 옛 이름
            "column": "WEIGHT_COL_3",
            "filter_property": "processResultDWeightCol2",        # 옛 이름
            "filter_column": "WEIGHT_COL_2",
            "filter_rule": "processResultDWeightCol2 != 0",
        },
    }
    classes = _classes({"ProcessResultD": {
        "processResultDMaterialSpreadWeight": ["WEIGHT_COL_3"],
        "processResultDCoilWeight": ["WEIGHT_COL_2"],
    }})

    out = _resolve_canonical_designations(canonical, classes)
    spec = out["압연 소재처리량 측정"]

    assert spec["property_resolved"] == "processResultDMaterialSpreadWeight"
    assert spec["filter_property_resolved"] == "processResultDCoilWeight"
    assert spec["_resolution_status"] == "resolved_by_column"
    # 원본은 보존 — 무엇이 드리프트했는지 검토 가능해야 한다
    assert spec["property"] == "processResultDWeightCol3"


def test_current_dp_name_marked_ok():
    """이름이 이미 맞으면 status=ok."""
    canonical = {
        "x": {"class": "C", "property": "cWeight", "column": "WGT"},
    }
    classes = _classes({"C": {"cWeight": ["WGT"]}})

    spec = _resolve_canonical_designations(canonical, classes)["x"]
    assert spec["_resolution_status"] == "ok"
    assert spec["property_resolved"] == "cWeight"


def test_unresolvable_marked_stale():
    """컬럼으로도 못 찾으면 stale — 규칙이 무효임을 드러낸다."""
    canonical = {
        "x": {"class": "C", "property": "gone", "column": "NO_SUCH_COL"},
    }
    classes = _classes({"C": {"cWeight": ["WGT"]}})

    spec = _resolve_canonical_designations(canonical, classes)["x"]
    assert spec["_resolution_status"] == "stale"
    assert "property_resolved" not in spec


def test_designation_without_property_refs_untouched():
    """클래스만 지정하는 항목(모집단 규칙 등)은 그대로 통과."""
    canonical = {
        "표면처리 모집단": {"class": "MaterialA", "note": "모집단은 MaterialA"},
    }
    classes = _classes({"MaterialA": {"materialAWeight": ["WGT"]}})

    spec = _resolve_canonical_designations(canonical, classes)["표면처리 모집단"]
    assert spec["_resolution_status"] == "no_property_refs"
    assert spec["note"] == "모집단은 MaterialA"


def test_empty_canonical_is_passthrough():
    """hints 가 없는 도메인에서 no-op (도메인 중립)."""
    assert _resolve_canonical_designations({}, {}) == {}


# ── 실제 리포 상태 검사 ───────────────────────────────────────────────


def test_live_hints_all_resolvable():
    """현재 리포의 hints 가 실제 딕셔너리로 전부 해석되는지.

    ``stale`` 이 하나라도 있으면 그 SME 규칙은 NL→SPARQL 엔진이 쓸 수 없다 —
    CQ01 오답(약 430 vs 약 430)의 원인이 정확히 이것이었다.
    """
    import config

    hints_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rules", "canonical_hints.json",
    )
    if not os.path.exists(hints_path):
        pytest.skip("canonical_hints.json 없음 (도메인 미설정)")
    if not os.path.exists(config.SEMANTIC_DICT_PATH):
        pytest.skip("시맨틱 딕셔너리 미생성")

    with open(hints_path, encoding="utf-8") as handle:
        canonical = json.load(handle).get("canonical_designations") or {}
    if not canonical:
        pytest.skip("canonical_designations 비어 있음")
    with open(config.SEMANTIC_DICT_PATH, encoding="utf-8") as handle:
        classes = json.load(handle).get("classes") or {}

    out = _resolve_canonical_designations(canonical, classes)
    stale = [
        intent for intent, spec in out.items()
        if isinstance(spec, dict) and spec.get("_resolution_status") == "stale"
    ]
    assert not stale, (
        "SME 집계 규칙이 존재하지 않는 DP 를 가리켜 사용 불가 상태다. "
        f"hints 의 *_column 값을 확인하라: {stale}"
    )
