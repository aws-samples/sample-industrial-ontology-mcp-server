"""FK 이름 기반 fallback 제거 후 동작 테스트."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_fk_cache():
    """각 테스트 사이에 FK skeleton 캐시를 비워 mock 주입 격리."""
    from tools.abox_generation import _reset_fk_skeleton_cache
    _reset_fk_skeleton_cache()
    yield
    _reset_fk_skeleton_cache()


def _detect(column: str, obj_props: list[dict], available: set[str] | None = None):
    from tools.abox_generation import _detect_fk_property
    return _detect_fk_property("SomeSource", column, "VAL1", obj_props, available)


def test_domain_range_match_returns_verified_fk():
    """domain/range가 실제 매칭되면 기존 동작대로 정상 FK 트리플 반환."""
    # _fk_column_to_class 가 target 을 인식하게 하기 위해 패치.
    with patch("tools.abox_generation._fk_column_to_class", return_value="TargetMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses",
               side_effect=lambda c: {c}):
        obj_props = [{"name": "hasTarget",
                       "domain": "SomeSource",
                       "range": "TargetMaster",
                       "inverse": None}]
        results = _detect("target_id", obj_props)
    assert results  # 비어있지 않음
    # prop_name, target_uri 형태 (__unverified__ 태그 아님)
    assert results[0][0] == "hasTarget"
    assert "__unverified__" not in [r[0] for r in results if isinstance(r, tuple)]


def test_no_match_returns_unverified_tag_only():
    """domain/range 매칭 실패 + 이름 기반 feature 있어도 이제는 __unverified__ 만 반환."""
    with patch("tools.abox_generation._fk_column_to_class", return_value="TargetMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses",
               side_effect=lambda c: {c}):
        # 이전에는 이름 기반 fallback이 매칭했을 프로퍼티 (target이 이름에 포함)
        obj_props = [{"name": "hasTargetish",
                       "domain": "UnrelatedClass",  # ≠ SomeSource
                       "range": "OtherClass",       # ≠ TargetMaster
                       "inverse": None}]
        results = _detect("target_id", obj_props)
    assert len(results) == 1
    tag = results[0]
    assert tag[0] == "__unverified__"
    assert tag[3] == "TargetMaster"


def test_unknown_target_class_returns_empty():
    """_fk_column_to_class 가 None 이면 그냥 빈 결과."""
    with patch("tools.abox_generation._fk_column_to_class", return_value=None):
        assert _detect("mystery_col", []) == []


def test_target_class_missing_from_tbox_returns_empty():
    with patch("tools.abox_generation._fk_column_to_class", return_value="GhostMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=False):
        assert _detect("ghost_id", []) == []


def test_specific_domain_wins_over_thing():
    """여러 OP 후보 중 domain이 source_class 인 OP를 우선 선택 (owl:Thing 밀려남)."""
    with patch("tools.abox_generation._fk_column_to_class", return_value="ProductMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses",
               side_effect=lambda c: {c, "SomeParent"}):
        obj_props = [
            # Thing-domain OP first in list — rank must not bias by order.
            {"name": "hasProductGeneric",
             "domain": "Thing",
             "range": "ProductMaster",
             "inverse": None},
            # Specific-domain OP — must win.
            {"name": "hasProductSpecific",
             "domain": "SomeSource",
             "range": "ProductMaster",
             "inverse": None},
        ]
        results = _detect("product_id", obj_props)
    forward_props = [r[0] for r in results if r[0] not in ("__inverse__", "__unverified__")]
    assert forward_props == ["hasProductSpecific"]


def test_ancestor_domain_beats_thing_but_loses_to_exact():
    """직접=2 > 조상=1 > Thing=0 순서로 랭킹."""
    with patch("tools.abox_generation._fk_column_to_class", return_value="ProductMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses",
               side_effect=lambda c: {c, "SomeParent"} if c == "SomeSource" else {c}):
        obj_props = [
            {"name": "thingOP", "domain": "Thing",
             "range": "ProductMaster", "inverse": None},
            {"name": "ancestorOP", "domain": "SomeParent",
             "range": "ProductMaster", "inverse": None},
            {"name": "exactOP", "domain": "SomeSource",
             "range": "ProductMaster", "inverse": None},
        ]
        results = _detect("product_id", obj_props)
    forward_props = [r[0] for r in results if r[0] not in ("__inverse__", "__unverified__")]
    assert forward_props[0] == "exactOP"
