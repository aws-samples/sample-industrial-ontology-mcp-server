"""딕셔너리가 집계 질의 작성자를 오답으로 이끌지 않는지 검사.

**배경 (2026-07-26 실측 3건 오답)**: 딕셔너리와 T-Box 만 보고 CQ SPARQL 을 작성했을 때
문법은 맞았지만 값이 틀렸다. 원인이 전부 **딕셔너리 구조** 에 있었다.

| CQ | 오답 | 정답 | 원인 |
|----|------|------|------|
| CQ01 | 약 430건 | 정답 | 필수 필터(산출물 중량 != 0) 누락 |
| CQ03 | 정답의 약 1/5 | 정답 | 모집단 클래스 오선택 (MaterialADetail → MaterialA) |

세 가지 구조 문제:

1. **규칙이 마지막에 숨어 있다** — ``canonical_designations`` 가 10번째(끝) 섹션이고,
   앞의 ``sparql_guide`` / ``common_mistakes`` 가 전혀 가리키지 않는다. 앞에서부터
   읽으면 SPARQL **문법** 규칙만 보고 질의를 쓰게 된다.
2. **규칙이 옛 DP 이름을 가리킨다** — 별도 테스트
   (``test_canonical_designation_resolution.py``) 가 담당.
3. **클래스 선택 근거가 없다** — 딕셔너리는 클래스별로만 나열돼 "이 컬럼이 어느
   클래스에 있나" 를 역방향으로 못 찾는다. 그래서 클래스를 먼저 고르고 그 안을
   뒤지는 순서가 되고, 고른 클래스에 컬럼이 없으면 불필요한 조인을 하게 된다.
"""
from __future__ import annotations

import json
import os

import pytest

from tools.semantic_dictionary import (
    _build_column_to_classes_index,
    _build_sparql_guide,
)


def _classes(spec: dict) -> dict:
    return {
        cls: {"datatype_properties": {
            dp: ({"source_columns": v} if isinstance(v, list) else v)
            for dp, v in dps.items()
        }}
        for cls, dps in spec.items()
    }


# ── 컬럼 → 클래스 역색인 ───────────────────────────────────────────────


def test_shared_column_lists_every_owner():
    """여러 클래스가 같은 컬럼을 가지면 전부 나열한다 — 선택 근거가 된다."""
    classes = _classes({
        "MaterialA":       {"materialACode": ["CODE_COL_1"]},
        "MaterialADetail": {"detailCode": ["CODE_COL_1"]},
    })
    idx = _build_column_to_classes_index(classes)
    assert idx["CODE_COL_1"] == ["MaterialA.materialACode", "MaterialADetail.detailCode"]


def test_single_owner_column_is_indexed():
    """한 클래스만 가진 컬럼도 색인한다 (2026-07-28 정책 변경).

    이전에는 "선택 고민이 없으니 빼서 크기를 절약" 했다. 그런데 역색인의 실제 용도는
    선택 고민 해소가 아니라 **"이 컬럼이 KG 에 있는가, 있으면 어디에"** 조회다.
    필터링 결과 2,034 컬럼 중 633 개만 남아, 처리량 집계의 필수 제외 필터
    (``OUTPUT_WGT_COL``) 와 처리량 본체 (``PROC_QTY_COL_A``), 스카핑 실시 판정
    (``SURFACE_OP_DT``) 이 **전부 단일소유라 조회 불가** 였다. 역색인에서 못 찾으면
    "KG 에 없다" 고 결론내고 엉뚱한 대체 컬럼을 쓰게 된다 — 빼서 아끼려던 크기보다
    오답 비용이 크다.
    """
    classes = _classes({"MaterialA": {"materialAOnly": ["ONLY_HERE"]}})
    idx = _build_column_to_classes_index(classes)
    assert idx["ONLY_HERE"] == ["MaterialA.materialAOnly"]


def test_empty_dp_is_marked():
    """값이 없는 DP 는 표시한다 — 질의에 쓰면 0건이 나온다."""
    classes = {
        "MaterialA": {"datatype_properties": {
            "materialAW": {"source_columns": ["WGT"], "is_populated": True},
        }},
        "MaterialB": {"datatype_properties": {
            "materialAW": {"source_columns": ["WGT"], "is_populated": False},
        }},
    }
    idx = _build_column_to_classes_index(classes)
    owners = idx["WGT"]
    assert "MaterialA.materialAW" in owners
    assert any("MaterialB.materialAW" in o and "∅" in o for o in owners)


def test_index_reveals_single_class_holding_all_columns():
    """CQ03 회귀 — 필요한 컬럼 전부를 값과 함께 가진 클래스를 식별할 수 있어야 한다.

    실측: 표면처리 집계에 CODE_COL_1 / WEIGHT_COL_1 / DATE_COL_2 가
    필요했고 MaterialA 하나가 셋을 다 가졌는데, 역색인이 없어 MaterialADetail 을 골라
    조인했다 (정답의 약 1/5).
    """
    classes = {
        "MaterialA": {"datatype_properties": {
            "a": {"source_columns": ["CODE_COL_1"], "is_populated": True},
            "b": {"source_columns": ["WEIGHT_COL_1"], "is_populated": True},
            "c": {"source_columns": ["DATE_COL_2"], "is_populated": True},
        }},
        "MaterialADetail": {"datatype_properties": {
            "d": {"source_columns": ["CODE_COL_1"], "is_populated": True},
            "e": {"source_columns": ["DATE_COL_2"], "is_populated": True},
        }},
        "MaterialB": {"datatype_properties": {
            "f": {"source_columns": ["WEIGHT_COL_1"], "is_populated": False},
        }},
    }
    idx = _build_column_to_classes_index(classes)
    needed = ["CODE_COL_1", "WEIGHT_COL_1", "DATE_COL_2"]

    # 각 컬럼을 '값이 있는' 상태로 가진 클래스 집합의 교집합
    sets = []
    for col in needed:
        sets.append({
            o.split(".", 1)[0] for o in idx.get(col, []) if "∅" not in o
        })
    common = set.intersection(*sets)
    assert common == {"MaterialA"}, (
        f"세 컬럼을 모두 가진 클래스는 MaterialA 하나여야 한다 — 실제 {common}"
    )


# ── sparql_guide 의 MUST_READ_FIRST 포인터 ────────────────────────────


def test_guide_points_to_canonical_designations():
    """canonical 규칙이 있으면 가이드가 그것을 먼저 읽으라고 지시한다."""
    from domain.tbox_utils import _new_graph

    canonical = {"압연 소재처리량 측정": {"class": "ProcessResultD"}}
    guide, _ = _build_sparql_guide({}, _new_graph(), [], canonical)

    assert "MUST_READ_FIRST" in guide, (
        "sparql_guide 는 SPARQL 문법만 담으므로, 도메인 집계 규칙으로 가는 "
        "포인터가 없으면 읽는 사람이 필수 필터/모집단을 놓친다"
    )
    block = guide["MUST_READ_FIRST"]
    assert "압연 소재처리량 측정" in block["canonical_designations_intents"]
    assert "canonical_designations" in block["description"]


def test_guide_omits_pointer_when_no_canonical():
    """canonical 이 없는 도메인에서는 포인터를 넣지 않는다 (도메인 중립)."""
    from domain.tbox_utils import _new_graph

    guide, _ = _build_sparql_guide({}, _new_graph(), [], None)
    assert "MUST_READ_FIRST" not in guide


# ── 실제 리포 딕셔너리 검사 ───────────────────────────────────────────


def test_live_dictionary_has_query_guidance():
    """현재 딕셔너리에 두 안내 장치가 모두 들어 있는지.

    딕셔너리 재생성이 필요한 상태면 skip — 기능 자체는 위 단위 테스트가 검증한다.
    """
    import config

    if not os.path.exists(config.SEMANTIC_DICT_PATH):
        pytest.skip("시맨틱 딕셔너리 미생성")
    with open(config.SEMANTIC_DICT_PATH, encoding="utf-8") as handle:
        d = json.load(handle)

    if not d.get("canonical_designations"):
        pytest.skip("canonical_designations 없는 도메인")

    missing = [
        key for key in ("column_to_classes",)
        if key not in d
    ]
    if "MUST_READ_FIRST" not in (d.get("sparql_guide") or {}):
        missing.append("sparql_guide.MUST_READ_FIRST")
    if missing:
        pytest.skip(
            f"딕셔너리가 신규 안내 장치 이전 버전이다 (재생성 필요): {missing}"
        )

    # 있으면 내용까지 확인
    assert d["column_to_classes"], "공유 컬럼 색인이 비어 있다"


def test_guide_intent_list_excludes_comment_keys():
    """intent 목록에 주석/메타 키(_comment 등)를 넣지 않는다.

    hints 파일은 밑줄 키로 변경 이력·주의사항을 담는데, 그것이 "읽어야 할 규칙"
    목록에 섞이면 실제 규칙이 무엇인지 흐려진다.
    """
    from domain.tbox_utils import _new_graph

    canonical = {
        "_comment": "이건 주석",
        "_dp_name_sync_2026-07-26": "이것도 메타",
        "압연 소재처리량 측정": {"class": "ProcessResultD"},
    }
    guide, _ = _build_sparql_guide({}, _new_graph(), [], canonical)
    intents = guide["MUST_READ_FIRST"]["canonical_designations_intents"]
    assert intents == ["압연 소재처리량 측정"]
