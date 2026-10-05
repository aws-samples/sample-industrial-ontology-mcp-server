"""Tests for tools/lpg_graph_context.py — #14."""
from __future__ import annotations

import csv
import json

import pytest

from tools.lpg_graph_context import (
    CONTEXT_COLUMN,
    annotate_lpg_graph_context,
    annotate_with_graph_context,
)


@pytest.fixture
def lpg_dir(tmp_path, monkeypatch):
    """MCP 도구의 쓰기 경계(dirname(INFERRED_PATH)/neo4j)를 tmp 로 옮긴다."""
    import tools.lpg_graph_context as lpg_graph_context

    monkeypatch.setattr(
        lpg_graph_context, "INFERRED_PATH", str(tmp_path / "all_inferred.ttl"),
    )
    directory = tmp_path / "neo4j"
    directory.mkdir()
    return directory


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)


def test_annotate_nodes_and_rels(tmp_path):
    nodes = tmp_path / "nodes.csv"
    rels = tmp_path / "rels.csv"
    _write_csv(nodes, [
        ["id:ID", ":LABEL", "name"],
        ["n1", "A", "alpha"],
        ["n2", "B", "beta"],
    ])
    _write_csv(rels, [
        [":START_ID", ":END_ID", ":TYPE"],
        ["n1", "n2", "hasX"],
    ])
    result = annotate_with_graph_context(
        str(nodes), str(rels),
        default_context="main",
    )
    assert result["nodes_updated"] == 2
    assert result["rels_updated"] == 1
    # 컬럼 추가 확인
    with open(nodes, encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert CONTEXT_COLUMN in header


def test_inferred_relationships_tagged(tmp_path):
    nodes = tmp_path / "nodes.csv"
    rels = tmp_path / "rels.csv"
    _write_csv(nodes, [["id:ID", ":LABEL"], ["a", "X"], ["b", "X"]])
    _write_csv(rels, [
        [":START_ID", ":END_ID", ":TYPE"],
        ["a", "b", "derived"],
        ["b", "a", "manual"],
    ])
    inferred = {("a", "derived", "b")}
    annotate_with_graph_context(
        str(nodes), str(rels),
        default_context="main",
        inferred_triples=inferred,
        inferred_context="inferred",
    )
    with open(rels, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        parsed = list(reader)
    derived = [r for r in parsed if r[":TYPE"] == "derived"][0]
    manual = [r for r in parsed if r[":TYPE"] == "manual"][0]
    assert derived[CONTEXT_COLUMN] == "inferred"
    assert manual[CONTEXT_COLUMN] == "main"


def test_idempotent(tmp_path):
    nodes = tmp_path / "nodes.csv"
    rels = tmp_path / "rels.csv"
    _write_csv(nodes, [["id:ID"], ["n1"]])
    _write_csv(rels, [[":START_ID", ":END_ID", ":TYPE"], ["n1", "n1", "T"]])
    annotate_with_graph_context(str(nodes), str(rels))
    sz_n1 = nodes.stat().st_size
    sz_r1 = rels.stat().st_size
    # 두 번째 호출은 컬럼이 이미 있어 무변경
    annotate_with_graph_context(str(nodes), str(rels))
    assert nodes.stat().st_size == sz_n1
    assert rels.stat().st_size == sz_r1


def test_mcp_tool_missing(lpg_dir):
    raw = annotate_lpg_graph_context(
        str(lpg_dir / "missing_nodes.csv"),
        str(lpg_dir / "missing_rels.csv"),
    )
    data = json.loads(raw)
    assert data["success"] is False


def test_mcp_tool_success(lpg_dir):
    nodes = lpg_dir / "n.csv"
    rels = lpg_dir / "r.csv"
    _write_csv(nodes, [["id:ID"], ["a"]])
    _write_csv(rels, [[":START_ID", ":END_ID", ":TYPE"], ["a", "a", "t"]])
    raw = annotate_lpg_graph_context(str(nodes), str(rels))
    data = json.loads(raw)
    assert data["success"] is True
    assert data["added_column"] == CONTEXT_COLUMN
