"""A-Box 생성 통합 테스트 — FK value 변형 자동 복원 시나리오.

CSV 에서 온 FK 값이 하이픈/언더스코어/대소문자 변형으로 master_data URI 와
문자 그대로 매칭되지 않을 때, 4단계 fallback 이 실제 master URI 로 복원하는지
end-to-end 로 확인한다.

구현 전략: _detect_fk_property 를 현실적인 setup (master_instance_uris +
build_master_value_index) 으로 호출하고, skeleton 은 _fk_column_to_class /
_class_exists_in_tbox mock 으로 격리. generate_abox 전체를 돌리는 것보다
빠르면서도 통합 경로 (resolve_fk_target → 카운터) 를 전부 실행.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.fk_matching import (
    build_master_value_index,
    get_and_clear_stats,
)


@pytest.fixture(autouse=True)
def _reset_caches_and_stats():
    """테스트 간 FK skeleton 캐시 + 단계 카운터 리셋."""
    from tools.abox_generation import _reset_fk_skeleton_cache
    _reset_fk_skeleton_cache()
    get_and_clear_stats()
    yield
    _reset_fk_skeleton_cache()
    get_and_clear_stats()


def _make_master_uris(steel_inst_base: str) -> set[str]:
    """Steel 네임스페이스 기반 테스트용 master URI set 생성."""
    return {
        f"{steel_inst_base}EquipmentMaster_EQ001",
        f"{steel_inst_base}EquipmentMaster_EQ002",
        f"{steel_inst_base}TagMaster_T001",
    }


def test_hyphen_variant_resolves_via_normalized_fallback():
    """CSV 에 EQ-001 있어도 master 의 EQ001 로 자동 복원."""
    from domain.namespaces import DOMAIN_INST_NS
    from tools.abox_generation import _detect_fk_property

    steel_inst_base = str(DOMAIN_INST_NS)
    master_uris = _make_master_uris(steel_inst_base)
    mv_index = build_master_value_index(master_uris)

    obj_props = [{
        "name": "hasEquipment",
        "domain": "ProcessBlastFurnace",
        "range": "EquipmentMaster",
        "inverse": None,
    }]

    with patch("tools.abox_generation._fk_column_to_class", return_value="EquipmentMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses", side_effect=lambda c: {c}):
        # CSV 값 "EQ-001" (하이픈) — master 에는 "EQ001" 만 존재
        results = _detect_fk_property(
            "ProcessBlastFurnace", "equipment_id", "EQ-001", obj_props,
            available_classes={"EquipmentMaster"},
            master_instance_uris=master_uris,
            master_value_index=mv_index,
            fuzzy_config={"normalized": True},
        )

    # normalized fallback 이 매칭에 성공해 results 는 정상 FK triple 형식
    assert results, "퍼지 매칭이 결과를 내야 한다"
    prop_name, resolved_uri = results[0]
    assert prop_name == "hasEquipment"
    # 실제 master URI (EQ001) 로 교체됐어야 함 — candidate (EQ-001) 이 아니라.
    assert str(resolved_uri) == f"{steel_inst_base}EquipmentMaster_EQ001", \
        f"normalized fallback 실패: got {resolved_uri}"

    # 단계 카운터에 normalized 히트가 기록됐는지 확인
    stats = get_and_clear_stats()
    assert stats["normalized"] == 1, f"normalized 카운터 미증가: {stats}"
    assert stats["exact"] == 0


def test_exact_match_skips_fuzzy_path():
    """깔끔한 값 (EQ001) 은 stage=exact 로 매칭, normalized 카운터 증가 없음."""
    from domain.namespaces import DOMAIN_INST_NS
    from tools.abox_generation import _detect_fk_property

    steel_inst_base = str(DOMAIN_INST_NS)
    master_uris = _make_master_uris(steel_inst_base)
    mv_index = build_master_value_index(master_uris)

    obj_props = [{
        "name": "hasEquipment",
        "domain": "SomeProcess",
        "range": "EquipmentMaster",
        "inverse": None,
    }]

    with patch("tools.abox_generation._fk_column_to_class", return_value="EquipmentMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses", side_effect=lambda c: {c}):
        results = _detect_fk_property(
            "SomeProcess", "equipment_id", "EQ001", obj_props,
            available_classes={"EquipmentMaster"},
            master_instance_uris=master_uris,
            master_value_index=mv_index,
            fuzzy_config={"normalized": True},
        )

    assert results
    stats = get_and_clear_stats()
    assert stats["exact"] == 1
    assert stats["normalized"] == 0


def test_unresolved_keeps_candidate_uri():
    """매칭 실패 시에도 fk_results 는 candidate URI 로 진행 (unverified 플로우로).

    이 경우 _apply_fk_results 가 downstream 에서 unverified 처리할 것이고,
    fk_match_stats.unresolved 카운터에 기록된다.
    """
    from domain.namespaces import DOMAIN_INST_NS
    from tools.abox_generation import _detect_fk_property

    steel_inst_base = str(DOMAIN_INST_NS)
    master_uris = _make_master_uris(steel_inst_base)
    mv_index = build_master_value_index(master_uris)

    obj_props = [{
        "name": "hasEquipment",
        "domain": "SomeProcess",
        "range": "EquipmentMaster",
        "inverse": None,
    }]

    with patch("tools.abox_generation._fk_column_to_class", return_value="EquipmentMaster"), \
         patch("tools.abox_generation._class_exists_in_tbox", return_value=True), \
         patch("tools.abox_generation._get_superclasses", side_effect=lambda c: {c}):
        # 완전히 새로운 값 — 어떤 단계도 매칭 못함
        results = _detect_fk_property(
            "SomeProcess", "equipment_id", "WHOLLY_NEW_999", obj_props,
            available_classes={"EquipmentMaster"},
            master_instance_uris=master_uris,
            master_value_index=mv_index,
            fuzzy_config={"normalized": True},
        )

    # results 는 여전히 반환 (candidate URI 사용)
    assert results
    stats = get_and_clear_stats()
    assert stats["unresolved"] == 1


def test_load_fuzzy_config_reads_section():
    """_load_fuzzy_config 가 domain_config.json 에서 섹션을 읽고,
    없는 키는 기본값 사용."""
    from tools.abox_generation import _load_fuzzy_config

    cfg = _load_fuzzy_config()
    # 철강 domain_config.json 은 normalized=true, 나머지 false 로 저장돼 있음
    assert isinstance(cfg, dict)
    assert set(cfg.keys()) == {"normalized", "levenshtein", "prefix"}
    assert cfg["normalized"] is True
    assert cfg["levenshtein"] is False
    assert cfg["prefix"] is False
