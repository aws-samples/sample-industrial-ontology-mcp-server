"""Rules-based deterministic tacit TTL generation and CSV FK augmentation.

Provides two MCP tools that replace domain-specific scripts with a reusable
JSON-driven engine:

* ``augment_csv_fk`` — adds or normalizes a foreign-key column on a source CSV,
  drawing target PK values from another CSV deterministically.
* ``generate_tacit_from_rules`` — reads ``rules/domain/tacit_rules.json`` and emits
  deterministic instance-level TTL mappings using four strategies:

  1. ``rotation`` — map source rows to target PKs in round-robin order
     (optionally filtered by column value).
  2. ``number_match`` — pair source and target PKs by trailing numeric
     components (MP001 ↔ EQ001). Requires matching digit counts.
  3. ``simple_join`` — follow a single FK column in the source CSV to find the
     matching target PK.
  4. ``via_mapping_chain`` — compose previously-generated mappings in-memory
     so multi-hop shortcuts can be built without reading the A-Box.
  5. ``fk_lookup_table`` — follow a source column through a lookup CSV to the
     target PK (1:1 indirect FK).
  6. ``shared_column_join`` — many-to-many join on a shared "bucket" value
     (e.g. both rows share a plant Location/area). Each side resolves its
     bucket either directly from a column or through a single lookup CSV.

The rules engine is purely deterministic: same inputs always produce the same
output, so re-running after CSV changes keeps tacit TTL in sync without LLM
calls. Contrast with ``generate_tacit_from_data`` (LLM bootstrap, only run
once when no domain rules exist yet).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from typing import Any

from config import PROJECT_ROOT, SOURCE_RAWDATA_DIR, SOURCE_TACIT_DIR
from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, NS_INST_PREFIX, NS_PREFIX
from domain.rules_paths import rules_path
from tools.common import atomic_write, error_response, resolve_child_path

logger = logging.getLogger(__name__)

_RULES_PATH = rules_path("tacit_rules.json")
_SUGGESTED_RULES_PATH = rules_path("tacit_rules.suggested.json")


# A2: 시계열 컬럼 감지 — abox_generation._TIMESTAMP_COLUMNS 와 동일 패턴.
# _abox_pk_iri 내 _ts() 에서도 같은 패턴을 쓰지만 모듈 레벨로 재사용 가능하게
# 분리 (suggest_tacit_rules 프롬프트, generate_tacit_from_rules 경고 로그에서
# CSV 헤더 레벨 감지에 사용).
_TIMESTAMP_COL_PATTERNS: tuple[str, ...] = (
    "timestamp", "datetime", "measurementdatetime",
    "testdatetime", "occurrencedatetime", "departuretime",
    "generationdate", "maintenancedate", "orderdate", "plandate",
)


def _detect_timestamp_column(headers: list[str]) -> str | None:
    """Return the first header matching a known timestamp pattern, else None.

    Two-pass match:
      1. Exact lowercase equality (``Timestamp`` → ``timestamp``).
      2. Substring match to cover conventions like ``Measurement_Timestamp``,
         ``Event_DateTime`` that the exact lookup misses.
      3. Date suffix (``Order_Date``) — covers typical ERP columns.
    """
    lower_to_orig = {h.lower(): h for h in headers}
    for pat in _TIMESTAMP_COL_PATTERNS:
        if pat in lower_to_orig:
            return lower_to_orig[pat]
    for pat in _TIMESTAMP_COL_PATTERNS:
        for h in headers:
            if pat in h.lower():
                return h
    for h in headers:
        lh = h.lower()
        if lh.endswith("date") or lh.endswith("_date") or lh.endswith("datetime"):
            return h
    return None


def _build_timeseries_prompt_section(csv_summary: dict[str, list[str]]) -> str:
    """Return a markdown section listing detected time-series CSV tables.

    Intended to be concatenated into the ``suggest_tacit_rules`` LLM prompt so
    the Architect knows which tables likely need composite PK with a timestamp
    column. Returns an empty string when no time-series tables are detected.

    Args:
        csv_summary: Mapping from CSV filename → list of header names.

    Returns:
        Markdown text (empty when nothing detected).
    """
    flagged: list[tuple[str, str]] = []
    for csv_name, headers in csv_summary.items():
        ts_col = _detect_timestamp_column(headers)
        if ts_col:
            flagged.append((csv_name, ts_col))
    if not flagged:
        return ""

    lines = [
        "## 시계열 테이블 감지 (composite PK 필수)",
        "",
        "다음 CSV 는 시간 컬럼을 포함합니다. **규칙 작성 시 반드시**",
        "`source_composite_pk` 에 해당 컬럼을 포함해야 A-Box IRI 와 일치합니다.",
        "누락 시 tacit TTL 이 생성되지만 CQ 조인 0건이 발생할 수 있습니다",
        "(A-Box 는 Timestamp 가 suffix 에 붙지만 tacit 은 붙지 않아 IRI 불일치).",
        "",
    ]
    for csv_name, ts_col in flagged[:15]:   # 최대 15개만 프롬프트에 노출
        lines.append(f"- `{csv_name}` — timestamp 컬럼: `{ts_col}`")
    return "\n".join(lines)


# ── CSV helpers ─────────────────────────────────────────


def _read_csv_rows(filename: str) -> tuple[list[str], list[dict]]:
    path = resolve_child_path(
        SOURCE_RAWDATA_DIR,
        filename,
        allowed_suffixes=(".csv",),
    )
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def _write_csv_rows(filename: str, fieldnames: list[str], rows: list[dict]) -> None:
    path = resolve_child_path(
        SOURCE_RAWDATA_DIR,
        filename,
        allowed_suffixes=(".csv",),
    )
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_target_pks(
    target_csv: str, pk_column: str, filter_column: str = "", filter_value: str = "",
) -> list[str]:
    _, rows = _read_csv_rows(target_csv)
    if filter_column and filter_value:
        rows = [r for r in rows if r.get(filter_column) == filter_value]
    return [r[pk_column] for r in rows if r.get(pk_column)]


# ── augment_csv_fk ──────────────────────────────────────


def augment_csv_fk(
    source_csv: str,
    new_column: str,
    target_csv: str,
    target_pk_column: str,
    filter_column: str = "",
    filter_value: str = "",
) -> str:
    """Add a new FK column to source_csv, drawing values from target_csv PK.

    Deterministic rotation — source row i gets target_pks[i % N]. Idempotent:
    if new_column already exists in source_csv, the call is a no-op.

    Use this when the CSV schema is missing a relationship that the domain
    actually implies (e.g. PurchaseOrder rows ought to reference a warehouse
    but the CSV has no such column). The added values are synthetic but
    structurally valid FKs, so A-Box generation can translate them into
    ObjectProperty triples.

    Args:
        source_csv: Relative filename under data/source/rawdata/.
        new_column: Header name to add (e.g. "Warehouse_Code").
        target_csv: CSV whose PK column supplies the values.
        target_pk_column: PK column name in target_csv.
        filter_column: Optional — restrict target_csv to rows where this
            column equals filter_value (e.g. Source_Type='Steam').
        filter_value: Value to match with filter_column.

    Returns:
        JSON with action summary: added / skipped / rows.
    """
    try:
        src_fields, src_rows = _read_csv_rows(source_csv)
        if new_column in src_fields:
            return json.dumps({
                "success": True,
                "action": "skipped",
                "reason": f"column {new_column!r} already exists in {source_csv}",
                "rows": len(src_rows),
            }, ensure_ascii=False, indent=2)

        target_pks = _load_target_pks(
            target_csv, target_pk_column, filter_column, filter_value,
        )
        if not target_pks:
            return error_response(
                f"target_csv={target_csv} has no usable PK values "
                f"(column={target_pk_column}, filter={filter_column}={filter_value})",
                logger=logger,
            )

        for i, row in enumerate(src_rows):
            row[new_column] = target_pks[i % len(target_pks)]

        _write_csv_rows(source_csv, src_fields + [new_column], src_rows)

        return json.dumps({
            "success": True,
            "action": "added",
            "source_csv": source_csv,
            "new_column": new_column,
            "rows_updated": len(src_rows),
            "target_pk_pool": len(target_pks),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


# ── generate_tacit_from_rules ───────────────────────────


def _trailing_int(value: str) -> int | None:
    """Return the integer suffix of a string, e.g. 'MP042' → 42."""
    m = re.search(r"(\d+)$", value or "")
    return int(m.group(1)) if m else None


def _format_pk(template: str, n: int) -> str:
    """Render a PK template with {n} or Python format spec, e.g. 'EQ{n:03d}'."""
    try:
        return template.format(n=n)
    except Exception:
        return template


def _ns_inst_iri(local_name: str) -> str:
    """Render a steel-inst:LocalName in TTL form."""
    return f"{NS_INST_PREFIX}:{local_name}"


def _abox_pk_iri(
    class_name: str, row: dict, pk_column: str | None, composite_pk: list[str] | None,
    auto_timestamp: bool = True,
) -> str | None:
    """Mirror of abox_generation._detect_pk_value for IRI reproducibility.

    Rules (A-Box):
      * single PK column → ``{class}_{value}`` with non-alnum → ``_``.
      * composite PK → ``{class}_{part1}_{part2}_..._{timestamp_if_present}``
        where each part is non-alnum-stripped AND a trailing timestamp suffix
        is re-appended if any column in the row looks like a timestamp.

    Used by tacit_rules so the IRIs it generates actually match the A-Box
    instances (otherwise cq tests still see 0 joins despite rule execution).

    Args:
        auto_timestamp: (A2) When True (default) and a single ``pk_column`` is
            supplied (no composite_pk), automatically append the row's
            timestamp column to the IRI suffix if present. Mirrors the A-Box
            ``_detect_pk_value`` fallback path (pk_is_unique_single=False) so
            time-series tables (Process_Blast_Furnace etc.) stay IRI-aligned
            even when the user omits Timestamp from ``source_composite_pk``.
            Pass False for strict single-PK behaviour (legacy).
    """
    row_lower = {k.lower(): str(v).strip() for k, v in row.items() if v and str(v).strip()}

    def _norm(v: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", v)

    def _ts(row_lower: dict) -> str | None:
        for key in ("timestamp", "datetime", "measurementdatetime", "testdatetime",
                    "occurrencedatetime", "departuretime", "generationdate",
                    "maintenancedate", "orderdate", "plandate"):
            v = row_lower.get(key)
            if v:
                return _norm(v)
        return None

    if composite_pk:
        parts: list[str] = []
        for col in composite_pk:
            v = row_lower.get(col.lower())
            if not v:
                return None
            parts.append(_norm(v))
        base = "_".join(parts)
        # 2026-05-10 fix: composite PK 에 timestamp 컬럼이 이미 포함돼 있으면
        # 중복 접미 방지. abox_generation._detect_pk_value 의 composite_has_ts
        # 플래그와 동일 규칙을 미러링 — 두 경로의 IRI 가 일치해야 inferred
        # 역방향 triple 이 댕글링으로 떨어지지 않는다.
        composite_has_ts = any(
            col.lower() in _TIMESTAMP_COL_PATTERNS
            or "timestamp" in col.lower()
            or "datetime" in col.lower()
            for col in composite_pk
        )
        if not composite_has_ts:
            ts = _ts(row_lower)
            if ts:
                return f"{class_name}_{base}_{ts}"
        return f"{class_name}_{base}"

    if pk_column:
        v = row_lower.get(pk_column.lower())
        if v:
            base = f"{class_name}_{_norm(v)}"
            if auto_timestamp:
                # A2: single-PK rows on time-series tables (Timestamp column
                # present) should mirror the A-Box fallback which appends ts.
                # Without this, tacit IRIs miss the ts suffix and CQ joins hit 0.
                ts = _ts(row_lower)
                if ts:
                    return f"{base}_{ts}"
            return base
    return None


def _build_pk_to_iri(
    class_name: str, csv_filename: str, pk_column: str, composite_pk: list[str] | None,
) -> dict[str, str]:
    """Return dict mapping PK (or composite key) → IRI local name.

    For composite PKs, the key is the values joined with '_' matching
    abox_generation's URI convention.
    """
    _, rows = _read_csv_rows(csv_filename)
    out: dict[str, str] = {}
    for row in rows:
        if composite_pk:
            parts = [row.get(c, "") for c in composite_pk]
            if not all(parts):
                continue
            key = "_".join(parts)
            iri_local = f"{class_name}_{key}"
        else:
            key = row.get(pk_column, "")
            if not key:
                continue
            iri_local = f"{class_name}_{key}"
        out[key] = iri_local
    return out


def _is_pk_unique_in_csv(csv_filename: str, pk_column: str) -> bool:
    """Return True when every non-empty ``pk_column`` value in ``csv_filename``
    is distinct.

    Mirrors the judgement used by ``abox_generation._detect_pk_column`` at
    line 580 (``distinct_ratio >= 1.0`` → single PK uniqueness confirmed).
    When the A-Box path hits that branch it *skips* timestamp suffixing on
    the generated IRI. Tacit rules must match the same choice, otherwise
    the per-row IRIs diverge (e.g. ``AlarmEvents_EVT00001`` vs
    ``AlarmEvents_EVT00001_2025-...``) and CQ joins stay at 0.

    Caching is deliberately skipped — each strategy runs once per rule,
    and ``_read_csv_rows`` is cheap enough compared to the LLM-free TTL
    emission that already dominates the path.
    """
    try:
        _, rows = _read_csv_rows(csv_filename)
    except Exception:
        return False
    if not rows:
        return False
    values = [
        str(row.get(pk_column, "")).strip() for row in rows
        if str(row.get(pk_column, "")).strip()
    ]
    if not values:
        return False
    return len(set(values)) == len(values)


def _warn_composite_pk_missing(rule: dict) -> None:
    """Log a hint when a rule's source_csv has a timestamp column but the rule
    omits it from source_composite_pk.

    A2 (post-fix): the strategy functions now run a CSV-level uniqueness
    precheck before calling ``_abox_pk_iri``. When ``source_pk_column`` is
    unique the tacit path auto-disables ``auto_timestamp`` so IRIs mirror
    the A-Box (which also skips the ts suffix on unique single PKs).
    When the PK is NOT unique the ts suffix is appended — matching the
    A-Box fallback branch. The hint below is informational and reminds
    SMEs that explicit ``source_composite_pk`` is still the clearest
    way to declare intent.
    """
    src_csv = rule.get("source_csv")
    if not src_csv:
        return
    try:
        headers, _ = _read_csv_rows(src_csv)
    except Exception:
        # CSV missing / unreadable — the strategy itself will raise; silent.
        return
    ts_col = _detect_timestamp_column(list(headers))
    if not ts_col:
        return
    composite = rule.get("source_composite_pk") or []
    composite_lower = [c.lower() for c in composite]
    if ts_col.lower() in composite_lower:
        return
    logger.info(
        "[A2] tacit rule %r: source_csv %r 에 시계열 컬럼 %r 감지됨. "
        "source_composite_pk 에 미포함 — source_pk_column 의 uniqueness 에 "
        "따라 자동 처리되지만 (unique=ts 생략, duplicate=ts append), "
        "정확한 의도 선언을 위해 source_composite_pk 명시를 권장합니다.",
        rule.get("name", "?"), src_csv, ts_col,
    )


def _warn_pk_diverges_from_abox(rule: dict) -> list[str]:
    """선언 PK 가 **A-Box 생성기가 고를 PK** 와 다른지 검사한다.

    ``_abox_pk_iri`` 는 A-Box IRI 규칙을 미러링하지만 **어느 컬럼이 PK 인가** 는
    미러링하지 않는다 — 그것은 룰이 직접 선언한다. 두 선택이 갈리면 같은 CSV 행에
    IRI 가 두 벌 생기고, tacit 쪽 IRI 는 ``rdf:type`` 이 없어 **유령 인스턴스**가
    된다.

    2026-08-30 실측 (``rule_electrical_consumption_to_equipment``)::

        선언  source_composite_pk = [Meter_ID, Timestamp]
              → ElectricalConsumption_MT001_2025-09-01_00_00_00   (rdf:type 없음)
        A-Box _detect_pk_column   = [equipment_id, timestamp]
              → ElectricalConsumption_EQ005_2025-09-01_00_00_00   (정상)

    피해: 유령 720개 + ``hasElectricalConsumptionEquipment`` 1,440건 중 절반이
    유령 주어. CQ05 가 정확히 2배로 답한다. Meter_ID·Equipment_ID 가 **둘 다
    우연히 720행 unique** 여서 uniqueness 선검사(A2)로는 잡히지 않았다.

    **왜 카운트 게이트가 침묵했나**: 유령은 ``rdf:type`` 이 없어 클래스별 인스턴스
    수에 안 잡히고, ``dangling_references`` 는 OP 의 *object* 만 보므로 *subject*
    가 미타입인 것을 보지 않는다.

    Returns: 경고 메시지 목록 (호출자가 응답에 실어 사람이 보게 한다).
    """
    src_csv = rule.get("source_csv")
    src_class = rule.get("source_class")
    if not src_csv or not src_class:
        return []
    declared = rule.get("source_composite_pk") or rule.get("source_pk_column")
    if not declared:
        return []
    try:
        _, rows = _read_csv_rows(src_csv)
    except Exception:
        return []
    if not rows:
        return []
    try:
        from tools.abox_generation import _detect_pk_column
        detected = _detect_pk_column(rows, src_class)
    except Exception:
        # 감지기를 못 불러오면 침묵한다 — 이 함수는 진단이고, 실패가 TTL 생성을
        # 막아서는 안 된다.
        return []
    if not detected:
        return []

    def _norm(v) -> list[str]:
        if isinstance(v, str):
            return [v.lower()]
        return [str(c).lower() for c in v]

    want, got = _norm(declared), _norm(detected)
    if want == got:
        return []
    msg = (
        f"tacit rule {rule.get('name', '?')!r}: 선언 PK {declared} 가 A-Box 감지 PK "
        f"{detected} 와 다릅니다 — 같은 행에 IRI 두 벌이 생겨 rdf:type 없는 유령 "
        f"인스턴스가 만들어집니다. source_composite_pk 를 {detected} 로 맞추세요."
    )
    logger.warning("[PK-DRIFT] %s", msg)
    return [msg]


def _generate_rotation(rule: dict, cache: dict) -> list[str]:
    """Strategy 1 — rotation. Map each source row to target PKs in round-robin."""
    _warn_composite_pk_missing(rule)
    src_csv = rule["source_csv"]
    src_class = rule["source_class"]
    src_pk = rule.get("source_pk_column")
    src_composite = rule.get("source_composite_pk")
    tgt_csv = rule["target_csv"]
    tgt_class = rule["target_class"]
    tgt_pk = rule["target_pk_column"]
    op = rule["op"]
    filter_column = rule.get("target_filter_column", "")
    filter_value = rule.get("target_filter_value", "")

    target_pks = _load_target_pks(tgt_csv, tgt_pk, filter_column, filter_value)
    if not target_pks:
        return []

    src_fields, src_rows = _read_csv_rows(src_csv)
    # A2 fix: when src_pk alone is unique in CSV, A-Box skips the ts suffix
    # (abox_generation._detect_pk_value pk_is_unique_single branch). Mirror it.
    auto_ts = not (
        src_pk and not src_composite and _is_pk_unique_in_csv(src_csv, src_pk)
    )
    lines: list[str] = []
    src_iris: dict[str, str] = {}
    for i, row in enumerate(src_rows):
        src_iri_local = _abox_pk_iri(
            src_class, row, src_pk, src_composite, auto_timestamp=auto_ts,
        )
        if not src_iri_local:
            continue
        src_key = src_iri_local[len(src_class) + 1:]  # strip "{class}_" prefix
        tgt_pk_val = target_pks[i % len(target_pks)]
        tgt_iri_local = f"{tgt_class}_{tgt_pk_val}"
        lines.append(
            f"{_ns_inst_iri(src_iri_local)} {NS_PREFIX}:{op} {_ns_inst_iri(tgt_iri_local)} ."
        )
        src_iris[src_key] = src_iri_local
    cache[rule["name"]] = {"src_iris": src_iris, "tgt_pool": target_pks}
    return lines


def _generate_number_match(rule: dict, cache: dict) -> list[str]:
    """Strategy 2 — pair by trailing integer in PK.

    Both source and target classes must have instances whose PKs end with
    a number (e.g. MP001, EQ001). When both n-values match, emit the triple.
    Two OPs may be emitted (forward + inverse-like) per source row.
    """
    _warn_composite_pk_missing(rule)
    src_csv = rule["source_csv"]
    src_class = rule["source_class"]
    src_pk = rule["source_pk_column"]
    tgt_class = rule["target_class"]
    tgt_pk_template = rule["target_pk_template"]  # e.g. "EQ{n:03d}"
    ops = rule["ops"] if isinstance(rule.get("ops"), list) else [rule["op"]]

    _, src_rows = _read_csv_rows(src_csv)
    lines: list[str] = []
    mapping: dict[str, str] = {}
    for row in src_rows:
        src_val = row.get(src_pk, "")
        n = _trailing_int(src_val)
        if n is None:
            continue
        tgt_val = _format_pk(tgt_pk_template, n)
        src_iri = f"{src_class}_{src_val}"
        tgt_iri = f"{tgt_class}_{tgt_val}"
        for op in ops:
            lines.append(
                f"{_ns_inst_iri(src_iri)} {NS_PREFIX}:{op} {_ns_inst_iri(tgt_iri)} ."
            )
        mapping[src_val] = tgt_val
    cache[rule["name"]] = {"mapping": mapping, "src_class": src_class, "tgt_class": tgt_class}
    return lines


def _generate_simple_join(rule: dict, cache: dict) -> list[str]:
    """Strategy 3 — direct FK column in source CSV points to target PK."""
    _warn_composite_pk_missing(rule)
    src_csv = rule["source_csv"]
    src_class = rule["source_class"]
    src_pk = rule.get("source_pk_column")
    src_composite = rule.get("source_composite_pk")
    fk_column = rule["source_fk_column"]
    tgt_class = rule["target_class"]
    op = rule["op"]

    _, src_rows = _read_csv_rows(src_csv)
    # A2 fix: mirror A-Box's unique-single-PK branch — no ts suffix when the
    # source_pk_column itself is already unique across the CSV.
    auto_ts = not (
        src_pk and not src_composite and _is_pk_unique_in_csv(src_csv, src_pk)
    )
    lines: list[str] = []
    for row in src_rows:
        fk_val = row.get(fk_column, "")
        if not fk_val:
            continue
        src_iri_local = _abox_pk_iri(
            src_class, row, src_pk, src_composite, auto_timestamp=auto_ts,
        )
        if not src_iri_local:
            continue
        tgt_iri_local = f"{tgt_class}_{fk_val}"
        lines.append(
            f"{_ns_inst_iri(src_iri_local)} {NS_PREFIX}:{op} {_ns_inst_iri(tgt_iri_local)} ."
        )
    return lines


def _generate_via_mapping_chain(rule: dict, cache: dict) -> list[str]:
    """Strategy 4 — compose an upstream number_match mapping with a FK lookup.

    Use when the source class has no direct FK to the target, but it links to
    an intermediate class via ``via_mapping`` (name of a previously-computed
    number_match rule), and the intermediate class has a direct FK to the
    target through ``intermediate_fk_csv``/``intermediate_fk_source_column``/
    ``intermediate_fk_target_column``.

    Concrete example (CQ08 shortcut):
      - source: WaterQualityMonitoring, has Point_ID
      - via_mapping: "point_equipment" (MP_N → EQ_N from number_match)
      - intermediate_fk_csv: Maintenance_History.csv (links Equipment_ID → Maintenance_ID)
      - emit: WQM_WS00001 hasMaintenanceHistoryForWQM MH_MNT00001
    """
    _warn_composite_pk_missing(rule)
    src_csv = rule["source_csv"]
    src_class = rule["source_class"]
    src_pk = rule.get("source_pk_column")
    src_composite = rule.get("source_composite_pk")
    src_link_column = rule["source_link_column"]  # e.g. Point_ID
    via = rule["via_mapping"]  # name of previous number_match rule
    inter_csv = rule.get("intermediate_fk_csv")  # optional — direct 1-hop if omitted
    inter_src_col = rule.get("intermediate_fk_source_column")
    inter_tgt_col = rule.get("intermediate_fk_target_column")
    tgt_class = rule["target_class"]
    op = rule["op"]

    upstream = cache.get(via)
    if not upstream or "mapping" not in upstream:
        logger.warning("via_mapping %r not found in cache; skip rule %r", via, rule["name"])
        return []
    mp_to_eq = upstream["mapping"]

    # Build intermediate index: eq_id → first target_id
    inter_index: dict[str, str] = {}
    if inter_csv and inter_src_col and inter_tgt_col:
        _, inter_rows = _read_csv_rows(inter_csv)
        for r in inter_rows:
            k = r.get(inter_src_col, "")
            v = r.get(inter_tgt_col, "")
            if k and v and k not in inter_index:
                inter_index[k] = v

    _, src_rows = _read_csv_rows(src_csv)
    # A2 fix: A-Box skips ts on unique single PKs — mirror for IRI parity.
    auto_ts = not (
        src_pk and not src_composite and _is_pk_unique_in_csv(src_csv, src_pk)
    )
    lines: list[str] = []
    for row in src_rows:
        link_val = row.get(src_link_column, "")
        mid = mp_to_eq.get(link_val)
        if not mid:
            continue
        if inter_index:
            tgt_pk = inter_index.get(mid)
            if not tgt_pk:
                continue
        else:
            tgt_pk = mid  # direct 1-hop: source → intermediate is the target
        src_iri_local = _abox_pk_iri(
            src_class, row, src_pk, src_composite, auto_timestamp=auto_ts,
        )
        if not src_iri_local:
            continue
        tgt_iri_local = f"{tgt_class}_{tgt_pk}"
        lines.append(
            f"{_ns_inst_iri(src_iri_local)} {NS_PREFIX}:{op} {_ns_inst_iri(tgt_iri_local)} ."
        )
    return lines


def _generate_fk_lookup_table(rule: dict, cache: dict) -> list[str]:
    """Strategy 5 — indirect FK via a lookup CSV.

    Source CSV has column X (e.g. Item_Code). A separate lookup CSV has the
    same X plus the wanted target PK (e.g. Item_Supplier_Map.Item_Code +
    Supplier_ID). Build X → target_pk index, then emit one triple per
    source row. Supports composite PK on the source side.

    Example (CQ10):
      source: Inventory_Status.csv, composite PK [Item_Code, Warehouse_Code]
      lookup: Item_Supplier_Map.csv, lookup_key=Item_Code, target=Supplier_ID
    """
    _warn_composite_pk_missing(rule)
    src_csv = rule["source_csv"]
    src_class = rule["source_class"]
    src_pk = rule.get("source_pk_column")
    src_composite = rule.get("source_composite_pk")
    src_join_column = rule["source_join_column"]
    lookup_csv = rule["lookup_csv"]
    lookup_key_column = rule["lookup_key_column"]
    lookup_value_column = rule["lookup_value_column"]
    tgt_class = rule["target_class"]
    op = rule["op"]

    # build index (first win) — lookup_key → target value
    _, lookup_rows = _read_csv_rows(lookup_csv)
    idx: dict[str, str] = {}
    for r in lookup_rows:
        k = r.get(lookup_key_column, "")
        v = r.get(lookup_value_column, "")
        if k and v and k not in idx:
            idx[k] = v
    if not idx:
        return []

    _, src_rows = _read_csv_rows(src_csv)
    # A2 fix: unique single src_pk → mirror A-Box's ts-skipping branch.
    auto_ts = not (
        src_pk and not src_composite and _is_pk_unique_in_csv(src_csv, src_pk)
    )
    lines: list[str] = []
    for row in src_rows:
        jk = row.get(src_join_column, "")
        tgt_pk = idx.get(jk)
        if not tgt_pk:
            continue
        # R26: A-Box 와 동일한 IRI 규칙 사용 (composite PK + timestamp 정규화)
        src_iri_local = _abox_pk_iri(
            src_class, row, src_pk, src_composite, auto_timestamp=auto_ts,
        )
        if not src_iri_local:
            continue
        tgt_iri_local = f"{tgt_class}_{tgt_pk}"
        lines.append(
            f"{_ns_inst_iri(src_iri_local)} {NS_PREFIX}:{op} {_ns_inst_iri(tgt_iri_local)} ."
        )
    return lines


def _resolve_bucket_index(
    csv_filename: str, key_column: str, bucket_column: str,
) -> dict[str, str]:
    """Build ``key_column value → bucket_column value`` (first-win) from a CSV.

    Used by ``shared_column_join`` to translate a side's join key (e.g. an
    Equipment_ID) into its bucket value (e.g. a Location) when the bucket is
    not present on the source row directly. First-win mirrors
    ``_generate_fk_lookup_table`` — a master CSV has one bucket per key.
    """
    _, rows = _read_csv_rows(csv_filename)
    idx: dict[str, str] = {}
    for r in rows:
        k = r.get(key_column, "")
        v = r.get(bucket_column, "")
        if k and v and k not in idx:
            idx[k] = v
    return idx


def _generate_shared_column_join(rule: dict, cache: dict) -> list[str]:
    """Strategy 6 — many-to-many join on a shared bucket value.

    Unlike the five 1:1 strategies, this links every source instance to *all*
    target instances that share the same bucket value (e.g. plant Location /
    area). Each side derives its bucket either directly from a row column or
    indirectly through a single-hop lookup CSV (master table). Use when no
    direct FK exists but co-location (same area) is a meaningful relation an
    SME confirms — the resulting edges mean "co-located in the same bucket",
    not a precise 1:1 reference.

    Rule fields::

        source_csv, source_class, source_pk_column|source_composite_pk,
        target_csv, target_class, target_pk_column|target_composite_pk,
        op, output_file,
        # bucket resolution (per side): either a direct column or a lookup
        source_bucket_column            # bucket value lives on the source row
          OR  source_join_column + source_lookup_csv
              + source_lookup_key_column + source_lookup_bucket_column
        target_bucket_column            # bucket value lives on the target row
          OR  target_join_column + target_lookup_csv
              + target_lookup_key_column + target_lookup_bucket_column

    Concrete example (CQ10 gasEnergyAtMonitoringPoint):
        source: Gas_Energy.csv (Equipment_ID) → Equipment_Master.csv (Location)
        target: Monitoring_Point_Master.csv (Location directly)
        emit: every GasEnergy ↔ MonitoringPointMaster sharing a Location.
    """
    _warn_composite_pk_missing(rule)
    op = rule["op"]

    def _side_buckets(prefix: str, csv_filename: str, class_name: str,
                      pk_col: str | None, composite_pk: list[str] | None,
                      ) -> dict[str, list[str]]:
        """Return ``bucket value → [instance IRI local names]`` for one side."""
        direct_col = rule.get(f"{prefix}_bucket_column")
        join_col = rule.get(f"{prefix}_join_column")
        lookup_idx: dict[str, str] = {}
        if not direct_col:
            lookup_idx = _resolve_bucket_index(
                rule[f"{prefix}_lookup_csv"],
                rule[f"{prefix}_lookup_key_column"],
                rule[f"{prefix}_lookup_bucket_column"],
            )
        auto_ts = not (
            pk_col and not composite_pk and _is_pk_unique_in_csv(csv_filename, pk_col)
        )
        buckets: dict[str, list[str]] = {}
        _, rows = _read_csv_rows(csv_filename)
        for row in rows:
            if direct_col:
                bucket = row.get(direct_col, "")
            else:
                bucket = lookup_idx.get(row.get(join_col, ""), "")
            if not bucket:
                continue
            iri_local = _abox_pk_iri(
                class_name, row, pk_col, composite_pk, auto_timestamp=auto_ts,
            )
            if not iri_local:
                continue
            buckets.setdefault(bucket, []).append(iri_local)
        return buckets

    src_buckets = _side_buckets(
        "source", rule["source_csv"], rule["source_class"],
        rule.get("source_pk_column"), rule.get("source_composite_pk"),
    )
    tgt_buckets = _side_buckets(
        "target", rule["target_csv"], rule["target_class"],
        rule.get("target_pk_column"), rule.get("target_composite_pk"),
    )

    lines: list[str] = []
    for bucket, src_iris in src_buckets.items():
        tgt_iris = tgt_buckets.get(bucket)
        if not tgt_iris:
            continue
        for s in src_iris:
            for t in tgt_iris:
                lines.append(
                    f"{_ns_inst_iri(s)} {NS_PREFIX}:{op} {_ns_inst_iri(t)} ."
                )
    return lines


_STRATEGIES = {
    "rotation": _generate_rotation,
    "number_match": _generate_number_match,
    "simple_join": _generate_simple_join,
    "via_mapping_chain": _generate_via_mapping_chain,
    "fk_lookup_table": _generate_fk_lookup_table,
    "shared_column_join": _generate_shared_column_join,
}


#: Strategies that join on a *shared bucket* rather than a foreign key. The
#: resulting links are co-location inferences, not recorded facts — every
#: source row pairs with every target row in the same bucket.
_ESTIMATED_STRATEGIES = frozenset({"shared_column_join"})

#: Confidence values that must be surfaced in the artifact. "high" needs no
#: caveat; anything less is an assumption a consumer has to know about.
_CAVEAT_CONFIDENCE = frozenset({"medium", "low", "unspecified"})


def _confidence_provenance(rule: dict, strategy: str) -> list[str]:
    """Comment lines that carry the rule's confidence into the TTL artifact.

    2026-08-28 실측: ``tacit_rules.json`` 의 여섯 ``shared_column_join`` 규칙은
    ``_confidence: medium`` + "SME 확인 필요" 를 기록하고 있었는데, 생성된 TTL 에는
    규칙 이름 한 줄만 남아 **그 신뢰도가 소비자에게 전달되지 않았다**.

    피해는 0행이 아니라 그럴듯한 오답이다. ``hasStackEquipment`` 는 이름이 "굴뚝
    설비" 인데 실제로는 "같은 구역 설비" 이고, 배출 측정 1건이 설비 7.15대와
    연결되며 distinct object 가 50 = **전체 설비**다 (판별력 없음). 'CQ06 배출
    최다 설비 top-5' 는 같은 구역 7대를 동점으로 받아 임의 절단된다.

    더 정확한 조인으로 교체할 수는 없다 — ``Air_Emission_Monitoring.Stack_ID``
    (Stack_1~5) 를 ``Equipment_Master`` 로 잇는 컬럼이 CSV 에 없다(실측 헤더:
    Equipment_ID, Equipment_Name, Equipment_Type, Location, Manufacturer). 즉
    정확한 매핑은 **데이터에 없다**. 그래서 조치는 추정을 지우는 것이 아니라
    추정임을 명시하는 것이다 — 값을 데이터에 맞춰 조작하면 지표 매수가 된다.
    """
    confidence = str(rule.get("_confidence") or "unspecified").strip().lower()
    estimated = strategy in _ESTIMATED_STRATEGIES
    if not estimated and confidence not in _CAVEAT_CONFIDENCE:
        return []

    out = [f"#   confidence: {confidence}"]
    if estimated:
        out.append(
            "#   ESTIMATED: bucket join, not a recorded FK — every source row "
            "links to every target row sharing the bucket. Do not read these "
            "as measured facts; ranking/top-N over them ties arbitrarily."
        )
        bucket = (
            rule.get("source_bucket_column")
            or rule.get("source_lookup_bucket_column")
            or rule.get("target_bucket_column")
            or "?"
        )
        out.append(f"#   bucket column: {bucket}")
    reasoning = str(rule.get("_reasoning") or "").strip()
    if reasoning:
        out.append(f"#   reasoning: {reasoning[:300]}")
    return out


def _render_ttl_file(header_lines: list[str], body_lines: list[str]) -> str:
    prefix_block = [
        f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .",
        f"@prefix {NS_INST_PREFIX}: <{DOMAIN_INST_NS}> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "",
    ]
    all_lines = prefix_block + header_lines + [""] + body_lines
    # 각 규칙 블록이 빈 줄로 끝나므로 마지막 블록의 빈 줄이 파일 말미에 남아
    # 이중 개행이 된다. pre-commit 의 end-of-file-fixer 가 그것을 매번 되돌려
    # 재생성마다 diff 가 생기므로(실측 2026-08-28, tacit TTL 7개) 여기서 정리한다.
    while all_lines and not all_lines[-1].strip():
        all_lines.pop()
    return "\n".join(all_lines) + "\n"


def generate_tacit_from_rules(rules_path: str = "") -> str:
    """Read ``rules/domain/tacit_rules.json`` and emit deterministic tacit TTL files.

    Deterministic — no LLM. Re-running regenerates identical output given the
    same CSVs. Use this in place of ``generate_tacit_from_data`` whenever the
    domain knowledge can be expressed as a rule (number matching, FK chains,
    rotation), which is the common case once an SME has reviewed the first
    pass.

    Supported strategies: rotation, number_match, simple_join, via_mapping_chain.
    See module docstring for descriptions.

    Rules JSON shape (abridged)::

        {
          "mappings": [
            {
              "name": "point_equipment",
              "strategy": "number_match",
              "source_csv": "Monitoring_Point_Master.csv",
              "source_class": "MonitoringPointMaster",
              "source_pk_column": "Point_ID",
              "target_class": "EquipmentMaster",
              "target_pk_template": "EQ{n:03d}",
              "ops": ["pointLocatesEquipment", "pointMonitorsEquipment"],
              "output_file": "point_equipment_mapping.ttl"
            },
            ...
          ]
        }

    Multiple rules sharing the same ``output_file`` are concatenated into one
    file. Each rule can reference a previous rule by ``via_mapping`` to build
    composed shortcuts.

    Args:
        rules_path: Optional override path to the rules JSON. Defaults to
            rules/domain/tacit_rules.json at project root.

    Returns:
        JSON with per-rule triples_written and per-file summary.
    """
    try:
        path = rules_path or _RULES_PATH
        if not os.path.exists(path):
            return error_response(
                f"rules file not found: {path}",
                hint="Create rules/domain/tacit_rules.json (see docstring).",
                logger=logger,
            )
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        mappings = cfg.get("mappings", [])
        if not mappings:
            # 빈 mappings 는 정상 (아직 규칙 미작성 상태). 에러가 아닌 no-op 로 반환.
            return json.dumps({
                "success": True,
                "rules_applied": 0,
                "rules_with_output": 0,
                "files_written": [],
                "per_rule": [],
                "note": (
                    "rules/domain/tacit_rules.json 의 mappings 가 비어 있습니다. "
                    "신규 도메인 적용 시: (A) rules/tacit_rules.example.steel.json 참고해 "
                    "직접 작성, (B) suggest_tacit_rules 도구로 LLM 초안 받기, "
                    "(C) skip 후 (a) 자연어 경로만 사용."
                ),
            }, ensure_ascii=False, indent=2)

        os.makedirs(SOURCE_TACIT_DIR, exist_ok=True)

        # 2026-05-10 회귀 수정: rule 의 (source_class, op, target_class) 방향이
        # T-Box domain/range 와 맞는지 사전 검증. 반대 방향이면 inverse op 로
        # 자동 교정, 교정 불가 rule 은 skip + warning.
        tbox_ttl = ""
        try:
            from config import TBOX_PATH
            if os.path.exists(TBOX_PATH):
                with open(TBOX_PATH, encoding="utf-8") as _tf:
                    tbox_ttl = _tf.read()
        except Exception as _e:
            logger.warning("T-Box load failed (op validation skipped): %s", _e)

        cache: dict[str, Any] = {}  # rule_name → intermediate data for chains
        per_file: dict[str, list[str]] = {}
        per_rule_counts: list[dict] = []
        op_corrections: list[dict] = []  # audit log for inverse auto-corrections
        # 선언 PK 가 A-Box 감지 PK 와 갈리면 같은 행에 IRI 두 벌이 생겨 rdf:type
        # 없는 유령 인스턴스가 만들어진다. 로그만 남기면 아무도 보지 않으므로
        # 응답에 실어 올린다 (_warn_pk_diverges_from_abox docstring 참조).
        pk_drift_warnings: list[str] = []

        for rule in mappings:
            strat_name = rule.get("strategy")
            strat = _STRATEGIES.get(strat_name or "")
            if not strat:
                per_rule_counts.append({
                    "name": rule.get("name", "?"),
                    "strategy": strat_name,
                    "error": f"unknown strategy {strat_name!r}",
                    "triples": 0,
                })
                continue

            # Pre-validate rule op direction — strategy 가 requires 단일 "op"
            # 필드인 경우 (rotation, simple_join, via_mapping_chain, fk_lookup_table).
            # number_match 는 "ops" 리스트 — 개별 검증.
            # 도메인 중립성: DOMAIN_NS 대신 rules/domain/domain_config.json 의 DOMAIN_NS
            # 를 prefix 로 사용해야 non-steel 도메인에서도 validator 가 동작.
            validation_error: str | None = None
            if tbox_ttl and rule.get("source_class") and rule.get("target_class"):
                from tools.tacit_op_validator import validate_op_direction
                _domain_prefix = str(DOMAIN_NS)  # DOMAIN_NS = DOMAIN_NS — domain_config.json 기반
                if "ops" in rule and isinstance(rule["ops"], list):
                    # number_match: 모든 ops 검증 + 교정
                    corrected_ops: list[str] = []
                    for _orig_op in rule["ops"]:
                        _vr = validate_op_direction(
                            op=_orig_op,
                            source_class=rule["source_class"],
                            target_class=rule["target_class"],
                            tbox_ttl=tbox_ttl,
                            domain_prefix=_domain_prefix,
                        )
                        if not _vr["valid"]:
                            validation_error = (
                                f"op '{_orig_op}' invalid: {_vr['reason']}"
                            )
                            break
                        if _vr["corrected"]:
                            op_corrections.append({
                                "rule": rule.get("name", "?"),
                                "original_op": _vr["original_op"],
                                "corrected_op": _vr["op"],
                                "reason": _vr["reason"],
                            })
                        corrected_ops.append(_vr["op"])
                    if validation_error is None:
                        rule = {**rule, "ops": corrected_ops}
                elif "op" in rule:
                    _vr = validate_op_direction(
                        op=rule["op"],
                        source_class=rule["source_class"],
                        target_class=rule["target_class"],
                        tbox_ttl=tbox_ttl,
                        domain_prefix=_domain_prefix,
                    )
                    if not _vr["valid"]:
                        validation_error = (
                            f"op '{rule['op']}' invalid: {_vr['reason']}"
                        )
                    else:
                        if _vr["corrected"]:
                            op_corrections.append({
                                "rule": rule.get("name", "?"),
                                "original_op": _vr["original_op"],
                                "corrected_op": _vr["op"],
                                "reason": _vr["reason"],
                            })
                        rule = {**rule, "op": _vr["op"]}

            if validation_error is not None:
                logger.warning(
                    "rule %s skipped (op direction invalid): %s",
                    rule.get("name"), validation_error,
                )
                per_rule_counts.append({
                    "name": rule.get("name", "?"),
                    "strategy": strat_name,
                    "error": validation_error,
                    "triples": 0,
                })
                continue

            pk_drift_warnings.extend(_warn_pk_diverges_from_abox(rule))

            try:
                lines = strat(rule, cache)
            except Exception as e:
                logger.warning("rule %s failed: %s", rule.get("name"), e)
                per_rule_counts.append({
                    "name": rule.get("name", "?"),
                    "strategy": strat_name,
                    "error": str(e),
                    "triples": 0,
                })
                continue

            out_file = rule.get("output_file", "rules_generated.ttl")
            header = per_file.setdefault(out_file, [])
            if lines:
                header.append(
                    f"# --- rule: {rule.get('name', '?')} (strategy={strat_name}) ---"
                )
                header.extend(_confidence_provenance(rule, strat_name))
                header.extend(lines)
                header.append("")
            per_rule_counts.append({
                "name": rule.get("name", "?"),
                "strategy": strat_name,
                "output_file": out_file,
                "triples": len(lines),
                "confidence": (rule.get("_confidence") or "unspecified"),
            })

        files_written: list[dict] = []
        for out_file, content_lines in per_file.items():
            ttl = _render_ttl_file([
                "# Auto-generated by generate_tacit_from_rules (deterministic).",
                # 출처를 리터럴로 박으면 파일이 옮겨질 때 조용히 낡는다 (실측:
                # rules/ 하위폴더 분리 후 추적 TTL 7개 전부가 존재하지 않는
                # 경로를 출처로 각인하고 있었다). 실제 읽은 경로에서 파생한다.
                f"# Source: {os.path.relpath(_RULES_PATH, PROJECT_ROOT)}",
                "# Do NOT hand-edit — rerun the tool instead.",
            ], content_lines)
            dest = resolve_child_path(
                SOURCE_TACIT_DIR,
                out_file,
                allowed_suffixes=(".ttl",),
            )
            atomic_write(dest, ttl)
            files_written.append({
                "path": dest,
                "filename": out_file,
                "size_chars": len(ttl),
            })

        return json.dumps({
            "success": True,
            "rules_applied": len(mappings),
            "rules_with_output": sum(1 for r in per_rule_counts if r.get("triples", 0) > 0),
            "files_written": files_written,
            "per_rule": per_rule_counts,
            "op_corrections": op_corrections,
            "op_corrections_count": len(op_corrections),
            # 비어 있어야 정상. 1건이라도 있으면 그 룰이 유령 인스턴스를 만들고
            # 있으므로 CQ 조인이 중복 계산된다.
            "pk_drift_warnings": pk_drift_warnings,
            "pk_drift_count": len(pk_drift_warnings),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


# ── suggest_tacit_rules (LLM 기반 초안 제안) ─────────────


def _collect_csv_headers_summary(max_tables: int = 40) -> str:
    """CSV 디렉토리의 테이블 헤더 요약을 LLM 프롬프트용 텍스트로 반환."""
    text, _ = _collect_csv_headers_with_map(max_tables)
    return text


def _collect_csv_headers_with_map(
    max_tables: int = 40,
) -> tuple[str, dict[str, list[str]]]:
    """Return both the prompt text and a {filename: headers} map.

    The map is used by ``_build_timeseries_prompt_section`` to detect
    time-series tables and warn the LLM to include composite PK columns.
    """
    lines: list[str] = []
    csv_map: dict[str, list[str]] = {}
    try:
        if not os.path.isdir(SOURCE_RAWDATA_DIR):
            return "(CSV 디렉토리 없음)", {}
        files = sorted(
            f for f in os.listdir(SOURCE_RAWDATA_DIR)
            if f.lower().endswith(".csv")
        )
        for fname in files[:max_tables]:
            path = os.path.join(SOURCE_RAWDATA_DIR, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    header = next(csv.reader(f), [])
                lines.append(f"- {fname}: {', '.join(header)}")
                csv_map[fname] = list(header)
            except Exception:  # noqa: BLE001 — 읽을 수 없는 CSV 헤더는 후보 목록에서 제외한다
                continue
    except Exception as e:
        logger.debug("CSV 헤더 수집 실패: %s", e)
    text = "\n".join(lines) if lines else "(CSV 없음)"
    return text, csv_map


def _collect_tbox_classes_ops() -> tuple[list[str], list[tuple[str, str, str]]]:
    """T-Box 에서 steel-ns 클래스 목록과 (op, domain, range) 튜플을 반환."""
    from config import TBOX_PATH
    if not os.path.exists(TBOX_PATH):
        return [], []
    from rdflib import OWL, RDF, RDFS, Graph, URIRef
    g = Graph()
    try:
        g.parse(TBOX_PATH, format="turtle")
    except Exception as e:
        logger.debug("T-Box 파싱 실패: %s", e)
        return [], []
    steel_str = str(DOMAIN_NS)
    classes = sorted(
        str(c).replace(steel_str, "") for c in g.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    )
    ops: list[tuple[str, str, str]] = []
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
            continue
        op_local = str(op).replace(steel_str, "")
        dom = next(
            (str(d).replace(steel_str, "") for d in g.objects(op, RDFS.domain)
             if isinstance(d, URIRef) and str(d).startswith(steel_str)),
            "?",
        )
        rng = next(
            (str(r).replace(steel_str, "") for r in g.objects(op, RDFS.range)
             if isinstance(r, URIRef) and str(r).startswith(steel_str)),
            "?",
        )
        ops.append((op_local, dom, rng))
    return classes, sorted(ops)


def _collect_cq_domains() -> list[dict]:
    """CQ 파일에서 id + domains + question_ko 요약 반환."""
    from config import COMPETENCY_QUESTIONS_PATH
    if not os.path.exists(COMPETENCY_QUESTIONS_PATH):
        return []
    try:
        with open(COMPETENCY_QUESTIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("questions") if isinstance(data, dict) else data
        out: list[dict] = []
        for cq in (items or []):
            out.append({
                "id": cq.get("id", "?"),
                "domains": cq.get("domains", []),
                "question": (cq.get("question_ko") or cq.get("question") or "")[:200],
            })
        return out
    except Exception as e:
        logger.debug("CQ 로드 실패: %s", e)
        return []


_SUGGEST_RULES_PROMPT = """당신은 온톨로지 엔지니어입니다. 다음 정보를 바탕으로
`rules/domain/tacit_rules.json` 의 초안 mappings 배열을 JSON 으로 제안하세요.

## 목적
CSV 에 없지만 **현장 운영자가 아는 암묵지** (공정 흐름, FK 경유 관계,
cross-domain shortcut) 를 결정적 규칙으로 materialize 해 CQ 질의가
실제 SPARQL 조인으로 답변 가능하게 만듭니다.

## 사용 가능 strategy (5개)
1. **number_match** — PK 끝자리 숫자로 쌍 매칭 (예: MP001 ↔ EQ001)
   필수 필드: source_csv, source_class, source_pk_column, target_class,
   target_pk_template (예: "EQ{{n:03d}}"), ops (list), output_file.

2. **rotation** — source 행을 target PK 배열에 라운드로빈 할당
   필수: source_csv, source_class, source_pk_column, target_csv,
   target_class, target_pk_column, op, output_file.
   옵션: target_filter_column, target_filter_value (필터링).

3. **simple_join** — source CSV 의 FK 컬럼을 바로 target PK 로
   필수: source_csv, source_class, source_pk_column (또는 source_composite_pk),
   source_fk_column, target_class, op, output_file.

4. **fk_lookup_table** — lookup CSV 경유해 target PK 조회 (composite PK 지원)
   필수: source_csv, source_class, source_pk_column (또는 source_composite_pk),
   source_join_column, lookup_csv, lookup_key_column, lookup_value_column,
   target_class, op, output_file.

5. **via_mapping_chain** — 이전 number_match 결과 재사용해 2-hop shortcut
   필수: source_csv, source_class, source_pk_column (또는 source_composite_pk),
   source_link_column, via_mapping (이전 rule name), target_class, op,
   output_file.
   옵션: intermediate_fk_csv, intermediate_fk_source_column,
   intermediate_fk_target_column.

## 입력 데이터

### CSV 테이블 헤더
{csv_headers}

### T-Box 클래스 (일부 — {class_count} 중 상위)
{class_list}

### T-Box ObjectProperty (일부 — {op_count} 중 상위)
{op_list}

### Competency Questions (CQ — 답해야 할 질문 {cq_count} 개)
{cq_list}

## 요청사항
1. CQ 의 domains 필드에서 2개 이상 도메인이 조인되는 케이스를 식별.
2. 각 조인에 필요한 CSV ↔ T-Box 경로를 분석하여 적합한 strategy 선택.
3. 각 mapping 의 필드 값 (source_csv, source_class 등) 을 실제 CSV 이름과
   T-Box 클래스 이름과 정확히 일치시켜 작성.
4. op 이름은 기존 T-Box ObjectProperty 중 하나를 재사용하거나, 새 의미
   있는 camelCase 이름 제안.
5. output_file 은 같은 테마의 규칙들을 한 TTL 로 묶어도 됨.

## ⚠ CRITICAL — OP 방향 제약 (반드시 준수)
생성되는 TTL 은 `<source_iri> <op> <target_iri>` 형식입니다.
따라서 **op 의 domain/range 는 source/target 과 정확히 일치해야** 합니다:
  - `source_class` 는 `op 의 domain` (또는 그 하위 클래스) 이어야 함
  - `target_class` 는 `op 의 range` (또는 그 하위 클래스) 이어야 함

잘못된 예 (**이렇게 쓰면 자동 교정 대상**):
  - rule: source=TagMaster, target=EquipmentMaster, op=equipmentHasTag
  - T-Box: equipmentHasTag (domain=EquipmentMaster, range=TagMaster)
  - 문제: source/target 과 op 의 domain/range 가 **반대**
  - 올바른 선택: op=tagBelongsToEquipment (domain=TagMaster, range=EquipmentMaster)

올바른 예:
  - rule: source=TagMaster, target=EquipmentMaster
  - 위 "T-Box ObjectProperty" 리스트에서 `(TagMaster → EquipmentMaster)` 로
    표기된 op 선택 (예: tagBelongsToEquipment, tagMonitorsEquipment)

**규칙 작성 전 각 op 의 `(domain → range)` 를 확인**하고, source/target 순서와
일치하는 op 만 쓰세요. 기존 T-Box 에 맞는 op 가 없으면 새 op 이름을 제안하되
`_new_op_domain` / `_new_op_range` 필드에 의도한 domain/range 를 명시하세요.

## 출력 형식
순수 JSON 만 출력. 마크다운 코드 블록 없이. `{{"mappings": [...]}}` 루트 객체.
필수 필드 누락 금지. 확신이 낮은 규칙은 각 mapping 의 `_confidence` 필드에
"low"/"medium"/"high" 로 표시하고 `_reasoning` 필드에 이유 한 줄.

불확실하면 규칙 개수를 적게 (3~5개) 제안하고 사용자에게 검토를 맡기세요.
"""


def _recover_truncated_mappings_json(json_text: str) -> str | None:
    """LLM 응답이 `"mappings": [` 안에서 끊긴 경우 완성된 object 까지만 잘라 재구성.

    전략:
    1. `"mappings": [` 의 시작 위치를 찾는다.
    2. 배열 안에서 top-level `{` 를 세며 matched `}` 직후 위치를 기록 (완성된 object 끝).
    3. 가장 마지막 완성 object 끝 위치에서 잘라 `]}` 로 닫는다.

    반환: 파싱 가능한 JSON 문자열 또는 None (복구 불가).
    """
    arr_start = json_text.find('"mappings"')
    if arr_start < 0:
        return None
    bracket_open = json_text.find("[", arr_start)
    if bracket_open < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    last_complete_end = -1  # 마지막 완성된 object 를 닫은 `}` 의 index (exclusive 잘라낼 끝)
    i = bracket_open + 1
    while i < len(json_text):
        ch = json_text[i]
        if escape:
            escape = False
        elif ch == "\\" and in_string:
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    # 상위 배열의 직계 자식 object 하나 완성
                    last_complete_end = i + 1
            elif ch == "]" and depth == 0:
                # 원래 배열이 정상 종료된 경우 — 복구 불필요 (호출자가 이미 실패한 맥락)
                break
        i += 1

    if last_complete_end < 0:
        return None
    # 배열을 닫고 객체를 닫는다.
    reconstructed = json_text[:last_complete_end] + "\n  ]\n}"
    return reconstructed


def suggest_tacit_rules(max_rules: int = 8, save_to_suggested: bool = True) -> str:
    """LLM 으로 rules/domain/tacit_rules.json 의 초안 mappings 를 제안한다.

    **위치 (S5 선택지 중 b 보조):**
      - (a) add_tacit_from_natural_language — SME 가 말로 번역
      - (b) generate_tacit_from_rules — 결정적 TTL 생성 (이 파일의 mappings 실행)
      - **이 도구** — (b) 를 쓰고 싶은데 mappings 가 비어있을 때 LLM 초안 받기
      - (c) generate_tacit_from_data — LLM 이 자유형 TTL 직접 생성 (비결정적)
      - (d) skip

    ## 사용 흐름
    1. 이 도구 호출 → `rules/tacit_rules.suggested.json` 생성 + preview 반환
    2. 사용자가 파일 열어 SME 관점에서 검토 → 각 mapping 의 _confidence, _reasoning 확인
    3. 검증 완료한 규칙만 `rules/domain/tacit_rules.json` 의 mappings 배열로 복사
    4. `generate_tacit_from_rules()` 호출해 실제 TTL 생성

    ## 결정적 vs LLM
    규칙 **생성** 은 LLM (비결정적), 규칙 **적용** 은 결정적.
    즉 초안은 매번 다를 수 있지만 검증된 규칙이 tacit_rules.json 에 들어가면
    이후 파이프라인은 재현 가능.

    Args:
        max_rules: 초안 최대 규칙 수 (기본 8).
        save_to_suggested: True (기본) 면 rules/tacit_rules.suggested.json 에 저장.
                            False 면 preview 만 반환.

    Returns:
        JSON — success, mappings_count, suggested_path, preview (첫 3 규칙),
        sme_checklist (검토 포인트 리스트).
    """
    try:
        from tools.bedrock import invoke_bedrock_with_metadata

        csv_headers, csv_summary = _collect_csv_headers_with_map(max_tables=40)
        classes, ops = _collect_tbox_classes_ops()
        cqs = _collect_cq_domains()

        if not classes and not cqs:
            return error_response(
                "T-Box 도 CQ 도 없습니다. S2 T-Box 생성 + S0 CQ 생성 후 재시도하세요.",
                logger=logger,
            )

        # 상위 일부만 프롬프트에 포함 (토큰 제한)
        class_list = "\n".join(f"- {c}" for c in classes[:40])
        op_list = "\n".join(f"- {name} ({dom} → {rng})" for name, dom, rng in ops[:60])
        cq_list = "\n".join(
            f"- {cq['id']} [{', '.join(cq['domains'])}]: {cq['question']}"
            for cq in cqs
        )

        prompt = _SUGGEST_RULES_PROMPT.format(
            csv_headers=csv_headers,
            class_count=len(classes),
            class_list=class_list or "(클래스 없음)",
            op_count=len(ops),
            op_list=op_list or "(OP 없음)",
            cq_count=len(cqs),
            cq_list=cq_list or "(CQ 없음)",
        )

        # A2: 시계열 테이블 감지 시 LLM 에게 composite_pk 포함을 지시.
        # 감지 없으면 프롬프트에 영향 없음.
        ts_section = _build_timeseries_prompt_section(csv_summary)
        if ts_section:
            prompt = f"{prompt}\n\n{ts_section}"

        # max_tokens 산정: 규칙당 ~1,500 토큰 + 오버헤드 1,000. 8 규칙 = ~13K.
        # 기본 max_rules=8 기준 안전마진 포함해 16K. 상한 32K (Bedrock 제약 내).
        per_rule_tokens = 1500
        dynamic_max = min(32000, 1000 + max_rules * per_rule_tokens)

        logger.info(
            "suggest_tacit_rules — Bedrock 호출 (max_rules=%d, max_tokens=%d)",
            max_rules, dynamic_max,
        )
        bedrock_result = invoke_bedrock_with_metadata(
            prompt, max_tokens=dynamic_max, temperature=0.2,
        )
        raw = bedrock_result.get("text", "")
        stop_reason = bedrock_result.get("stop_reason", "")
        truncated = stop_reason == "max_tokens"
        if truncated:
            logger.warning(
                "suggest_tacit_rules — LLM 응답이 max_tokens=%d 에 도달해 잘림. "
                "truncation 복구 로직으로 완성된 mapping 까지만 추출 시도.",
                dynamic_max,
            )
        if isinstance(raw, (bytes, bytearray)):  # noqa: UP038  (pre-existing)
            raw = raw.decode("utf-8", errors="replace")
        raw = str(raw).strip()

        # JSON 추출 (마크다운 코드블록 방어)
        json_text = raw
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
        if m:
            json_text = m.group(1).strip()
        # preamble 제거 — { 로 시작하는 지점까지 trim
        first_brace = json_text.find("{")
        if first_brace > 0:
            json_text = json_text[first_brace:]
        # 마지막 } 이후 trim
        last_brace = json_text.rfind("}")
        if last_brace != -1:
            json_text = json_text[: last_brace + 1]

        parsed = None
        truncation_recovered = False
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as je:
            # Truncation 복구: 응답이 끊긴 경우, "mappings": [ 까지는 완성돼 있고
            # 그 안의 object 중 일부만 완전한 상태. 가장 마지막의 완성된 }] 위치까지
            # 잘라내 재구성.
            recovered = _recover_truncated_mappings_json(json_text)
            if recovered is not None:
                try:
                    parsed = json.loads(recovered)
                    truncation_recovered = True
                    logger.warning(
                        "suggest_tacit_rules — truncated JSON 에서 %d 개 mapping 복구",
                        len(parsed.get("mappings", [])),
                    )
                except json.JSONDecodeError:
                    parsed = None
            if parsed is None:
                return error_response(
                    f"LLM 응답 JSON 파싱 실패: {je}. max_rules 를 줄여 재시도하거나 "
                    "rules/tacit_rules.example.steel.json 을 참고해 직접 작성하세요.",
                    hint=f"응답 앞부분: {raw[:200]!r}",
                    logger=logger,
                )

        mappings = parsed.get("mappings", []) if isinstance(parsed, dict) else []
        if not isinstance(mappings, list):
            return error_response(
                "LLM 응답의 mappings 가 list 가 아닙니다.",
                logger=logger,
            )

        # max_rules 상한 적용
        mappings = mappings[:max_rules]

        # 2026-05-10 fix: write-time op direction 검증.
        # LLM 이 reversed op 를 제안하면 inverse 로 자동 교정 + annotation.
        # SME 가 검토하면서 교정 기록을 볼 수 있도록 원본 op 와 사유를 보존.
        auto_corrected = 0
        direction_warnings = 0
        tbox_ttl_for_validation = ""
        try:
            from config import TBOX_PATH as _TBOX_PATH
            if os.path.exists(_TBOX_PATH):
                with open(_TBOX_PATH, encoding="utf-8") as _tf:
                    tbox_ttl_for_validation = _tf.read()
        except Exception as _e:
            logger.warning("T-Box load for validation failed: %s", _e)

        if tbox_ttl_for_validation:
            from tools.tacit_op_validator import validate_op_direction
            _domain_prefix = str(DOMAIN_NS)
            for mapping in mappings:
                src_cls = mapping.get("source_class")
                tgt_cls = mapping.get("target_class")
                if not src_cls or not tgt_cls:
                    continue
                _ops_to_check = []
                if "ops" in mapping and isinstance(mapping["ops"], list):
                    _ops_to_check = [(i, op) for i, op in enumerate(mapping["ops"])]
                elif "op" in mapping:
                    _ops_to_check = [(None, mapping["op"])]
                for idx, op_name in _ops_to_check:
                    vr = validate_op_direction(
                        op=op_name,
                        source_class=src_cls,
                        target_class=tgt_cls,
                        tbox_ttl=tbox_ttl_for_validation,
                        domain_prefix=_domain_prefix,
                    )
                    if vr["corrected"]:
                        # inverse 로 교정 + audit annotation
                        if idx is None:
                            mapping["op"] = vr["op"]
                        else:
                            mapping["ops"][idx] = vr["op"]
                        mapping.setdefault("_auto_correction", {
                            "original_op": vr["original_op"],
                            "corrected_op": vr["op"],
                            "reason": vr["reason"],
                        })
                        auto_corrected += 1
                    elif not vr["valid"]:
                        # 교정 불가 — SME 검토용 경고만
                        mapping.setdefault("_direction_warning", vr["reason"])
                        direction_warnings += 1

        output = {
            "mappings": mappings,
            "_source": "suggest_tacit_rules (LLM bootstrap)",
            "_sme_review_required": True,
            "_note": (
                "이 파일은 LLM 초안입니다. SME 검토 후 검증된 규칙만 "
                "rules/domain/tacit_rules.json 의 mappings 배열로 옮겨 적용하세요. "
                "generate_tacit_from_rules 는 rules/domain/tacit_rules.json (이 파일 아님) 을 읽습니다."
            ),
        }
        if auto_corrected or direction_warnings:
            output["_validation_note"] = (
                f"{auto_corrected} 개 op 가 reversed → inverse 로 자동 교정됨 "
                f"(_auto_correction 필드 참조). "
                f"{direction_warnings} 개 op 는 교정 불가 — _direction_warning 확인."
            )
        if truncated or truncation_recovered:
            output["_truncation_note"] = (
                f"LLM 응답이 max_tokens={dynamic_max} 에 도달해 JSON 이 중간에 끊김. "
                f"완성된 mapping {len(mappings)} 개만 복구돼 저장됨. "
                "필요하면 max_rules 를 줄여 재호출하거나 이 파일을 기반으로 직접 보강하세요."
            )

        suggested_path = None
        if save_to_suggested:
            suggested_path = _SUGGESTED_RULES_PATH
            atomic_write(
                suggested_path,
                json.dumps(output, ensure_ascii=False, indent=2),
            )

        return json.dumps({
            "success": True,
            "mappings_count": len(mappings),
            "suggested_path": suggested_path,
            "truncated": truncated,
            "truncation_recovered": truncation_recovered,
            "preview": mappings[:3],  # 첫 3개만 미리보기
            "validation_summary": {
                "total": len(mappings),
                "auto_corrected": auto_corrected,
                "direction_warnings": direction_warnings,
            },
            "sme_checklist": [
                "각 mapping 의 source_csv / source_class 가 실제 CSV/T-Box 이름과 일치하는가?",
                "_confidence: low/medium 규칙을 우선 검토 또는 제거",
                "_reasoning 이 도메인 지식과 맞는가?",
                "op 이름이 기존 T-Box 와 충돌하지 않는가?",
                "_auto_correction 필드가 있으면 교정 기록 확인 후 수용 여부 결정",
                "_direction_warning 필드가 있으면 op 이름/방향을 재검토",
                "composite PK 필요 여부 (시계열 테이블은 Timestamp 포함)",
                "검증된 규칙만 rules/domain/tacit_rules.json 으로 복사 후 generate_tacit_from_rules 실행",
            ],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


__all__ = (
    "augment_csv_fk",
    "generate_tacit_from_rules",
    "suggest_tacit_rules",
)
