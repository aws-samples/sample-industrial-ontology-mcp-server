"""``rules/`` 를 카테고리 하위 폴더로 나눈 뒤에도 모든 설정이 로드된다.

2026-08-23: ``rules/`` 가 3개월간 3개 → 26개 파일로 늘고, 성격이 다른 세 종류
(도메인 자산 / 엔진 정책 / 도메인-중립 계약) 가 한 폴더에 섞였다. 파일명만 보고
"새 도메인에서 이걸 교체해야 하는가" 를 알 수 없었다.

## 이 테스트가 지키는 것 — 조용한 빈 폴백

경로를 만드는 지점이 **129곳** 이었고 모듈마다 자기 ``_RULES_DIR`` 을 따로 계산했다.
하위 폴더로 옮기면서 한 곳이라도 놓치면 ``FileNotFoundError`` → ``except`` → **빈
dict 폴백** 이 되고, 그러면 게이트가 **꺼진 채 통과** 한다 (이 리포에서 반복된 실패
모드인 "선언만 채점하고 실제 효과를 보지 않는 게이트" 계열).

그래서 여기서는 "함수가 예외를 안 낸다" 가 아니라 **실제 내용이 실려 오는가** 를
주장한다 — 빈 결과는 실패로 본다.

## 특히 glob 계열

``glob(rules/*.json)`` 은 하위 폴더로 나눈 순간 **0건** 이 된다. 두 스텝
(step_22e 중복 OP 정리 / step_22f OP 근거 게이트) 이 "설정이 이 OP 를 언급하는가" 를
그 목록으로 판정하는데, 0건은 "아무 설정도 안 쓴다" 로 읽혀 **SME 가 선언한 OP 를
근거 없음으로 지우거나 감점** 한다. 예외도 로그도 남지 않는다.
"""
from __future__ import annotations

import json
import os

import pytest

from domain.rules_paths import (
    CONTRACT_DIR,
    DOMAIN_DIR,
    POLICY_DIR,
    REPLACE_ON_NEW_DOMAIN,
    RULES_ROOT,
    category_of,
    rules_json_glob,
    rules_path,
    rules_subdir,
)

# 이 리포에 실제로 있어야 하는 파일 (부재면 파이프라인이 조용히 열화된다).
_PRESENT = {
    "domain_config.json": DOMAIN_DIR,
    "table_class_mapping.json": DOMAIN_DIR,
    "disjoint_groups.json": DOMAIN_DIR,
    "design_patterns.json": DOMAIN_DIR,
    "abstract_group_hints.json": DOMAIN_DIR,
    "ontoclean_labels.json": DOMAIN_DIR,
    "property_chains.json": DOMAIN_DIR,
    "tacit_rules.json": DOMAIN_DIR,
    "entailment_golden.json": DOMAIN_DIR,
    "table_labels.json": DOMAIN_DIR,
    "anomaly_hints.json": DOMAIN_DIR,
    "tbox_manual_additions.ttl": DOMAIN_DIR,
    "quality_thresholds.json": POLICY_DIR,
    "tbox_shapes.ttl": POLICY_DIR,
    "tbox_generation_config.json": POLICY_DIR,
    "op_name_hints.json": POLICY_DIR,
    "common_dp.json": CONTRACT_DIR,
    "fk_patterns.json": CONTRACT_DIR,
    "value_heuristics.json": CONTRACT_DIR,
    "value_normalizations.json": CONTRACT_DIR,
    "value_ranges.json": CONTRACT_DIR,
}


# ──────────────────────────────────────────────────────────────────
# 1. 해석기 자체
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("filename", "category"), sorted(_PRESENT.items()))
def test_known_file_resolves_and_exists(filename: str, category: str):
    """21개 설정 파일이 모두 해석되고 실제로 존재한다."""
    path = rules_path(filename)
    assert os.path.exists(path), f"{filename} 이 해석 경로에 없다: {path}"
    assert category_of(filename) == category
    assert os.path.join(category, filename) in path


def test_unknown_file_defaults_to_domain():
    """표에 없는 파일은 domain 으로 본다 — 오분류 대가가 비대칭이기 때문.

    도메인 자산을 policy 로 두면 이식 시 교체 대상에서 빠져 **조용히 오염** 되고,
    반대는 불필요한 교체 안내뿐이다.
    """
    assert category_of("brand_new_thing.json") == DOMAIN_DIR


def test_basename_is_used_even_if_path_given():
    a = rules_path("domain_config.json")
    b = rules_path("some/stale/prefix/domain_config.json")
    assert a == b


def test_absent_file_returns_categorized_path():
    """없는 파일은 **새 구조 경로** 를 반환해야 한다.

    호출부의 ``os.path.exists`` 폴백이 그대로 동작하고, 생성하는 쪽은 새 구조에
    쓴다. ``None`` 을 반환하면 호출부가 ``os.path.join(None, …)`` 로 죽는다.
    """
    path = rules_path("definitely_absent_xyz.json")
    assert isinstance(path, str) and path
    assert not os.path.exists(path)
    assert DOMAIN_DIR in path


def test_legacy_flat_location_still_wins(tmp_path):
    """루트 직하에 파일이 있으면 그것을 쓴다 (하위 호환 / 평평한 tmp_path 주입)."""
    flat = tmp_path / "domain_config.json"
    flat.write_text("{}", encoding="utf-8")
    assert rules_path("domain_config.json", base=str(tmp_path)) == str(flat)


def test_categorized_location_preferred_over_flat(tmp_path):
    """둘 다 있으면 새 구조가 이긴다 — 마이그레이션 후 잔재를 따라가지 않는다."""
    (tmp_path / DOMAIN_DIR).mkdir()
    categorized = tmp_path / DOMAIN_DIR / "domain_config.json"
    categorized.write_text("{}", encoding="utf-8")
    (tmp_path / "domain_config.json").write_text("{}", encoding="utf-8")
    assert rules_path("domain_config.json", base=str(tmp_path)) == str(categorized)


def test_rules_subdir_handles_non_category_dirs():
    """``mutations`` / ``swrl`` 같은 기존 하위 폴더도 받는다."""
    assert rules_subdir("mutations") == os.path.join(RULES_ROOT, "mutations")


# ──────────────────────────────────────────────────────────────────
# 2. glob — 하위 폴더를 포함해야 한다 (게이트가 조용히 꺼지는 자리)
# ──────────────────────────────────────────────────────────────────

def test_json_glob_finds_files_in_subdirectories():
    """``rules/*.json`` 은 0건이 된다. 해석기는 하위 폴더를 훑어야 한다."""
    found = rules_json_glob()
    assert len(found) >= 15, f"설정 JSON 을 {len(found)}개만 찾았다 — glob 이 깨졌다"
    names = {os.path.basename(p) for p in found}
    # 세 카테고리가 모두 대표된다 (한 폴더만 훑는 회귀를 잡는다)
    assert "domain_config.json" in names
    assert "quality_thresholds.json" in names
    assert "fk_patterns.json" in names


def test_json_glob_is_deterministic():
    assert rules_json_glob() == sorted(rules_json_glob())


def test_json_glob_includes_flat_files_too(tmp_path):
    (tmp_path / POLICY_DIR).mkdir()
    (tmp_path / POLICY_DIR / "nested.json").write_text("{}", encoding="utf-8")
    (tmp_path / "flat.json").write_text("{}", encoding="utf-8")
    names = {os.path.basename(p) for p in rules_json_glob(base=str(tmp_path))}
    assert names == {"nested.json", "flat.json"}


# ──────────────────────────────────────────────────────────────────
# 3. 실제 로더가 **내용** 을 싣고 온다 (빈 폴백이 아니다)
# ──────────────────────────────────────────────────────────────────
# 여기가 이 파일의 핵심이다. 경로 함수가 문자열을 잘 만드는 것과, 129곳의 호출부가
# 그 함수를 **실제로 쓰는** 것은 다른 문제다. 함수 경계 아래의 배선은 산출물로만
# 확인된다.

def test_domain_config_loaded_with_content():
    from domain.namespaces import DOMAIN_CONFIG
    assert DOMAIN_CONFIG.get("namespace", {}).get("prefix")


def test_disjoint_groups_non_empty():
    from tools.ontology_quality import _load_disjoint_config
    assert _load_disjoint_config().get("groups")


def test_fk_patterns_non_empty():
    from tools.ontology_quality import _load_fk_patterns
    assert _load_fk_patterns()


def test_table_class_mapping_non_empty():
    from domain.table_mapping import load_table_class_mapping
    assert load_table_class_mapping()


def test_abstract_group_hints_non_empty():
    from tools.tbox_generation import _load_abstract_group_hints
    assert _load_abstract_group_hints()


def test_op_name_hints_non_empty():
    from tools.tbox_generation import _load_op_name_hints
    assert _load_op_name_hints()


def test_property_chains_non_empty():
    from tools.ontology_quality import _load_property_chains
    assert _load_property_chains()


def test_ontoclean_labels_non_empty():
    """이 로더는 ``class_labels`` 섹션을 **이미 벗겨서** 반환한다."""
    from tools.ontology_quality import _load_ontoclean_labels
    labels = _load_ontoclean_labels()
    assert labels and all(isinstance(v, dict) for v in labels.values())


def test_quality_thresholds_loaded_not_defaults():
    """파일을 못 찾으면 ``_DEFAULTS`` 로 조용히 폴백한다 — 그 구분을 주장한다.

    ``_load()`` 는 ``_DEFAULTS`` 에 있는 섹션만 병합하므로 ``score_formula``
    (tbox_metrics 가 직접 읽는 섹션) 는 파일에서 따로 확인한다.
    """
    from tools.validation_support.thresholds import _THRESHOLDS_PATH, _load
    assert os.path.exists(_THRESHOLDS_PATH), _THRESHOLDS_PATH
    with open(_THRESHOLDS_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)
    assert raw.get("score_formula"), "score_formula 섹션이 비었다"
    # 파일의 값이 실제로 병합 결과에 반영된다 (defaults 폴백이 아니다).
    file_tiers = set(raw.get("class_tiers", {}))
    assert file_tiers and file_tiers <= set(_load()["class_tiers"])


def test_abox_required_contracts_non_empty():
    """``required=True`` 로 로드하는 계약 3개 — 빈 dict 면 A-Box 값 처리가 무력화."""
    import tools.abox_generation as ab
    assert ab._VALUE_HEURISTICS.get("numeric_keywords")
    assert ab._COMMON_DP_CFG.get("common_datatype_properties")
    assert ab._VALUE_NORMALIZE_CFG.get("enum_synonyms")


def test_tbox_shapes_resolves_for_shacl():
    """SHACL 정적 shapes — 못 찾으면 S4 가 위반 0건으로 조용히 통과한다."""
    from tools.validation_core import RULES_DIR
    path = rules_path("tbox_shapes.ttl", base=str(RULES_DIR))
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_manual_additions_patch_resolves():
    """step_30 이 못 찾으면 수동 추가분이 통째로 유실된다 (조용히)."""
    from tools.quality_steps.step_30_tbox_manual_additions import _patch_path
    assert os.path.exists(_patch_path())
    assert os.path.getsize(_patch_path()) > 0


def test_entailment_golden_resolves():
    import tools.entailment_regression as er
    assert os.path.exists(er._GOLDEN_PATH)


def test_tacit_rules_resolves():
    import tools.tacit_rules as tr
    assert os.path.exists(tr._RULES_PATH)


def test_validate_rules_sees_real_files():
    """``validate_rules`` 가 빈 dict 를 검사하면 **위반 0건** 으로 통과한다.

    빈 입력은 스키마 위반이 없으므로, 파일을 못 찾은 것과 파일이 정상인 것이
    똑같이 "이슈 없음" 으로 보인다. 그래서 로더 쪽에서 내용을 확인한다.
    """
    from tools.validate_rules import _load
    assert _load("common_dp.json").get("common_datatype_properties")
    assert _load("fk_patterns.json").get("patterns")


def test_op_grounding_gate_sees_config_names():
    """step_22f 의 G3 근거 — 0건이면 근거 있는 OP 가 무근거로 집계된다."""
    from tools.quality_steps.step_22f_op_grounding_gate import _config_mentioned_names
    assert len(_config_mentioned_names()) > 50


def test_duplicate_op_prune_sees_config_references(tmp_path):
    """step_22e 의 외부 참조 — 0건이면 SME 선언 OP 를 지운다."""
    from tools.quality_steps.step_22e_duplicate_op_prune import _external_references
    # 설정에 실제로 적힌 이름을 하나 골라 되찾을 수 있는지 본다.
    with open(rules_path("fk_patterns.json"), encoding="utf-8") as handle:
        fk = json.load(handle)
    sample = next(iter(fk.get("patterns", {})), None)
    if not sample:
        pytest.skip("fk_patterns.patterns 가 비어 있다")
    assert _external_references({sample}) == {sample}


# ──────────────────────────────────────────────────────────────────
# 4. 이식 계약
# ──────────────────────────────────────────────────────────────────

def test_replace_on_new_domain_is_all_domain_assets():
    """교체 목록은 domain 카테고리의 **필수** 파일과 일치한다."""
    assert REPLACE_ON_NEW_DOMAIN
    for f in REPLACE_ON_NEW_DOMAIN:
        assert category_of(f) == DOMAIN_DIR, f
        assert os.path.exists(rules_path(f)), f"{f} 가 없다"


def test_replace_list_covers_the_previously_missed_file():
    """``abstract_group_hints.json`` 이 목록에 있다.

    이 파일의 ``child_name_patterns`` 는 ``"Monitoring"`` / ``"Energy"`` 처럼
    **일반 영어 단어** 라 철강 파일이 새 도메인에 남으면 조용히 오염시킨다.
    그런데 ``initialize_domain_rules`` 의 12개 목록에 없어서 이식 시 아무도
    교체하지 않았다.
    """
    assert "abstract_group_hints.json" in REPLACE_ON_NEW_DOMAIN


def test_policy_files_are_not_in_replace_list():
    """엔진 정책은 도메인과 무관하다 — 교체를 권하면 지표 기준이 흔들린다."""
    for f in ("quality_thresholds.json", "tbox_shapes.ttl", "op_name_hints.json"):
        assert f not in REPLACE_ON_NEW_DOMAIN, f
