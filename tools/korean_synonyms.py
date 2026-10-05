"""D3 — Korean synonyms dictionary for NL -> ontology term resolution.

Optional file ``rules/domain/korean_synonyms.json`` format:
    {
      "description": "...",
      "synonyms": {
        "고로": ["BlastFurnace"],
        "용광로": ["BlastFurnace"],
        "설비": ["EquipmentMaster", "Equipment"]
      }
    }

When the file is absent, empty, or malformed, ``_load_synonyms`` returns {}
and downstream helpers produce empty hints — existing ``ask_ontology``
behaviour is unchanged (opt-in).
"""
from __future__ import annotations

import json
import logging
import os

from domain.rules_paths import rules_path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = rules_path("korean_synonyms.json")


def _load_synonyms(path: str | None = None) -> dict[str, list[str]]:
    """Load the synonym map. Returns {} on any failure (graceful)."""
    p = path or _DEFAULT_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("korean_synonyms.json 로드 실패: %s", e)
        return {}
    syn = data.get("synonyms", {}) if isinstance(data, dict) else {}
    if not isinstance(syn, dict):
        return {}
    # Normalize: values must be list[str]
    return {
        k: list(v)
        for k, v in syn.items()
        if isinstance(k, str) and isinstance(v, list)
    }


def resolve_keywords(
    question: str,
    synonyms: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Find Korean synonym keywords in the question.

    Returns list of {"ko": keyword, "classes": [class_names...]} in order of
    first occurrence in the question. Deduped by keyword.
    """
    syn = synonyms if synonyms is not None else _load_synonyms()
    if not syn:
        return []
    matches: list[dict] = []
    seen: set[str] = set()
    # Sort keywords by length (longest first) to avoid substring collisions:
    # "용광로" should match before "광" if both were registered.
    for ko in sorted(syn.keys(), key=len, reverse=True):
        if ko in question and ko not in seen:
            matches.append({"ko": ko, "classes": list(syn[ko])})
            seen.add(ko)
    return matches


def format_synonym_hint(matches: list[dict]) -> str:
    """Render synonym matches as a markdown section for ask_ontology prompt."""
    if not matches:
        return ""
    lines = ["\n## 질문에 등장한 한국어 용어와 온톨로지 클래스"]
    for m in matches:
        classes = ", ".join(m.get("classes", []))
        lines.append(f"- \"{m.get('ko')}\" → {classes}")
    lines.append(
        "\n위 매핑을 우선 사용해 SPARQL 쿼리의 클래스/프로퍼티를 선택하세요.",
    )
    return "\n".join(lines)
