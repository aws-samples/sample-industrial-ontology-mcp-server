"""Step 12d — Auto-inject missing class-specific DPs from CSV headers (R2 fix).

R1A spec mandates every non-PK/FK CSV column to surface as a class-specific
DatatypeProperty named ``{classCamelLower}{ColumnCamelCase}``. R2 added a
coverage gate (Step 12c) that *measures* compliance. When the Multi-Agent
T-Box generator (S2) skips numeric/measurement columns (Casting_Speed_mmin,
Steel_Grade, etc.), Step 12c only warned — leaving 4 classes at <20%
coverage in our 2026-05-30 run.

This step closes the loop: it scans CSV headers, compares against declared
DPs (using the same `_coverage_match` routine as Step 12c), and injects the
missing DPs deterministically — no LLM, no common_dp.json template
required. Range is inferred from the column suffix (`_mm` → xsd:decimal,
`_Percent` → xsd:decimal, `_C` → xsd:decimal, etc.); fallback xsd:string.

Position: between Step 12b (class-specific enforcer) and Step 12c (coverage
gate). Running before 12c means the gate measures the *post-injection*
state — coverage typically jumps to 100% for the targeted classes.

Toggle via env ``TBOX_AUTO_INJECT_MISSING_DPS`` (default ``true``). Set to
``false`` to disable for legacy/audit comparisons.
"""
from __future__ import annotations

import logging
import os

from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, XSD

from domain import column_dictionary
from tools.quality_steps._base import StepContext, StepResult

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")

logger = logging.getLogger(__name__)


_NUMERIC_SUFFIX_HINTS = (
    # length
    "_mm", "_cm", "_m", "_um", "_km",
    # mass
    "_kg", "_ton", "_g",
    # force
    "_kn", "_n",
    # temperature
    "_c", "_k", "_f",
    # pressure
    "_kpa", "_mpa", "_pa", "_bar", "_psi",
    # power / electrical
    "_kw", "_kwh", "_w", "_v", "_a", "_hz", "_db",
    # flow / volume
    "_m3h", "_m3min", "_nm3h", "_nm3min", "_l", "_ml", "_lph", "_tonh",
    # ratio / percent
    "_percent", "_pct", "_ratio", "_rate",
    # speed
    "_mmin", "_ms", "_kmh", "_rpm",
    # specific physical units commonly seen
    "_mpa", "_hb", "_ra",
)

_INTEGER_SUFFIX_HINTS = ("_count", "_qty", "_quantity", "_days", "_hours", "_min")

_DATE_SUFFIX_HINTS = ("_date", "_time", "_datetime", "_timestamp")


def _infer_range(col_lower: str) -> URIRef:
    """Map CSV column name suffix → xsd datatype."""
    for suf in _DATE_SUFFIX_HINTS:
        if col_lower.endswith(suf):
            return XSD.dateTime
    for suf in _INTEGER_SUFFIX_HINTS:
        if col_lower.endswith(suf):
            return XSD.integer
    for suf in _NUMERIC_SUFFIX_HINTS:
        if col_lower.endswith(suf):
            return XSD.decimal
    return XSD.string


def _column_to_label(col: str) -> str:
    """`Casting_Speed_mmin` → `Casting Speed Mmin` (best-effort English label)."""
    parts = [p for p in col.split("_") if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts) if parts else col


def _column_to_dp_name(class_camel_lower: str, col: str) -> str:
    """`Casting_Speed_mmin`, `processContinuousCasting`
    → `processContinuousCastingCastingSpeedMmin`."""
    parts = [p for p in col.split("_") if p]
    if not parts:
        return class_camel_lower
    camel = "".join(p[:1].upper() + p[1:] for p in parts)
    return class_camel_lower + camel


def apply(g: Graph, ctx: StepContext) -> StepResult:
    enabled = os.getenv("TBOX_AUTO_INJECT_MISSING_DPS", "true").lower() not in ("false", "0", "no")
    if not enabled:
        return StepResult(
            name="step_12d_csv_dp_inject",
            stats={"injected_dps": 0, "skipped": "disabled_via_env"},
            step_number="12d",
            step_label="csv_dp_auto_inject",
        )

    from tools.ontology_quality import (
        _collect_class_dp_source_columns,
        _collect_expected_columns_per_class,
        _count_class_dps,
        _coverage_match,
    )

    steel_str = ctx.domain_ns
    expected = _collect_expected_columns_per_class()

    before = len(g)
    injected = 0
    by_class: dict[str, int] = {}
    samples: list[dict] = []

    for class_name, info in expected.items():
        cols: set[str] = info.get("expected_columns") or set()
        if not cols:
            continue
        class_uri = URIRef(steel_str + class_name)
        # Skip classes that are not declared in the T-Box (e.g. CSV table
        # never matched a class — out of scope for this step).
        if (class_uri, RDF.type, OWL.Class) not in g:
            continue
        class_camel_lower = class_name[:1].lower() + class_name[1:]
        declared = _count_class_dps(g, steel_str, class_name)
        # 출처 표기 (dcterms:source) 로 이미 커버된 컬럼은 이름이 달라도 재주입
        # 금지. 이 집합 없이는 S2 의 한글명 기반 DP
        # (컬럼명과 다른 이름으로 선언된 DP) 를 미커버로 오판해
        # UGLY 중복 (processStepADATECOL1) 을 만든다 (2026-07-25 실측 1,300개).
        declared_sources = _collect_class_dp_source_columns(
            g, steel_str, class_name,
        )

        for col_lower in cols:
            if _coverage_match(col_lower, class_camel_lower, declared,
                               declared_sources):
                continue
            # Recover original casing if available — _collect_expected_columns_per_class
            # lower-cases columns; rebuild from header where possible.
            original = next(
                (c for c in info.get("all_columns", []) if c.lower() == col_lower),
                col_lower,
            )
            dp_name = _column_to_dp_name(class_camel_lower, original)
            dp_uri = URIRef(steel_str + dp_name)
            if (dp_uri, RDF.type, OWL.DatatypeProperty) in g:
                # already declared somewhere — just ensure domain + provenance
                if (dp_uri, RDFS.domain, class_uri) not in g:
                    g.add((dp_uri, RDFS.domain, class_uri))
                if (dp_uri, _DCTERMS_NS.source, None) not in g:
                    g.add((dp_uri, _DCTERMS_NS.source, Literal(original)))
                continue
            # 표준항목 딕셔너리 우선: 권위 있는 값타입(N/C/D)→XSD range 와
            # 한글명. 없으면 컬럼 suffix 휴리스틱으로 폴백.
            dict_entry = column_dictionary.lookup(original)
            dict_range = dict_entry.get("xsd_range") if dict_entry else ""
            if dict_range:
                range_uri = URIRef(dict_range)
                # 딕셔너리는 N→decimal 까지만 단언. 정수성 suffix 면 integer 로 좁힘.
                if range_uri == XSD.decimal and col_lower.endswith(_INTEGER_SUFFIX_HINTS):
                    range_uri = XSD.integer
            else:
                range_uri = _infer_range(col_lower)
            label_en = _column_to_label(original)
            korean_name = dict_entry.get("korean_name") if dict_entry else ""

            g.add((dp_uri, RDF.type, OWL.DatatypeProperty))
            g.add((dp_uri, RDFS.domain, class_uri))
            g.add((dp_uri, RDFS.range, range_uri))
            # 출처 컬럼 표기 — A-Box 생성기가 컬럼↔DP 를 추측 없이 잇는 근거
            # (_col_to_prop step 0). Step 12e 게이트가 커버리지를 측정한다.
            g.add((dp_uri, _DCTERMS_NS.source, Literal(original)))
            g.add((dp_uri, RDFS.label, Literal(label_en, lang="en")))
            if korean_name:
                g.add((dp_uri, RDFS.label, Literal(korean_name, lang="ko")))
            unit = dict_entry.get("unit") if dict_entry else ""
            comment = (
                f"Auto-injected by Step 12d from CSV column '{original}' "
                f"(table {info.get('table', '?')})."
            )
            if korean_name:
                comment = f"{korean_name}({original})." + (
                    f" 단위: {unit}." if unit else ""
                ) + f" Auto-injected by Step 12d (table {info.get('table', '?')})."
            g.add((dp_uri, RDFS.comment, Literal(comment, lang="ko" if korean_name else "en")))
            declared.add(dp_name.lower())  # avoid double-injection in same loop
            injected += 1
            by_class[class_name] = by_class.get(class_name, 0) + 1
            if len(samples) < 10:
                samples.append({
                    "class": class_name,
                    "column": original,
                    "dp": dp_name,
                    "range": str(range_uri),
                })

    if injected:
        logger.info(
            "Step 12d: auto-injected %d class-specific DPs across %d classes (%s)",
            injected, len(by_class), sorted(by_class.items(), key=lambda x: -x[1])[:5],
        )

    stats = {
        "injected_dps": injected,
        "classes_touched": len(by_class),
        "by_class": by_class,
        "samples": samples,
    }
    return StepResult(
        name="step_12d_csv_dp_inject",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="12d",
        step_label="csv_dp_auto_inject",
    )
