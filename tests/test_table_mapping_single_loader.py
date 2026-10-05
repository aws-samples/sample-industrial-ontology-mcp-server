"""``rules/domain/table_class_mapping.json`` 해석이 소비자 전체에서 일치하는지 회귀 가드.

배경 (2026-08-08 실측): 10개 모듈이 이 파일을 각자 파싱했고, **같은 입력에 서로
다른 답** 을 냈다:

    입력: {"T_ALPHA": "steel:Alpha", "T_BETA": "Beta",
           "T_GAMMA": "other:Gamma", "T_DELTA": {"class": "steel:Delta"}}

    abox_generation      → T_DELTA 소실, T_GAMMA = 'other:Gamma'  (콜론 포함!)
    competency_questions → 4개 모두 정상
    temporal_cardinality → T_DELTA 소실

세 결과가 다르면 S7 (A-Box 생성) 과 S9/S12 (검증·질의) 가 **다른 클래스 이름을
보고 있다** 는 뜻이다. 중첩 dict 를 못 읽는 쪽은 그 테이블을 통째로 잃고, 배포
prefix 만 벗기는 쪽은 존재하지 않는 클래스 이름 (``other:Gamma``) 을 만들어 이후
조회를 전부 실패시킨다.

파싱은 이제 ``domain.table_mapping`` 하나가 담당한다. 이 파일은 (1) 정본 파서의
계약과 (2) 소비자들이 그 계약에 실제로 합의하는지를 함께 고정한다.
"""

from __future__ import annotations

import json

import pytest

from domain.table_mapping import (
    invalidate,
    load_class_prop_mapping,
    load_table_class_mapping,
    load_table_pk_columns,
    mapped_class_names,
    strip_prefix,
)

#: 실제 배포에서 관측된 네 가지 표기 형태를 한 번에 담은 입력.
_MIXED_FORMS = {
    "table_class_mapping": {
        "T_ALPHA": "steel:Alpha",  # 배포 prefix
        "T_BETA": "Beta",  # prefix 없음
        "T_GAMMA": "other:Gamma",  # 다른 prefix
        "T_DELTA": {"class": "steel:Delta"},  # 중첩 dict
        "T_EPS": "http://x.org/ns#Eps",  # 완전 IRI (# 구분자)
        "T_ZETA": "http://x.org/ns/Zeta",  # 완전 IRI (/ 구분자)
    },
    "table_pk_columns": {"T_ALPHA": ["ID_COL_1"], "T_BETA": "SINGLE_COL"},
    "class_prop_mapping": {"Alpha": {"col": "dp"}},
}

_EXPECTED = {
    "T_ALPHA": "Alpha",
    "T_BETA": "Beta",
    "T_GAMMA": "Gamma",
    "T_DELTA": "Delta",
    "T_EPS": "Eps",
    "T_ZETA": "Zeta",
}


@pytest.fixture
def rules_dir(tmp_path):
    """혼합 표기 매핑 파일을 담은 임시 rules 디렉토리."""
    path = tmp_path / "table_class_mapping.json"
    path.write_text(json.dumps(_MIXED_FORMS), encoding="utf-8")
    invalidate()
    yield str(tmp_path)
    invalidate()


def test_all_prefix_forms_resolve_to_local_name(rules_dir):
    """THE REGRESSION: 어떤 표기든 로컬 이름으로 해석되고 아무것도 사라지지 않는다."""
    assert load_table_class_mapping(rules_dir) == _EXPECTED


def test_nested_dict_form_is_not_dropped(rules_dir):
    """중첩 dict 를 무시하면 그 테이블이 조용히 사라진다 (사본 2개가 그랬다)."""
    assert load_table_class_mapping(rules_dir).get("T_DELTA") == "Delta"


def test_foreign_prefix_is_stripped_not_kept(rules_dir):
    """배포 prefix 만 벗기면 ``other:Gamma`` 가 클래스 이름이 되어 조회가 전부 깨진다."""
    resolved = load_table_class_mapping(rules_dir)["T_GAMMA"]
    assert ":" not in resolved and resolved == "Gamma"


def test_consumers_agree_with_canonical_parser(rules_dir, monkeypatch):
    """소비자들이 정본 파서와 **같은 답** 을 낸다 — 원래 결함의 핵심."""
    import tools.abox_generation as ab
    import tools.validation_support.checks.temporal_cardinality as tc

    monkeypatch.setattr(ab, "_RULES_DIR", rules_dir)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)
    monkeypatch.setattr(tc, "_rules_dir", lambda: rules_dir)
    tc.invalidate_csv_caches()

    canonical = load_table_class_mapping(rules_dir)
    assert ab._load_table_class_mapping() == canonical, "A-Box 생성기가 다른 답을 낸다"
    assert tc._table_class_map() == canonical, "validate_kg 체크가 다른 답을 낸다"


def test_pk_columns_normalise_scalar_to_list(rules_dir):
    """단일 문자열 PK 도 리스트로 정규화된다 (호출부는 항상 리스트를 기대한다)."""
    assert load_table_pk_columns(rules_dir) == {
        "T_ALPHA": ["ID_COL_1"],
        "T_BETA": ["SINGLE_COL"],
    }


def test_class_prop_and_mapped_names(rules_dir):
    assert load_class_prop_mapping(rules_dir) == {"Alpha": {"col": "dp"}}
    assert mapped_class_names(rules_dir) == set(_EXPECTED.values())


def test_missing_file_is_empty_not_raising(tmp_path):
    """파일이 없으면 빈 dict — 매핑은 힌트이지 필수 입력이 아니다."""
    invalidate()
    assert load_table_class_mapping(str(tmp_path)) == {}
    assert load_table_pk_columns(str(tmp_path)) == {}


def test_corrupt_file_is_absorbed(tmp_path):
    """손상된 JSON 도 예외를 올리지 않는다 (상위 blanket handler 가 hint 를 잃는다)."""
    (tmp_path / "table_class_mapping.json").write_text("{not json", encoding="utf-8")
    invalidate()
    assert load_table_class_mapping(str(tmp_path)) == {}


def test_cache_invalidates_on_file_change(tmp_path):
    """운영 중 매핑을 고치면 다음 호출에 반영된다 (영구 캐시가 stale 판정을 만들었다)."""
    import os
    import time

    path = tmp_path / "table_class_mapping.json"
    path.write_text(json.dumps({"table_class_mapping": {"T": "steel:Before"}}), encoding="utf-8")
    invalidate()
    assert load_table_class_mapping(str(tmp_path))["T"] == "Before"

    path.write_text(json.dumps({"table_class_mapping": {"T": "steel:After"}}), encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))
    assert load_table_class_mapping(str(tmp_path))["T"] == "After"


def test_flat_dict_without_wrapper_key(tmp_path):
    """최상위 래핑 키가 없는 평평한 dict 도 관용한다."""
    (tmp_path / "table_class_mapping.json").write_text(json.dumps({"T_ONE": "steel:One"}), encoding="utf-8")
    invalidate()
    assert load_table_class_mapping(str(tmp_path)) == {"T_ONE": "One"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ns:Local", "Local"),
        ("http://x.org/o#Local", "Local"),
        ("http://x.org/o/Local", "Local"),
        ("Local", "Local"),
    ],
)
def test_strip_prefix_forms(raw, expected):
    assert strip_prefix(raw) == expected
