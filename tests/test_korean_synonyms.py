"""D3 — Tests for Korean synonyms dictionary integration."""
from __future__ import annotations

import json

from tools.korean_synonyms import (
    _load_synonyms,
    format_synonym_hint,
    resolve_keywords,
)


def test_load_synonyms_from_file(tmp_path):
    """Valid JSON file returns normalized synonyms dict."""
    p = tmp_path / "korean_synonyms.json"
    p.write_text(
        json.dumps(
            {
                "description": "domain synonyms",
                "synonyms": {
                    "고로": ["BlastFurnace"],
                    "설비": ["EquipmentMaster", "Equipment"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = _load_synonyms(str(p))
    assert result == {
        "고로": ["BlastFurnace"],
        "설비": ["EquipmentMaster", "Equipment"],
    }


def test_load_synonyms_missing_returns_empty(tmp_path):
    """Missing file returns empty dict gracefully."""
    missing = tmp_path / "does_not_exist.json"
    assert _load_synonyms(str(missing)) == {}


def test_load_synonyms_corrupted_returns_empty(tmp_path):
    """Corrupted JSON returns empty dict and logs warning."""
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert _load_synonyms(str(p)) == {}


def test_resolve_keywords_in_question():
    """Keywords found in question return ordered matches."""
    synonyms = {
        "고로": ["BlastFurnace"],
        "알람": ["AlarmEvents"],
    }
    matches = resolve_keywords("고로에서 발생하는 알람", synonyms)
    assert len(matches) == 2
    kos = {m["ko"] for m in matches}
    assert kos == {"고로", "알람"}
    for m in matches:
        assert "ko" in m
        assert "classes" in m
        assert isinstance(m["classes"], list)


def test_resolve_no_match_returns_empty():
    """Question without any synonym keyword returns empty list."""
    synonyms = {"고로": ["BlastFurnace"], "알람": ["AlarmEvents"]}
    matches = resolve_keywords("오늘 날씨가 좋네요", synonyms)
    assert matches == []


def test_format_synonym_hint_renders_section():
    """Matches render as markdown section; empty list → empty string."""
    matches = [
        {"ko": "고로", "classes": ["BlastFurnace"]},
        {"ko": "설비", "classes": ["EquipmentMaster", "Equipment"]},
    ]
    hint = format_synonym_hint(matches)
    assert "질문에 등장한 한국어 용어" in hint
    assert "\"고로\"" in hint
    assert "BlastFurnace" in hint
    assert "EquipmentMaster, Equipment" in hint
    # Empty list → empty string
    assert format_synonym_hint([]) == ""
