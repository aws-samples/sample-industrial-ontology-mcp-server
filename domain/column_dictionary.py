"""표준항목 딕셔너리 lookup (rules/domain/column_dictionary.json).

Single source of truth for CSV-column → (Korean name, value type, XSD range,
unit) resolution. Both Step 12d (deterministic DP injection) and S2
(_load_source_data LLM hints) consume this so naming/range stays consistent.

Lookup order for a column code:
  1. exact match on upper-cased code
  2. digit-stripped base code (a numbered column → its unnumbered base)
  3. alias_index (base → canonical entry key)

Returns None when unresolved (PK/FK identifier columns, or columns absent from
the source dictionary) so callers can fall back to their existing heuristics.
"""
from __future__ import annotations

import json
import logging
import os
import re

from domain.rules_paths import rules_path

logger = logging.getLogger(__name__)

_DICT_PATH = rules_path("column_dictionary.json")

_TRAILING_DIGITS = re.compile(r"\d+$")

# Cache: (entries, alias_index, mtime). Reloaded when the file changes.
_cache: tuple[dict, dict, float] | None = None


def _load() -> tuple[dict, dict]:
    """Return (columns, alias_index), reloading if the JSON changed on disk."""
    global _cache
    try:
        mtime = os.path.getmtime(_DICT_PATH)
    except OSError:
        return {}, {}
    if _cache is not None and _cache[2] == mtime:
        return _cache[0], _cache[1]
    try:
        with open(_DICT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        columns = data.get("columns", {})
        alias_index = data.get("alias_index", {})
        _cache = (columns, alias_index, mtime)
        return columns, alias_index
    except Exception as exc:  # noqa: BLE001 — degrade gracefully to heuristics
        logger.warning("column_dictionary.json 로드 실패 (heuristic fallback): %s", exc)
        return {}, {}


def lookup(column_code: str) -> dict | None:
    """Resolve a CSV column code to its dictionary entry, or None.

    Entry keys: korean_name, value_type (N/C/D), xsd_range, uom_class, unit,
    definition.
    """
    if not column_code:
        return None
    columns, alias_index = _load()
    if not columns:
        return None
    code = column_code.strip().upper()
    if code in columns:
        return columns[code]
    base = _TRAILING_DIGITS.sub("", code)
    if base != code:
        if base in columns:
            return columns[base]
        alias = alias_index.get(base)
        if alias and alias in columns:
            return columns[alias]
    return None


def xsd_range_for(column_code: str) -> str | None:
    """Authoritative XSD range for a column, or None if unresolved/blank."""
    entry = lookup(column_code)
    if entry:
        rng = entry.get("xsd_range")
        if rng:
            return rng
    return None


def korean_label_for(column_code: str) -> str | None:
    """Authoritative Korean name for a column, or None if unresolved/blank."""
    entry = lookup(column_code)
    if entry:
        name = entry.get("korean_name")
        if name:
            return name
    return None


def is_loaded() -> bool:
    """True if the dictionary file is present and parsed at least one entry."""
    columns, _ = _load()
    return bool(columns)
