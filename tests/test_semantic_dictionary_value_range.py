"""D4 — DP value_stats / value_range_check / filter_out_of_range 통합 테스트.

generate_semantic_dictionary 실행 후 디스크에 써진 JSON 을 다시 로드해 확인
(in-memory dict 확인은 A4 교훈 — return JSON 에 classes/sparql_guide 없음).
"""
import json
import os

import pytest


def _skip_if_no_tbox():
    from config import TBOX_PATH
    if not os.path.exists(TBOX_PATH):
        pytest.skip("T-Box 파일 없음")


def test_generated_dict_has_value_stats():
    """최소 한 DP 엔트리가 value_stats 필드 포함."""
    _skip_if_no_tbox()
    from tools.semantic_dictionary import generate_semantic_dictionary

    result = json.loads(generate_semantic_dictionary())
    assert result["success"] is True
    dict_path = result["path"]

    with open(dict_path, encoding="utf-8") as f:
        full_dict = json.load(f)
    classes = full_dict.get("classes", {})

    found = False
    for cls_info in classes.values():
        for dp_info in cls_info.get("datatype_properties", {}).values():
            if "value_stats" in dp_info:
                vs = dp_info["value_stats"]
                assert "count" in vs, "value_stats 는 count 포함 필수"
                found = True
                break
        if found:
            break
    assert found, "적어도 한 DP 는 value_stats 를 가져야 함"


def test_common_patterns_has_value_range_check():
    """Numeric DP (p10/p90) 가 있으면 value_range_check 자동 생성."""
    _skip_if_no_tbox()
    from tools.semantic_dictionary import generate_semantic_dictionary

    result = json.loads(generate_semantic_dictionary())
    dict_path = result["path"]
    with open(dict_path, encoding="utf-8") as f:
        full_dict = json.load(f)

    classes = full_dict.get("classes", {})
    has_numeric_dp = any(
        "p10" in dp.get("value_stats", {}) and "p90" in dp.get("value_stats", {})
        for cls in classes.values()
        for dp in cls.get("datatype_properties", {}).values()
    )
    if has_numeric_dp:
        patterns = full_dict.get("sparql_guide", {}).get("common_patterns", {})
        assert "value_range_check" in patterns, (
            "numeric DP 있으면 value_range_check 패턴 생성돼야 함"
        )
        vrc = patterns["value_range_check"]
        assert "sparql" in vrc
        assert "note" in vrc
        assert "FILTER" in vrc["sparql"]


def test_anti_patterns_has_filter_out_of_range():
    """filter_out_of_range 안티패턴 항상 포함 (정적 항목)."""
    _skip_if_no_tbox()
    from tools.semantic_dictionary import generate_semantic_dictionary

    result = json.loads(generate_semantic_dictionary())
    dict_path = result["path"]
    with open(dict_path, encoding="utf-8") as f:
        full_dict = json.load(f)

    anti = full_dict.get("sparql_guide", {}).get("anti_patterns", {})
    assert "filter_out_of_range" in anti
    msg = anti["filter_out_of_range"]
    # value_stats 안내 문구 포함
    assert "value_stats" in msg
    assert "out of range" in msg.lower() or "범위" in msg
