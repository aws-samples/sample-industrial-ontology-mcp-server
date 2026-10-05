"""LPG Named Graph Context 보존 — #14.

RDF Dataset(Named Graph)을 LPG로 변환할 때 source graph 식별자를 잃지 않도록
노드/관계에 `graph_context` 속성을 부여하는 유틸리티.

사용:
    from tools.lpg_graph_context import (
        read_named_graph_sources, annotate_with_graph_context,
    )

    # CSV 한 쌍(nodes.csv, relationships.csv)에 graph_context 컬럼 추가
    annotate_with_graph_context(
        nodes_csv_path, relationships_csv_path,
        default_context="main",
    )

#19(CWA/OWA 태깅)와 함께 사용하면 "CSV 유래 / 암묵지 유래 / 추론 유래"를
LPG 상에서 구분할 수 있다.
"""
from __future__ import annotations

import csv
import logging
import os

from rdflib import ConjunctiveGraph

from config import INFERRED_PATH
from tools.common import atomic_write, error_response, resolve_path_within, success_response

logger = logging.getLogger(__name__)


CONTEXT_COLUMN = "graph_context:string"


def read_named_graph_sources(dataset: ConjunctiveGraph) -> dict[str, set[str]]:
    """rdflib ConjunctiveGraph에서 각 (s, p, o) → named graph IRI 집합 추출.

    반환 key: "s|p|o" 문자열. 값: 해당 트리플을 포함한 graph IRI 집합.
    """
    out: dict[str, set[str]] = {}
    for s, p, o, ctx in dataset.quads():
        key = f"{s}|{p}|{o}"
        out.setdefault(key, set()).add(str(ctx.identifier))
    return out


def _detect_delimiter(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        sample = f.read(2048)
    return "\t" if "\t" in sample and sample.count("\t") > sample.count(",") else ","


def annotate_with_graph_context(
    nodes_csv: str,
    relationships_csv: str,
    *,
    default_context: str = "main",
    inferred_triples: set[tuple[str, str, str]] | None = None,
    inferred_context: str = "inferred",
) -> dict:
    """CSV nodes/relationships에 graph_context 컬럼을 추가.

    Neo4j의 APOC 배치 적재 시 자동으로 해당 속성이 노드/관계에 부여됨.

    Args:
        nodes_csv: 노드 CSV 경로 (Neo4j 헤더 규칙: `id:ID`, `:LABEL` 등).
        relationships_csv: 관계 CSV 경로.
        default_context: 기본 태그. CSV 유래는 "main".
        inferred_triples: (s_uri, p_uri, o_uri) 튜플 집합. 여기 포함된 관계는
            context를 `inferred_context`로 설정.
        inferred_context: 추론 유래 태그. 기본 "inferred".

    Returns:
        {nodes_updated, rels_updated, added_column}
    """
    if not os.path.exists(nodes_csv):
        raise FileNotFoundError(nodes_csv)
    if not os.path.exists(relationships_csv):
        raise FileNotFoundError(relationships_csv)

    inferred_triples = inferred_triples or set()

    # Nodes: 단순히 default_context 컬럼 추가 (노드 자체는 graph-level 구분이 어려움)
    nodes_updated = _append_column_csv(
        nodes_csv, CONTEXT_COLUMN, lambda row: default_context,
    )

    # Relationships: inferred_triples에 포함된 것은 "inferred"
    def _rel_context(row: dict) -> str:
        start = row.get(":START_ID") or row.get("start:ID") or ""
        end = row.get(":END_ID") or row.get("end:ID") or ""
        rtype = row.get(":TYPE") or row.get("rel_type") or ""
        key = (start, rtype, end)
        if key in inferred_triples:
            return inferred_context
        return default_context

    rels_updated = _append_column_csv(
        relationships_csv, CONTEXT_COLUMN, _rel_context,
    )

    return {
        "nodes_updated": nodes_updated,
        "rels_updated": rels_updated,
        "added_column": CONTEXT_COLUMN,
    }


def _append_column_csv(
    path: str, col_header: str, value_fn,
) -> int:
    """CSV 파일에 컬럼 추가. 기존에 있으면 스킵. atomic write.

    Returns:
        업데이트된 행 수.
    """
    delim = _detect_delimiter(path)
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=delim)
        rows = list(reader)
    if not rows:
        return 0
    header = rows[0]
    if col_header in header:
        return 0

    header.append(col_header)
    out_rows = [header]
    count = 0
    for r in rows[1:]:
        row_dict = dict(zip(rows[0], r, strict=False))
        val = value_fn(row_dict) if callable(value_fn) else value_fn
        out_rows.append(list(r) + [val])
        count += 1

    from io import StringIO
    buf = StringIO()
    writer = csv.writer(buf, delimiter=delim)
    writer.writerows(out_rows)
    atomic_write(path, buf.getvalue())
    return count


def annotate_lpg_graph_context(
    nodes_csv: str,
    relationships_csv: str,
    default_context: str = "main",
) -> str:
    """LPG CSV 한 쌍에 graph_context 컬럼을 추가해 네임드 그래프 유래 보존 (#14).

    Neo4j APOC 배치 적재 시 노드/관계 속성으로 자동 세팅되어,
    이후 Cypher 질의에서 `WHERE n.graph_context = 'inferred'` 로 필터 가능.

    두 CSV 를 제자리에서 다시 쓰므로 경로는 convert_rdf_to_lpg 가 CSV 를 쓰는
    data/generated/inferred/neo4j 안의 .csv 파일이어야 한다 (symlink 해석 후 기준).
    둘 중 하나라도 경계 밖이면 어느 파일도 쓰지 않는다.

    Args:
        nodes_csv: data/generated/inferred/neo4j 아래 노드 CSV 경로 (절대경로 또는
            작업 디렉터리 기준 상대경로).
        relationships_csv: data/generated/inferred/neo4j 아래 관계 CSV 경로 (절대경로
            또는 작업 디렉터리 기준 상대경로).
        default_context: CSV 유래 기본 태그 (기본 "main").
    """
    lpg_csv_dir = os.path.join(os.path.dirname(INFERRED_PATH), "neo4j")
    try:
        nodes_csv = resolve_path_within(lpg_csv_dir, nodes_csv, allowed_suffixes=(".csv",))
        relationships_csv = resolve_path_within(
            lpg_csv_dir, relationships_csv, allowed_suffixes=(".csv",),
        )
    except Exception as e:
        return error_response(e, logger=logger)

    try:
        r = annotate_with_graph_context(
            nodes_csv, relationships_csv, default_context=default_context,
        )
        return success_response(r)
    except Exception as e:
        return error_response(e, logger=logger)
