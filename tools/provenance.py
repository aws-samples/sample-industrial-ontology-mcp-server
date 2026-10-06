"""PROV-O 출처 정보 생성 (작업코드 P2 — docs/reference/task-glossary.md).

KG 파이프라인 각 단계(A-Box 생성, 추론, LPG 변환)에서 만들어진 결과에
W3C PROV-O 메타데이터를 RDF로 매립한다. JSON 사이드카를 RDF로 격상시켜
SPARQL로 "왜 X가 생성됐는가?"를 역추적 가능하게 한다.

설계 원칙:
- 트리플 폭발 방지: 개별 추론 트리플마다 Activity를 만들지 않고
  규칙별로 **공용 Activity** 하나 + named rule으로 분류
- Agent: 도구명 + 버전 (예: owlrl 7.1.4)
- Entity: rule instance URI (예: steel-prov:rule_eq-p)
- Activity: step (예: steel-prov:act_inference_2026-04-18T00:00:00Z)

I2 per-triple reification (2026-05):
- 기존 rule-level 요약에 더해 각 추론 triple 을 rdf:Statement reification 으로
  매립해 SPARQL 로 "이 추론 triple 의 유도 규칙은?" 즉석 조회 가능.
- URI 스킴: urn:inferred-stmt:<sha256[:12]>, urn:source-stmt:<..>, urn:tbox-axiom:<..>.
- prerequisites 는 _build_justifications_from_graphs 가 설명 문자열로 생성하므로
  (예: "(s, p, o) in pre"), 정확한 triple 포맷 파싱 대신 steel-prov:prerequisiteNote
  리터럴로 보존 — 기계 추론 체인 재구성은 별도 과제 (scope 외).
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime

from rdflib import XSD, Graph, Literal, URIRef
from rdflib.namespace import RDF

from config import ABOX_PATH, GENERATED_DIR, INFERRED_PATH
from domain.namespaces import DOMAIN_NS_OBJ, PROV
from domain.tbox_utils import _new_graph
from tools.common import error_response, resolve_path_within, success_response

logger = logging.getLogger(__name__)


# 파이프라인 단계별 Agent/Activity URI
PROV_BASE = str(DOMAIN_NS_OBJ).rstrip("#") + "-prov#"


# I2: per-triple reification URI prefix 상수
_INFERRED_STMT_PREFIX = "urn:inferred-stmt:"
_SOURCE_STMT_PREFIX = "urn:source-stmt:"
_TBOX_AXIOM_PREFIX = "urn:tbox-axiom:"


def _make_activity_uri(stage: str, timestamp: str) -> URIRef:
    safe_ts = timestamp.replace(":", "-").replace(".", "-")
    return URIRef(f"{PROV_BASE}act_{stage}_{safe_ts}")


def _make_agent_uri(tool_name: str, version: str) -> URIRef:
    return URIRef(f"{PROV_BASE}agent_{tool_name}_{version}")


def _make_rule_uri(rule_id: str) -> URIRef:
    safe = rule_id.replace(" ", "_").replace("/", "_")
    return URIRef(f"{PROV_BASE}rule_{safe}")


# ── I2 per-triple reification 헬퍼 ──────────────────────────────


def _stable_triple_hash(s, p, o) -> str:
    """Triple (s, p, o) → 결정적 해시 (SHA256 12자 prefix).

    동일 (s, p, o) 는 항상 동일 해시 → reification URI 재현성 보장.
    특수문자/공백/URI 구분자 안전 (utf-8 인코딩 후 해시).
    """
    key = f"{s}||{p}||{o}".encode()
    return hashlib.sha256(key).hexdigest()[:12]


def make_inferred_stmt_uri(s, p, o) -> URIRef:
    """추론 결과 triple 을 가리키는 rdf:Statement URI."""
    return URIRef(f"{_INFERRED_STMT_PREFIX}{_stable_triple_hash(s, p, o)}")


def make_source_stmt_uri(s, p, o) -> URIRef:
    """추론 prerequisite (A-Box 원본) triple reification URI."""
    return URIRef(f"{_SOURCE_STMT_PREFIX}{_stable_triple_hash(s, p, o)}")


def make_tbox_axiom_uri(s, p, o) -> URIRef:
    """추론 prerequisite (T-Box axiom) triple reification URI."""
    return URIRef(f"{_TBOX_AXIOM_PREFIX}{_stable_triple_hash(s, p, o)}")


# I2 기본 skip 규칙: 추론 정보가치 낮음 (이미 존재 / 설명 불가)
_DEFAULT_SKIP_RULES = frozenset({"already_exists", "unknown"})


def build_per_triple_reification(
    justifications: list[dict],
    pre_graph: Graph | None,
    tbox: Graph | None,
    activity_uri: URIRef,
    *,
    skip_rules: frozenset[str] = _DEFAULT_SKIP_RULES,
    max_entries: int | None = None,
) -> Graph:
    """justification 리스트 → rdf:Statement reification 그래프.

    각 추론 triple 을 rdf:Statement 로 매립하고 derivedByRule + confidence +
    wasGeneratedBy(activity) 를 부여한다. prerequisites 는 현재 구현에서 설명
    문자열로 제공되므로 (예: "(s, p, o) in pre") steel-prov:prerequisiteNote
    리터럴로 보존 — 기계 추론 체인 재구성은 별도 과제.

    Args:
        justifications: _build_justifications_from_graphs 반환값과 동일 스키마
            ({"triple": (s, p, o), "rule": str, "prerequisites": [str], "confidence": str}).
        pre_graph: 현 구현에서는 사용하지 않음 (구조적 prerequisite parsing 생략).
            시그니처는 향후 확장성을 위해 유지.
        tbox: 동일 — 현 구현 미사용.
        activity_uri: prov:Activity URI (이 추론 run 의 식별자).
        skip_rules: reification 을 생략할 rule 이름 (기본: already_exists/unknown).
        max_entries: rule 별 최대 reify 개수 (None = 무제한). 파일 크기 관리용.

    Returns:
        reification triples 를 담은 rdflib Graph. Caller 가 기존
        inference_provenance.ttl 과 merge 후 serialize.
    """
    g = _new_graph()
    g.bind("prov", PROV)
    g.bind("rdf", str(RDF))

    rule_counts: dict[str, int] = defaultdict(int)
    derived_by_rule = URIRef(f"{PROV_BASE}derivedByRule")
    conf_pred = URIRef(f"{PROV_BASE}confidence")
    note_pred = URIRef(f"{PROV_BASE}prerequisiteNote")
    was_generated_by = URIRef(f"{PROV}wasGeneratedBy")

    for j in justifications:
        rule = j.get("rule", "unknown")
        if rule in skip_rules:
            continue
        if max_entries is not None and rule_counts[rule] >= max_entries:
            continue

        triple = j.get("triple")
        if triple is None or len(triple) != 3:
            continue
        s, p, o = triple

        stmt_uri = make_inferred_stmt_uri(s, p, o)

        # reification 핵심 4 triple
        g.add((stmt_uri, RDF.type, RDF.Statement))
        g.add((stmt_uri, RDF.subject, s))
        g.add((stmt_uri, RDF.predicate, p))
        g.add((stmt_uri, RDF.object, o))
        # 규칙 / 신뢰도 / activity 링크
        g.add((stmt_uri, derived_by_rule, Literal(rule)))
        g.add((stmt_uri, conf_pred, Literal(j.get("confidence", "high"))))
        g.add((stmt_uri, was_generated_by, activity_uri))

        # prerequisites 는 설명 문자열 → Literal 로 보존
        for prereq in j.get("prerequisites", []) or []:
            try:
                g.add((stmt_uri, note_pred, Literal(str(prereq))))
            except Exception:
                # Literal 변환 실패 시 해당 prereq 만 skip (나머지 유지)
                continue

        rule_counts[rule] += 1

    return g


def build_inference_provenance(
    justification_summary: dict,
    *,
    tool_name: str = "owlrl",
    tool_version: str = "7.1.4",
    started_at: str | None = None,
    ended_at: str | None = None,
    input_ttl_paths: list[str] | None = None,
) -> Graph:
    """추론 justification_summary를 PROV-O RDF 그래프로 변환.

    justification_summary 구조 (inference.py가 생성):
    {
        "generated_at": iso,
        "total_inferred": int,
        "justified": int,
        "unjustified": int,
        "by_rule": { rule_name: {"count": N, "sample": [...]}, ... },
        "unjustified_triples": [...]
    }

    반환: PROV-O 트리플을 담은 rdflib Graph. caller가 서LIZE.
    """
    g = _new_graph()
    g.bind("prov", PROV)

    ts = justification_summary.get("generated_at", datetime.now().isoformat())
    act = _make_activity_uri("inference", ts)
    agent = _make_agent_uri(tool_name, tool_version)

    # Activity
    g.add((act, RDF.type, URIRef(f"{PROV}Activity")))
    if started_at:
        g.add((act, URIRef(f"{PROV}startedAtTime"),
               Literal(started_at, datatype=XSD.dateTime)))
    if ended_at:
        g.add((act, URIRef(f"{PROV}endedAtTime"),
               Literal(ended_at, datatype=XSD.dateTime)))
    g.add((act, URIRef(f"{PROV}wasAssociatedWith"), agent))

    # Agent
    g.add((agent, RDF.type, URIRef(f"{PROV}SoftwareAgent")))
    g.add((agent, URIRef(f"{PROV}label") if False else
           URIRef("http://www.w3.org/2000/01/rdf-schema#label"),
           Literal(f"{tool_name}@{tool_version}")))

    # Input entities (used)
    for path in (input_ttl_paths or []):
        ent = URIRef(f"file://{os.path.abspath(path)}")
        g.add((ent, RDF.type, URIRef(f"{PROV}Entity")))
        g.add((act, URIRef(f"{PROV}used"), ent))

    # Per-rule derived entity + count
    by_rule = justification_summary.get("by_rule", {})
    for rule_name, data in by_rule.items():
        rule_ent = _make_rule_uri(rule_name)
        g.add((rule_ent, RDF.type, URIRef(f"{PROV}Entity")))
        g.add((rule_ent, URIRef(f"{PROV}wasGeneratedBy"), act))
        g.add((rule_ent, URIRef(f"{PROV}wasAttributedTo"), agent))
        g.add((
            rule_ent,
            URIRef(f"{PROV_BASE}count"),
            Literal(int(data.get("count", 0)), datatype=XSD.integer),
        ))
        g.add((
            rule_ent,
            URIRef(f"{PROV_BASE}ruleName"),
            Literal(rule_name),
        ))

    # Summary entity
    summary_ent = URIRef(f"{PROV_BASE}inference_summary_{ts.replace(':', '-')}")
    g.add((summary_ent, RDF.type, URIRef(f"{PROV}Entity")))
    g.add((summary_ent, URIRef(f"{PROV}wasGeneratedBy"), act))
    g.add((summary_ent, URIRef(f"{PROV_BASE}totalInferred"),
           Literal(int(justification_summary.get("total_inferred", 0)), datatype=XSD.integer)))
    g.add((summary_ent, URIRef(f"{PROV_BASE}justifiedCount"),
           Literal(int(justification_summary.get("justified", 0)), datatype=XSD.integer)))
    g.add((summary_ent, URIRef(f"{PROV_BASE}unjustifiedCount"),
           Literal(int(justification_summary.get("unjustified", 0)), datatype=XSD.integer)))

    return g


def write_inference_provenance(
    justification_summary: dict,
    output_path: str,
    *,
    tool_name: str = "owlrl",
    tool_version: str = "7.1.4",
    started_at: str | None = None,
    ended_at: str | None = None,
    input_ttl_paths: list[str] | None = None,
) -> int:
    """PROV-O 그래프를 TTL 파일로 저장. 반환: 쓴 트리플 수."""
    g = build_inference_provenance(
        justification_summary,
        tool_name=tool_name,
        tool_version=tool_version,
        started_at=started_at,
        ended_at=ended_at,
        input_ttl_paths=input_ttl_paths,
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    g.serialize(destination=output_path, format="turtle")
    return len(g)


def build_abox_provenance(
    per_class_stats: dict,
    *,
    tool_name: str = "abox_generation",
    tool_version: str = "1.0",
    source_csv_dir: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> Graph:
    """A-Box 클래스별 인스턴스가 어느 CSV 테이블에서 왔는지 PROV 기록.

    per_class_stats: abox_stats.json의 per_class 필드.
    """
    g = _new_graph()
    g.bind("prov", PROV)

    ts = datetime.now().isoformat()
    act = _make_activity_uri("abox", ts)
    agent = _make_agent_uri(tool_name, tool_version)

    g.add((act, RDF.type, URIRef(f"{PROV}Activity")))
    g.add((act, URIRef(f"{PROV}wasAssociatedWith"), agent))
    if started_at:
        g.add((act, URIRef(f"{PROV}startedAtTime"),
               Literal(started_at, datatype=XSD.dateTime)))
    if ended_at:
        g.add((act, URIRef(f"{PROV}endedAtTime"),
               Literal(ended_at, datatype=XSD.dateTime)))

    g.add((agent, RDF.type, URIRef(f"{PROV}SoftwareAgent")))

    for cls_name, stats in per_class_stats.items():
        cls_ent = URIRef(f"{PROV_BASE}abox_class_{cls_name}")
        g.add((cls_ent, RDF.type, URIRef(f"{PROV}Entity")))
        g.add((cls_ent, URIRef(f"{PROV}wasGeneratedBy"), act))
        g.add((cls_ent, URIRef(f"{PROV_BASE}className"), Literal(cls_name)))
        g.add((cls_ent, URIRef(f"{PROV_BASE}instanceCount"),
               Literal(int(stats.get("instance_count", 0)), datatype=XSD.integer)))
        source_table = stats.get("source_table")
        if source_table and source_csv_dir:
            csv_path = os.path.join(source_csv_dir, f"{source_table}.csv")
            csv_ent = URIRef(f"file://{os.path.abspath(csv_path)}")
            g.add((csv_ent, RDF.type, URIRef(f"{PROV}Entity")))
            g.add((cls_ent, URIRef(f"{PROV}wasDerivedFrom"), csv_ent))

    return g


def write_abox_provenance(
    per_class_stats: dict,
    output_path: str,
    **kwargs,
) -> int:
    g = build_abox_provenance(per_class_stats, **kwargs)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    g.serialize(destination=output_path, format="turtle")
    return len(g)


# ── #9 Cell-level provenance ──────────────────────


def make_cell_provenance_uri(
    csv_table: str, row_num: int, col_name: str,
) -> URIRef:
    """CSV 셀을 가리키는 안정 URI — file+row+col 조합.

    Format: prov://<csv_table>#row=<N>&col=<colname>
    """
    safe_col = col_name.replace(" ", "_").replace("/", "_")
    return URIRef(f"prov://{csv_table}#row={row_num}&col={safe_col}")


# ── A4 Row-level provenance ───────────────────────


def make_row_provenance_uri(csv_table: str, row_num: int) -> URIRef:
    """CSV row 를 가리키는 안정 URI.

    Cell-level make_cell_provenance_uri 와 접두부 일치
    (prov://<table>#row=<N>) — 향후 cell-level 추가 시 호환.

    Format: prov://<csv_table>#row=<N>
    """
    return URIRef(f"prov://{csv_table}#row={row_num}")


def build_row_uri_for_abox(csv_table: str, row_num: int) -> URIRef:
    """A-Box 본체에서 쓸 row URI — graph 객체 없이 URI 만 필요한 writer 경로용.

    annotate_row_provenance 와 분리된 이유: A-Box 본체는 writer pattern (bulk
    N-triples) 이라 graph 객체를 가지지 않음. make_row_provenance_uri 의
    alias 로 호출자에게 의도를 명확히 드러낸다.
    """
    return make_row_provenance_uri(csv_table, row_num)


def _get_csv_mtime_iso(csv_path: str) -> str | None:
    """CSV 파일의 수정 시간을 ISO-8601 (UTC) 로 반환. 파일 없으면 None."""
    from datetime import datetime

    if not os.path.exists(csv_path):
        return None
    try:
        mtime = os.path.getmtime(csv_path)
        return datetime.fromtimestamp(mtime, tz=UTC).isoformat()
    except OSError:
        return None


def annotate_row_provenance(
    sidecar: Graph,
    instance_uri: URIRef,
    csv_table: str,
    row_num: int,
    csv_path: str | None = None,
) -> int:
    """A-Box 인스턴스에 row-level provenance 를 sidecar graph 에 기록한다.

    A-Box 본체에는 instance → row_uri 링크만 (caller 가 별도 add).
    Sidecar 에는 row entity 의 메타데이터 (csvTable, rowNumber, csvMtime, file link).

    Row URI 가 이미 sidecar 에 있으면 메타데이터 추가 skip (dedupe).

    Args:
        sidecar: abox_provenance.ttl 에 쓰일 graph
        instance_uri: 주 A-Box 인스턴스 (sidecar 에는 link 안 함 — 본체가 담당)
        csv_table: CSV 파일 basename (확장자 제외)
        row_num: 1-based row index (헤더 제외)
        csv_path: 실제 CSV 파일 경로 (mtime 추출용, 선택)

    Returns:
        sidecar 에 추가된 triple 수 (dedupe 시 0)
    """
    row_uri = make_row_provenance_uri(csv_table, row_num)

    # Dedupe: row entity 가 metadata (csvTable) 까지 포함해 완전히 있으면 skip.
    # 단순 type triple 만 있는 상태 (이전 run 결함 등) 는 보완을 위해 재기록.
    if (row_uri, URIRef(f"{PROV_BASE}csvTable"), None) in sidecar:
        return 0

    added = 0
    sidecar.add((row_uri, RDF.type, URIRef(f"{PROV}Entity")))
    sidecar.add((row_uri, URIRef(f"{PROV_BASE}csvTable"), Literal(csv_table)))
    sidecar.add((
        row_uri,
        URIRef(f"{PROV_BASE}rowNumber"),
        Literal(int(row_num), datatype=XSD.integer),
    ))
    added += 3

    if csv_path:
        mtime_iso = _get_csv_mtime_iso(csv_path)
        if mtime_iso:
            sidecar.add((
                row_uri,
                URIRef(f"{PROV_BASE}csvMtime"),
                Literal(mtime_iso, datatype=XSD.dateTime),
            ))
            added += 1
        if os.path.exists(csv_path):
            # CSV file entity link — file:// absolute URI
            csv_file_uri = URIRef(f"file://{os.path.abspath(csv_path)}")
            sidecar.add((row_uri, URIRef(f"{PROV}wasDerivedFrom"), csv_file_uri))
            added += 1

    return added


def annotate_instance_cell_provenance(
    g: Graph,
    instance_uri: URIRef,
    csv_table: str,
    row_num: int,
    cell_map: dict[str, str] | None = None,
) -> int:
    """A-Box 인스턴스에 cell-level provenance를 부여한다.

    각 인스턴스는 최소한 원본 CSV 파일+row를 가리키고,
    선택적으로 각 property별 source column을 기록한다.

    Args:
        g: A-Box 또는 provenance sidecar 그래프.
        instance_uri: A-Box 인스턴스 URI.
        csv_table: CSV 파일 basename (확장자 제외).
        row_num: 1-based row index (헤더 제외).
        cell_map: {property_local_name: column_name} — 선택적 칼럼 매핑.

    Returns:
        추가된 트리플 수.
    """
    row_uri = URIRef(
        f"prov://{csv_table}#row={row_num}",
    )
    added = 0
    if (instance_uri, URIRef(f"{PROV}wasDerivedFrom"), row_uri) not in g:
        g.add((instance_uri, URIRef(f"{PROV}wasDerivedFrom"), row_uri))
        g.add((row_uri, RDF.type, URIRef(f"{PROV}Entity")))
        g.add((row_uri, URIRef(f"{PROV_BASE}csvTable"), Literal(csv_table)))
        g.add((row_uri, URIRef(f"{PROV_BASE}rowNumber"),
               Literal(int(row_num), datatype=XSD.integer)))
        added += 3

    if cell_map:
        for prop_name, col in cell_map.items():
            cell_uri = make_cell_provenance_uri(csv_table, row_num, col)
            g.add((cell_uri, RDF.type, URIRef(f"{PROV}Entity")))
            g.add((cell_uri, URIRef(f"{PROV_BASE}csvTable"), Literal(csv_table)))
            g.add((cell_uri, URIRef(f"{PROV_BASE}rowNumber"),
                   Literal(int(row_num), datatype=XSD.integer)))
            g.add((cell_uri, URIRef(f"{PROV_BASE}column"), Literal(col)))
            g.add((cell_uri, URIRef(f"{PROV_BASE}property"),
                   Literal(prop_name)))
            g.add((cell_uri, URIRef(f"{PROV}wasDerivedFrom"), row_uri))
            added += 6
    return added


def trace_instance_to_cell(
    g: Graph, instance_uri: URIRef,
) -> dict:
    """인스턴스 → CSV row/col 역추적 조회.

    Returns:
        {csv_table, row_number, cells: [{property, column}]}
    """
    derived = [o for o in g.objects(instance_uri,
                                    URIRef(f"{PROV}wasDerivedFrom"))
               if isinstance(o, URIRef) and str(o).startswith("prov://")]
    if not derived:
        return {"csv_table": None, "row_number": None, "cells": []}
    # row URI (prov://<table>#row=N)
    row_uri = next(
        (u for u in derived if "&col=" not in str(u)), derived[0],
    )
    table_vals = list(g.objects(row_uri, URIRef(f"{PROV_BASE}csvTable")))
    row_vals = list(g.objects(row_uri, URIRef(f"{PROV_BASE}rowNumber")))
    csv_table = str(table_vals[0]) if table_vals else None
    row_number = int(str(row_vals[0])) if row_vals else None

    # Cell URIs: prov://<table>#row=N&col=X
    cells = []
    for cell in g.subjects(URIRef(f"{PROV}wasDerivedFrom"), row_uri):
        if not (isinstance(cell, URIRef) and "&col=" in str(cell)):
            continue
        prop_vals = list(g.objects(cell, URIRef(f"{PROV_BASE}property")))
        col_vals = list(g.objects(cell, URIRef(f"{PROV_BASE}column")))
        cells.append({
            "property": str(prop_vals[0]) if prop_vals else None,
            "column": str(col_vals[0]) if col_vals else None,
        })
    return {
        "csv_table": csv_table,
        "row_number": row_number,
        "cells": sorted(cells, key=lambda c: c.get("column") or ""),
    }


def trace_provenance(instance_uri: str, prov_path: str = "") -> str:
    """인스턴스 URI → 원본 CSV row/column 역추적 (#9).

    A-Box에 cell-level provenance가 기록되어 있으면 해당 인스턴스가
    어느 CSV 테이블의 몇 번째 행 / 어떤 컬럼에서 유래했는지 반환.
    이상치 감지, 데이터 품질 감사 시 원인 데이터로 바로 이동하는 용도.

    Args:
        instance_uri: 조회할 A-Box 인스턴스 URI (전체 URI).
        prov_path: data/generated 아래 provenance TTL 경로 (절대경로 또는 작업
                   디렉터리 기준 상대경로). symlink 해석 후에도 data/generated 안이어야
                   한다. 비어있으면 A-Box 디렉터리의 provenance sidecar
                   (data/generated/abox/abox_provenance.ttl) 또는 A-Box 자체에서 검색.
    """
    logger = logging.getLogger(__name__)
    try:
        candidate_paths = []
        if prov_path:
            candidate_paths.append(
                resolve_path_within(GENERATED_DIR, prov_path, allowed_suffixes=(".ttl",)),
            )
        else:
            abox_dir = os.path.dirname(ABOX_PATH)
            candidate_paths.extend([
                os.path.join(abox_dir, "abox_provenance.ttl"),
                ABOX_PATH,
            ])
        g = _new_graph()
        loaded = False
        for p in candidate_paths:
            if os.path.exists(p):
                g.parse(p, format="turtle")
                loaded = True
        if not loaded:
            return error_response(
                f"provenance TTL을 찾지 못했습니다: {candidate_paths}",
                hint="generate_abox 실행 시 cell-level provenance가 기록됩니다.",
                logger=logger,
            )
        result = trace_instance_to_cell(g, URIRef(instance_uri))
        result["instance_uri"] = instance_uri
        if result["csv_table"] is None:
            result["message"] = "해당 인스턴스에 cell-level provenance가 없습니다."
        return success_response(result)
    except Exception as e:
        return error_response(e, logger=logger)


# ── I2 query_inference_justification ─────────────────────────────


def _default_inference_prov_path() -> str:
    """기본 inference_provenance.ttl 경로 — INFERRED_PATH 와 동일 디렉토리."""
    return os.path.join(
        os.path.dirname(INFERRED_PATH), "inference_provenance.ttl",
    )


def query_inference_justification(
    s: str, p: str, o: str, prov_path: str = "",
) -> str:
    """추론된 특정 triple 의 justification (rule + prerequisites) 를 역추적.

    `inference_provenance.ttl` 에 매립된 per-triple rdf:Statement reification 을
    SPARQL 로 조회해 "이 triple 이 어떤 규칙으로 도출됐나" 를 반환. 환경변수
    `I2_PER_TRIPLE_JUSTIFICATION=true` (기본) 로 run_owl_rl_inference 실행 시
    매립된다.

    Args:
        s, p, o: 조회할 추론 triple (전체 URI 문자열, 예: "http://.../EQ001").
        prov_path: data/generated 아래 inference_provenance.ttl 경로 (절대경로 또는
            작업 디렉터리 기준 상대경로). symlink 해석 후에도 data/generated 안이어야
            한다. 빈 문자열이면 기본 위치 사용.

    Returns:
        success_response({
          "found": bool,
          "rule": str,              # 예: "inverse_of", "rdfs_domain"
          "confidence": str,        # "high" | "low"
          "prerequisites": [str],   # 설명 문자열 (예: "(s, p, o) in pre")
        })
        또는 error_response (파일 없음 / SPARQL 실패).
    """
    try:
        path = _default_inference_prov_path()
        if prov_path:
            path = resolve_path_within(
                GENERATED_DIR, prov_path, allowed_suffixes=(".ttl",),
            )
        if not os.path.exists(path):
            return error_response(
                f"inference_provenance.ttl 없음: {path}",
                hint=(
                    "run_owl_rl_inference 실행 후 이용 가능. "
                    "I2_PER_TRIPLE_JUSTIFICATION=false 로 비활성화됐을 수 있음."
                ),
                logger=logger,
            )

        g = _new_graph()
        g.parse(path, format="turtle")

        # SPARQL 실행 — s/p/o 는 initBindings 로 파라미터 바인딩해 URI 안전성 보장.
        # PREFIX 는 f-string 보간 필요 (SPARQL 파서 레벨). steel: 네임스페이스는
        # 고정 상수 PROV_BASE 에서 유래하므로 사용자 입력 아님.
        # NOTE: oxigraph 는 initBindings 변수를 SELECT projection 에 포함해야 하므로
        # ?s/?p/?o 도 SELECT 에 포함 — 결과 unpack 인덱스는 [3]~[5] 로 이동.
        sparql = f"""
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX steel: <{PROV_BASE}>

        SELECT ?s ?p ?o ?rule ?conf ?note WHERE {{
          ?stmt rdf:type rdf:Statement ;
                rdf:subject ?s ;
                rdf:predicate ?p ;
                rdf:object ?o ;
                steel:derivedByRule ?rule ;
                steel:confidence ?conf .
          OPTIONAL {{ ?stmt steel:prerequisiteNote ?note }}
        }}
        """
        rows = list(g.query(sparql, initBindings={
            "s": URIRef(s),
            "p": URIRef(p),
            "o": URIRef(o),
        }))
        if not rows:
            return success_response({
                "found": False,
                "message": (
                    "해당 triple 의 reification 없음 — pre-existing / skip_rules / "
                    "I2 disabled 중 하나. inference_justifications.json 에서 "
                    "rule-level 샘플 확인 권장."
                ),
            })

        # SELECT projection: ?s ?p ?o ?rule ?conf ?note → rule=[3], conf=[4], note=[5]
        rule = str(rows[0][3])
        conf = str(rows[0][4])
        prerequisites = []
        seen_notes = set()
        for row in rows:
            note = row[5]
            if note is not None:
                note_str = str(note)
                if note_str not in seen_notes:
                    prerequisites.append(note_str)
                    seen_notes.add(note_str)

        return success_response({
            "found": True,
            "rule": rule,
            "confidence": conf,
            "prerequisites": prerequisites,
        })
    except Exception as e:
        return error_response(e, logger=logger)
