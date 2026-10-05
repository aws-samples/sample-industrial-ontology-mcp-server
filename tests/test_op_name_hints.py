"""P0 — rules/policy/op_name_hints.json 기반 Architect 프롬프트 힌트 주입 검증.

파일 없거나 비어 있으면 기존 프롬프트 동작 완전 보존 (backward compat).
"""
from __future__ import annotations

import json
from unittest.mock import patch


def _minimal_csv(tmp_path):
    raw = tmp_path / "rawdata"
    raw.mkdir(exist_ok=True)
    (raw / "Equipment_Master.csv").write_text("Equipment_ID,Name\nEQ001,pump\n", encoding="utf-8")
    (raw / "Alarm_Events.csv").write_text("Event_ID,Equipment_ID,Severity\nE1,EQ001,HIGH\n", encoding="utf-8")


def _rules_dir_with(tmp_path, hints_payload: dict | None):
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "fk_patterns.json").write_text(json.dumps({"patterns": {"equipmentid": "EquipmentMaster"}}), encoding="utf-8")
    if hints_payload is not None:
        (rules / "op_name_hints.json").write_text(
            json.dumps(hints_payload, ensure_ascii=False), encoding="utf-8",
        )
    return str(rules)


def _build_prompt(tmp_path, rules_dir):
    from tools import tbox_generation as tg
    with patch.object(tg, "_RULES_DIR", rules_dir), \
         patch("config.SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata")):
        cached_prefix, variable_prompt = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "Alarm_Events", "columns": ["Event_ID", "Equipment_ID"]}],
            total_chunks=1,
            iof_summary="(없음)",
            schema_info={},
            relationships_info="",
            table_class_map={"Alarm_Events": "AlarmEvents"},
        )
    return cached_prefix + "\n" + variable_prompt


def test_op_name_hints_file_schema_valid_roundtrip():
    """rules/policy/op_name_hints.json 의 실제 프로덕션 파일 로드 확인 — 필드 구조."""
    from tools import tbox_generation as tg
    hints = tg._load_op_name_hints()
    # 프로덕션 파일은 존재해야 함 (P0 git-committed)
    assert isinstance(hints, dict)
    if hints:  # 비어 있을 수도 있음 (테스트 환경)
        assert "prefer_these_names_when_applicable" in hints or "avoid_these_patterns" in hints


def test_hints_injected_into_architect_prompt(tmp_path):
    """_build_chunk_prompt 가 op_name_hints 의 내용을 cached_prefix 에 포함."""
    _minimal_csv(tmp_path)
    hints = {
        "prefer_these_names_when_applicable": [
            {"name": "hasInput", "meaning": "process ← consumes material"},
            {"name": "hasParticipant", "meaning": "process ↔ participant"},
        ],
        "avoid_these_patterns": [
            "doubleVerb (e.g., hasHasPart)",
        ],
    }
    rules_dir = _rules_dir_with(tmp_path, hints)
    prompt = _build_prompt(tmp_path, rules_dir)
    assert "ObjectProperty 네이밍 힌트" in prompt
    assert "hasInput" in prompt
    assert "hasParticipant" in prompt
    assert "doubleVerb" in prompt


def test_missing_hints_file_returns_empty_noop(tmp_path):
    """op_name_hints.json 파일 없으면 프롬프트에 힌트 섹션 없음."""
    _minimal_csv(tmp_path)
    rules_dir = _rules_dir_with(tmp_path, hints_payload=None)  # 파일 미생성
    prompt = _build_prompt(tmp_path, rules_dir)
    # 힌트 섹션 헤더 부재 확인
    assert "ObjectProperty 네이밍 힌트" not in prompt


def test_hint_content_has_prefer_and_avoid_sections(tmp_path):
    """prefer/avoid 둘 다 있으면 각 섹션 헤더 렌더링."""
    _minimal_csv(tmp_path)
    hints = {
        "prefer_these_names_when_applicable": [
            {"name": "hasInput", "meaning": "test"},
        ],
        "avoid_these_patterns": ["self-prefixing"],
    }
    rules_dir = _rules_dir_with(tmp_path, hints)
    prompt = _build_prompt(tmp_path, rules_dir)
    assert "선호 이름 목록" in prompt
    assert "피해야 할 패턴" in prompt


def test_hints_only_prefer_section_when_avoid_empty(tmp_path):
    """avoid 비어 있으면 prefer 섹션만 렌더, avoid 헤더 없음."""
    _minimal_csv(tmp_path)
    hints = {
        "prefer_these_names_when_applicable": [
            {"name": "hasOutput", "meaning": "process → produces"},
        ],
        "avoid_these_patterns": [],
    }
    rules_dir = _rules_dir_with(tmp_path, hints)
    prompt = _build_prompt(tmp_path, rules_dir)
    assert "hasOutput" in prompt
    assert "선호 이름 목록" in prompt
    assert "피해야 할 패턴" not in prompt


def test_corrupt_hints_file_gracefully_disabled(tmp_path):
    """JSON 손상된 op_name_hints.json 은 조용히 무시 (backward compat)."""
    from tools import tbox_generation as tg
    _minimal_csv(tmp_path)
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "fk_patterns.json").write_text(json.dumps({"patterns": {}}), encoding="utf-8")
    (rules / "op_name_hints.json").write_text("{not valid json", encoding="utf-8")
    with patch.object(tg, "_RULES_DIR", str(rules)):
        hints = tg._load_op_name_hints()
    assert hints == {}


def test_render_block_skips_invalid_entries():
    """prefer 목록에 dict 아닌 항목이나 name 없는 entry 는 skip."""
    from tools import tbox_generation as tg
    with patch.object(tg, "_load_op_name_hints", return_value={
        "prefer_these_names_when_applicable": [
            {"name": "hasInput", "meaning": "ok"},
            "not a dict",  # invalid
            {"meaning": "no name field"},  # invalid
            {"name": "hasOutput", "meaning": "valid"},
        ],
        "avoid_these_patterns": ["", None, "valid-pattern"],
    }):
        block = tg._render_op_name_hints_block()
    assert "hasInput" in block
    assert "hasOutput" in block
    # invalid 항목이 렌더링되지 않았는지 확인
    assert "not a dict" not in block
    assert "no name field" not in block
    assert "valid-pattern" in block
