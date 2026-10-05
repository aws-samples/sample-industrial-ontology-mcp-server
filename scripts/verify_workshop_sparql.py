#!/usr/bin/env python3
"""Verify every code block in workshop/*.md is runnable.

Coverage:
- ```sparql blocks: run on workshop/pre-generated/{t_box,a_box}.ttl, must
  return >= 1 row (or be marked `<!-- zero-rows-ok: reason -->`).
- ```json blocks: must parse as valid JSON (or be marked
  `<!-- json-fragment-ok: reason -->` for snippet/example fragments).
- ```turtle blocks: must parse as Turtle via rdflib (with implicit
  `@prefix steel: ...` etc. injected).
- ```bash blocks: must pass `bash -n` syntax check (or be marked
  `<!-- placeholder-ok: reason -->` if they contain user-fillable
  `<...>` placeholders that intentionally break shell syntax).

Exit code 0 = all blocks pass or are marked. Exit 1 = at least one
fails without a marker.

Usage:
    python scripts/verify_workshop_sparql.py

Add to instructor-guide D-7 checklist when regenerating pre-generated
artifacts: rerun and ensure exit 0 before workshop day.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import rdflib

REPO = Path(__file__).resolve().parent.parent
PRE_TBOX = REPO / "workshop/pre-generated/t_box.ttl"
PRE_ABOX = REPO / "workshop/pre-generated/a_box.ttl.gz"
PRE_ABOX_SHA256 = (
    "8a448d45aa09d87674b41d3d928b892a7"  # pragma: allowlist secret
    "58650eebb01af415530e5c469a17b30"  # pragma: allowlist secret
)

# instructor-guide intentionally has no SPARQL blocks; keeping it out of the
# list avoids a "no SPARQL blocks found" empty-section header.
DOCS = [
    REPO / "workshop/participant-workbook.md",
    REPO / "workshop/sparql-cheatsheet.md",
    REPO / "workshop/setup-guide.md",
    REPO / "workshop/post-workshop-guide.md",
]

# Files where unfilled-placeholder check is enforced (instructor-guide is
# template documentation and intentionally describes blank placeholders).
PLACEHOLDER_CHECK_FILES = {
    "workshop/setup-guide.md",
}

# Allow-list for instructor-controlled placeholder regions that the
# instructor MAY fill but is not strictly required to before commit.
# Keys are file paths; values are list of marker strings whose containing
# line is whitelisted.
PLACEHOLDER_ALLOW = {
    "workshop/setup-guide.md": [
        # the documentation row inside the "강사 연락처" table itself —
        # this row exists to BE filled, but the schema (이름/채널) is
        # described elsewhere; we still flag it so the instructor must
        # replace before distributing.
    ],
}

#: Sections the distributed dictionary must carry. Participants copy
#: ``workshop/pre-generated/semantic_dictionary.json`` into ``data/generated/``
#: (setup-guide §"pre-generated 산출물을 정상 경로로 복사"), and
#: ``read_semantic_dictionary`` returns the file **verbatim** — every section is
#: LLM context for NL→SPARQL. A section dropped from the copy silently degrades
#: the workshop.
#:
#: Why this check exists (2026-08-26): the leak gate blocked a dictionary update
#: over 11 unit-suffixed column keys, and the fix applied was to strip the whole
#: ``column_to_classes`` index (199 entries) from the distributed copy. This
#: script still reported PASS 49 / FAIL 0 — it only executes ```sparql blocks and
#: never opens the dictionary. Neither does ``validate_semantic_dictionary``:
#: its ``required_sections`` list (tools/semantic_dict_validation.py) has eight
#: entries and ``column_to_classes`` is not among them. Nothing in the repo could
#: see a fifth of the vocabulary contract go missing.
PRE_DICT = REPO / "workshop/pre-generated/semantic_dictionary.json"
DICT_REQUIRED_SECTIONS = (
    "metadata", "classes", "object_properties", "sparql_guide",
    "common_mistakes", "question_templates", "class_quick_reference",
    "process_flow", "column_to_classes",
)


def verify_gzip_artifact(path: Path, expected_sha256: str) -> list[str]:
    """gzip 산출물을 해제한 바이트 기준 SHA-256으로 검증한다."""
    if not path.is_file():
        return [f"missing file: {path.name}"]
    digest = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, EOFError):
        return ["invalid gzip stream"]
    if digest.hexdigest() != expected_sha256:
        return ["decompressed SHA-256 mismatch"]
    return []


def verify_dictionary() -> list[str]:
    """Return human-readable problems with the distributed semantic dictionary.

    Empty list means the file is complete. Checks only for *absence* — content
    drift against ``data/generated/`` is not asserted, because the two are
    legitimately generated at different times.
    """
    if not PRE_DICT.exists():
        return [f"missing file: {PRE_DICT.relative_to(REPO)}"]
    try:
        data = json.loads(PRE_DICT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"unreadable: {exc}"]

    problems = [
        f"section missing: {name}"
        for name in DICT_REQUIRED_SECTIONS
        if name not in data
    ]
    problems += [
        f"section empty: {name}"
        for name in DICT_REQUIRED_SECTIONS
        if name in data and not data[name]
    ]
    # A redaction marker means someone removed content to satisfy another gate.
    # That is a decision worth surfacing, not a silent state.
    if isinstance(data.get("metadata"), dict):
        for key in data["metadata"]:
            if "redact" in key.lower():
                problems.append(f"metadata carries a redaction marker: {key}")
    return problems


PREFIXES = """PREFIX steel: <http://example.com/steel-ontology#>
PREFIX steel-inst: <http://example.com/steel-ontology/instances#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

TURTLE_PREFIXES = """@prefix steel: <http://example.com/steel-ontology#> .
@prefix steel-inst: <http://example.com/steel-ontology/instances#> .
@prefix iof-core: <https://example.com/iof/core#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""

CODE_BLOCK = re.compile(r"```(\w+)\n(.*?)\n```", re.DOTALL)
ZERO_OK = re.compile(r"<!--\s*zero-rows-ok:\s*(.*?)\s*-->")
JSON_FRAGMENT_OK = re.compile(r"<!--\s*json-fragment-ok:\s*(.*?)\s*-->")
PLACEHOLDER_OK = re.compile(r"<!--\s*placeholder-ok:\s*(.*?)\s*-->")
QUERY_KEYWORD = re.compile(
    r"^\s*(SELECT|ASK|CONSTRUCT|DESCRIBE)", re.MULTILINE | re.IGNORECASE
)

# Detect un-filled placeholders that the workshop instructor must replace
# before distributing the package to participants. These are template lines
# like `| 강사 이름 | _________________________ |` that have no value but
# pass markdown / json / sparql verifiers because they're tabular text.
# Skip lines that mention "_____" only as documentation about the template
# itself (avoid false positive in instructor-guide).
UNFILLED_PLACEHOLDER = re.compile(r"_{8,}")


def find_marker(pattern: re.Pattern, text: str, block_start: int) -> str | None:
    lookback = text[max(0, block_start - 500):block_start]
    matches = pattern.findall(lookback)
    return matches[-1] if matches else None


def first_line(block: str, limit: int = 70) -> str:
    for line in block.strip().split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            return stripped[:limit]
    return block.strip().split("\n")[0][:limit]


def strip_sparql_comments(query: str) -> str:
    return "\n".join(
        line for line in query.split("\n") if not line.strip().startswith("#")
    )


# ── Verifiers per language ─────────────────────────────────────────────


def verify_sparql(block: str, graph: rdflib.Graph) -> tuple[bool, str]:
    cleaned = strip_sparql_comments(block)
    if not QUERY_KEYWORD.search(cleaned):
        return True, "skip (not SELECT/ASK)"
    try:
        rows = list(graph.query(PREFIXES + cleaned))
    except Exception as exc:
        return False, f"PARSE ERROR: {str(exc)[:80]}"
    return (len(rows) > 0), f"{len(rows)} rows"


def verify_json(block: str) -> tuple[bool, str]:
    try:
        json.loads(block)
        return True, "valid JSON"
    except Exception as exc:
        return False, f"PARSE ERROR: {str(exc)[:80]}"


def verify_turtle(block: str) -> tuple[bool, str]:
    candidate = block if "@prefix" in block else TURTLE_PREFIXES + block
    try:
        g = rdflib.Graph()
        g.parse(data=candidate, format="turtle")
        return True, f"{len(g)} triples"
    except Exception as exc:
        return False, f"PARSE ERROR: {str(exc)[:80]}"


# Windows 기본 환경에는 bash 가 없다. bash 블록은 "구문" 검증 대상이라
# Windows(Git Bash 미설치)에서는 검증을 의미있게 할 수 없으므로 PASS 로
# 스킵한다 (FAIL 로 처리하면 Windows fallback 사용자가 exit 0 을 못 받아
# D-1 회신이 막힌다). bash 가 있으면 기존대로 `bash -n` 문법 검사.
_BASH_AVAILABLE = shutil.which("bash") is not None


def verify_bash(block: str) -> tuple[bool, str]:
    if not _BASH_AVAILABLE:
        return True, "skipped (no bash — Windows; 구문 검증 생략)"
    try:
        result = subprocess.run(
            ["bash", "-n", "-c", block],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, "syntax OK"
        return False, f"SYNTAX ERROR: {result.stderr.strip()[:80]}"
    except Exception as exc:
        return False, f"ERROR: {str(exc)[:80]}"


# ── Main ───────────────────────────────────────────────────────────────


def main() -> int:
    # Flag --ignore-placeholders skips the §10 강사 연락처 unfilled-placeholder
    # check. Used by repo-side CI (the package commit intentionally has blank
    # placeholders for the instructor to fill before distributing). Workshop
    # instructors should run WITHOUT this flag before D-3 to ensure §10 is
    # filled.
    ignore_placeholders = "--ignore-placeholders" in sys.argv

    if not PRE_TBOX.exists() or not PRE_ABOX.exists():
        print(f"ERROR: pre-generated files not found at {PRE_TBOX}")
        return 1

    artifact_problems = verify_gzip_artifact(PRE_ABOX, PRE_ABOX_SHA256)
    if artifact_problems:
        for problem in artifact_problems:
            print(f"ERROR: {PRE_ABOX.relative_to(REPO)}: {problem}")
        return 1

    dict_problems = verify_dictionary()
    if dict_problems:
        print(f"=== {PRE_DICT.relative_to(REPO)} ===")
        for problem in dict_problems:
            print(f"  [✗ dict     ] {problem}")
        print()

    print(f"Loading {PRE_TBOX} + {PRE_ABOX} ...")
    graph = rdflib.Graph()
    graph.parse(PRE_TBOX, format="turtle")
    with gzip.open(PRE_ABOX, "rt", encoding="utf-8") as stream:
        graph.parse(source=stream, format="turtle")
    print(f"  triples = {len(graph):,}\n")

    failures: list[tuple[str, int, str, str, str]] = []
    placeholder_failures: list[tuple[str, int, str]] = []
    counts = {"PASS": 0, "MARKED": 0, "FAIL": 0, "SKIP": 0}

    for path in DOCS:
        if not path.exists():
            continue
        text = path.read_text()
        rel = str(path.relative_to(REPO))
        print(f"=== {rel} ===")

        # Check unfilled placeholders before per-block verification (M6
        # instructor-fills like "강사 이름 ___" in setup-guide §10).
        if rel in PLACEHOLDER_CHECK_FILES and not ignore_placeholders:
            for line_no, line in enumerate(text.splitlines(), start=1):
                if UNFILLED_PLACEHOLDER.search(line):
                    placeholder_failures.append((rel, line_no, line.strip()[:80]))
                    print(f"  [✗ placeholder] line {line_no}: {line.strip()[:60]}")

        for match in CODE_BLOCK.finditer(text):
            lang = match.group(1).lower()
            block = match.group(2)
            label = first_line(block)

            # Dispatch by language
            if lang == "sparql":
                ok, msg = verify_sparql(block, graph)
                marker = find_marker(ZERO_OK, text, match.start())
                marker_kind = "zero-rows-ok"
            elif lang == "json":
                ok, msg = verify_json(block)
                marker = find_marker(JSON_FRAGMENT_OK, text, match.start())
                marker_kind = "json-fragment-ok"
            elif lang == "turtle":
                ok, msg = verify_turtle(block)
                marker = None  # No marker scheme for turtle yet
                marker_kind = ""
            elif lang == "bash":
                ok, msg = verify_bash(block)
                marker = find_marker(PLACEHOLDER_OK, text, match.start())
                marker_kind = "placeholder-ok"
            else:
                # sql, text, etc. — skip silently
                counts["SKIP"] += 1
                continue

            if msg == "skip (not SELECT/ASK)":
                counts["SKIP"] += 1
                continue

            if ok:
                counts["PASS"] += 1
                print(f"  [✓ {lang:7}] {msg:30}  {label}")
            elif marker:
                counts["MARKED"] += 1
                print(f"  [○ {lang:7}] marked: {marker[:30]:30}  {label}")
            else:
                counts["FAIL"] += 1
                # marker_kind names the escape hatch for *this* block type, so a
                # failure line tells the author what to add rather than making
                # them look it up in the Fix options list below.
                hint = f" (or mark `{marker_kind}`)" if marker_kind else ""
                print(f"  [✗ {lang:7}] {msg:30}  {label}{hint}")
                failures.append(
                    (
                        str(path.relative_to(REPO)),
                        match.start(),
                        lang,
                        label,
                        msg,
                    )
                )
        print()

    print("─" * 70)
    print(
        f"PASS {counts['PASS']}  |  MARKED {counts['MARKED']}  "
        f"|  FAIL {counts['FAIL']}  |  SKIP {counts['SKIP']}  "
        f"|  PLACEHOLDER-FAIL {len(placeholder_failures)}  "
        f"|  DICT-FAIL {len(dict_problems)}"
    )

    if failures:
        print("\nFailures:")
        for path, offset, lang, label, msg in failures:
            print(f"  [{lang}] {path}@{offset}: {msg}")
            print(f"    {label}")
        print(
            "\nFix options:\n"
            "  sparql:  correct the query OR add `<!-- zero-rows-ok: reason -->` above.\n"
            "  json:    fix syntax OR add `<!-- json-fragment-ok: reason -->` above\n"
            "           (for snippet/example fragments that aren't standalone JSON).\n"
            "  turtle:  fix syntax. No marker scheme yet — TTL examples must parse.\n"
            "  bash:    fix syntax OR add `<!-- placeholder-ok: reason -->` above\n"
            "           (for snippets with user-fillable `<...>` placeholders)."
        )

    if placeholder_failures:
        print("\nUnfilled placeholders — instructor must replace before workshop:")
        for path, line_no, content in placeholder_failures:
            print(f"  [{path}:{line_no}] {content}")
        print(
            "\nFix: replace the `_____...` template with the actual instructor "
            "name / channel / deadline value before distributing the workshop "
            "package to participants. (setup-guide §10 강사 연락처 테이블)"
        )

    if dict_problems:
        print(
            "\nSemantic dictionary incomplete — participants copy this file into\n"
            "data/generated/ and read_semantic_dictionary returns it verbatim, so a\n"
            "missing section removes vocabulary the LLM needs for NL→SPARQL.\n"
            "Fix: regenerate with generate_semantic_dictionary and copy the whole\n"
            "file. Do not strip sections to satisfy another gate — fix that gate."
        )

    if failures or placeholder_failures or dict_problems:
        return 1

    print("\nAll workshop code blocks runnable / parseable / marked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
