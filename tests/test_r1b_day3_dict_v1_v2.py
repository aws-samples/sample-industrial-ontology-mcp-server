"""기능 단계 — generate_semantic_dictionary(include_stats=...) 분리 검증.

v1 (include_stats=False): T-Box 구조만, A-Box 통계 없음. A-Box 생성 전
단계에서 vocabulary contract 로 사용.
v2 (include_stats=True, default): T-Box 구조 + A-Box 통계. LLM NL→SPARQL
레퍼런스.

v1 은 v2 의 구조적 부분집합. 같은 파일 (SEMANTIC_DICT_PATH) 덮어쓰기.
"""
from __future__ import annotations

import json
import os

import pytest


def _dict_json() -> dict | None:
    """현재 SEMANTIC_DICT_PATH 의 딕셔너리 로드 (없으면 None)."""
    from config import SEMANTIC_DICT_PATH
    if not os.path.exists(SEMANTIC_DICT_PATH):
        return None
    with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
        return json.load(f)


def _require_tbox():
    """T-Box 없으면 skip — integration test."""
    from config import TBOX_PATH
    if not os.path.exists(TBOX_PATH):
        pytest.skip(f"T-Box 없음 ({TBOX_PATH}) — generate_tbox 먼저 필요")


def test_include_stats_true_produces_v2(tmp_path, monkeypatch):
    """include_stats=True (default) 시 version='v2' + stats_included=True."""
    _require_tbox()
    from tools import semantic_dictionary as sd
    # 임시 경로로 격리
    sd_path = tmp_path / "dict_v2.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_path))
    result = json.loads(sd.generate_semantic_dictionary(include_stats=True))
    assert result["success"] is True
    assert result["version"] == "v2"
    # 파일 저장
    assert sd_path.exists()
    d = json.loads(sd_path.read_text(encoding="utf-8"))
    assert d["metadata"]["stats_included"] is True
    assert d["metadata"]["dictionary_stage"] == "v2_full"


def test_include_stats_false_produces_v1(tmp_path, monkeypatch):
    """include_stats=False 시 version='v1' + stats_included=False."""
    _require_tbox()
    from tools import semantic_dictionary as sd
    sd_path = tmp_path / "dict_v1.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_path))
    result = json.loads(sd.generate_semantic_dictionary(include_stats=False))
    assert result["success"] is True
    assert result["version"] == "v1"
    d = json.loads(sd_path.read_text(encoding="utf-8"))
    assert d["metadata"]["stats_included"] is False
    assert d["metadata"]["dictionary_stage"] == "v1_vocabulary_contract"


def test_v1_has_no_instance_count_in_abox_stats(tmp_path, monkeypatch):
    """v1 은 A-Box 통계 안 함 — abox_triples 가 0."""
    _require_tbox()
    from tools import semantic_dictionary as sd
    sd_path = tmp_path / "dict_v1.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_path))
    sd.generate_semantic_dictionary(include_stats=False)
    d = json.loads(sd_path.read_text(encoding="utf-8"))
    # A-Box 로드 skip → abox_triples 0
    assert d["metadata"]["abox_triples"] == 0


def test_v2_includes_abox_triples(tmp_path, monkeypatch):
    """v2 는 A-Box 로드 → abox_triples > 0 (A-Box 가 존재하면)."""
    _require_tbox()
    from config import ABOX_PATH
    from tools import semantic_dictionary as sd
    if not os.path.exists(ABOX_PATH):
        pytest.skip("A-Box 없음 — v2 통계 테스트 skip")
    sd_path = tmp_path / "dict_v2.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_path))
    sd.generate_semantic_dictionary(include_stats=True)
    d = json.loads(sd_path.read_text(encoding="utf-8"))
    assert d["metadata"]["abox_triples"] > 0


def test_v1_structure_is_subset_of_v2(tmp_path, monkeypatch):
    """v1 의 모든 top-level key 가 v2 에도 존재 (superset 관계)."""
    _require_tbox()
    from tools import semantic_dictionary as sd
    sd_v1 = tmp_path / "dict_v1.json"
    sd_v2 = tmp_path / "dict_v2.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_v1))
    sd.generate_semantic_dictionary(include_stats=False)
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_v2))
    sd.generate_semantic_dictionary(include_stats=True)

    d1 = json.loads(sd_v1.read_text(encoding="utf-8"))
    d2 = json.loads(sd_v2.read_text(encoding="utf-8"))
    # 모든 v1 key 가 v2 에 존재
    for key in d1:
        assert key in d2, f"v1 key '{key}' 가 v2 에 없음"
    # 클래스 목록 자체는 동일 (구조)
    if "classes" in d1 and "classes" in d2:
        assert set(d1["classes"].keys()) == set(d2["classes"].keys()), \
            "v1 과 v2 클래스 목록 불일치"


def test_v1_classes_preserve_dp_names_structure(tmp_path, monkeypatch):
    """v1 에도 각 클래스의 datatype_properties 는 존재해야 (contract 역할).

    통계 필드 (value_stats, distinct_values) 는 빈 값이라도 DP 이름 자체는
    T-Box 기준으로 채워져 있어야 A-Box 생성기가 참조 가능.
    """
    _require_tbox()
    from tools import semantic_dictionary as sd
    sd_path = tmp_path / "dict_v1.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_path))
    sd.generate_semantic_dictionary(include_stats=False)
    d = json.loads(sd_path.read_text(encoding="utf-8"))

    assert "classes" in d
    # 최소 몇 개 클래스는 DP 정의를 가져야 함
    classes_with_dps = [
        name for name, info in d["classes"].items()
        if info.get("datatype_properties")
    ]
    assert len(classes_with_dps) > 0, \
        "v1 에 datatype_properties 있는 클래스 0개 — contract 역할 실패"


def test_default_include_stats_is_true(tmp_path, monkeypatch):
    """인자 없이 호출하면 include_stats=True (v2) 기본 — backward compat."""
    _require_tbox()
    from tools import semantic_dictionary as sd
    sd_path = tmp_path / "dict_default.json"
    monkeypatch.setattr(sd, "SEMANTIC_DICT_PATH", str(sd_path))
    result = json.loads(sd.generate_semantic_dictionary())  # 인자 없음
    assert result["version"] == "v2"
