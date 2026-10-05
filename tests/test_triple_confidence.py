"""Tests for tools/triple_confidence.py — Q2."""
from __future__ import annotations

import json

from rdflib import Literal, URIRef
from rdflib.namespace import RDF

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.triple_confidence import (
    CONFIDENCE,
    annotate_triple,
    build_confidence_sidecar,
    confidence_for,
    filter_by_confidence,
    summarize_confidence_sidecar,
    summarize_sidecar,
    write_confidence_sidecar,
)


def test_confidence_for_known():
    assert confidence_for("csv_direct") == 1.0
    assert confidence_for("fk_verified") == 0.95
    assert confidence_for("unknown") == 0.5


def test_confidence_for_chain_depth():
    assert confidence_for("owl_rl_chain", chain_depth=2) == 0.5
    assert confidence_for("owl_rl_chain", chain_depth=10) == 0.3  # clamped


def test_annotate_triple_adds_reified():
    g = _new_graph()
    s = URIRef(f"{DOMAIN_INST_NS}A")
    p = URIRef(f"{DOMAIN_NS}hasX")
    o = URIRef(f"{DOMAIN_INST_NS}B")
    stmt = annotate_triple(g, s, p, o, provenance="csv_direct")
    assert (stmt, RDF.type, RDF.Statement) in g
    assert (stmt, RDF.subject, s) in g
    assert (stmt, RDF.predicate, p) in g
    assert (stmt, RDF.object, o) in g
    confs = list(g.objects(stmt, CONFIDENCE))
    assert len(confs) == 1
    assert float(confs[0].toPython()) == 1.0


def test_annotate_triple_custom_confidence():
    g = _new_graph()
    s = URIRef(f"{DOMAIN_INST_NS}A")
    p = URIRef(f"{DOMAIN_NS}hasX")
    o = URIRef(f"{DOMAIN_INST_NS}B")
    annotate_triple(g, s, p, o, provenance="bedrock_llm", confidence=0.3)
    vals = [float(x.toPython()) for x in g.objects(None, CONFIDENCE)]
    assert 0.3 in vals


def test_annotate_deterministic_stmt_uri():
    g = _new_graph()
    s = URIRef(f"{DOMAIN_INST_NS}A")
    p = URIRef(f"{DOMAIN_NS}hasX")
    o = URIRef(f"{DOMAIN_INST_NS}B")
    stmt1 = annotate_triple(g, s, p, o, provenance="csv_direct")
    g2 = _new_graph()
    stmt2 = annotate_triple(g2, s, p, o, provenance="csv_direct")
    assert stmt1 == stmt2  # SHA256 기반, 결정적


def test_build_confidence_sidecar():
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}hasX"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct"},
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}hasName"),
         "o": Literal("name"), "provenance": "bedrock_llm", "confidence": 0.4},
    ]
    g = build_confidence_sidecar(anns)
    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 2


def test_filter_by_confidence():
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}p1"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct"},
        {"s": URIRef(f"{DOMAIN_INST_NS}C"), "p": URIRef(f"{DOMAIN_NS}p2"),
         "o": URIRef(f"{DOMAIN_INST_NS}D"), "provenance": "bedrock_llm",
         "confidence": 0.3},
    ]
    g = build_confidence_sidecar(anns)
    high = filter_by_confidence(g, 0.9)
    assert len(high) == 1
    low_ok = filter_by_confidence(g, 0.2)
    assert len(low_ok) == 2


def test_write_and_parse_roundtrip(tmp_path):
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}p"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct"},
    ]
    out = tmp_path / "sidecar.ttl"
    n = write_confidence_sidecar(anns, str(out))
    assert n > 0 and out.exists()
    g = _new_graph()
    g.parse(str(out), format="turtle")
    assert len(g) == n


def test_summarize_sidecar():
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A{i}"), "p": URIRef(f"{DOMAIN_NS}p"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct"}
        for i in range(3)
    ] + [
        {"s": URIRef(f"{DOMAIN_INST_NS}C"), "p": URIRef(f"{DOMAIN_NS}p"),
         "o": URIRef(f"{DOMAIN_INST_NS}D"), "provenance": "bedrock_llm",
         "confidence": 0.4},
    ]
    g = build_confidence_sidecar(anns)
    s = summarize_sidecar(g)
    assert s["total_statements"] == 4
    assert s["by_provenance"]["csv_direct"] == 3
    assert s["by_provenance"]["bedrock_llm"] == 1
    assert s["confidence"]["max"] == 1.0
    assert s["confidence"]["min"] == 0.4


def test_summarize_confidence_sidecar_tool(tmp_path, monkeypatch):
    # MCP 도구는 data/generated 안의 sidecar 만 읽는다.
    monkeypatch.setattr("tools.triple_confidence.GENERATED_DIR", str(tmp_path))
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}p"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct"},
    ]
    out = tmp_path / "sidecar.ttl"
    write_confidence_sidecar(anns, str(out))

    raw = summarize_confidence_sidecar(str(out))
    data = json.loads(raw)
    assert data["success"] is True
    assert data["total_statements"] == 1


def test_summarize_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.triple_confidence.GENERATED_DIR", str(tmp_path))
    raw = summarize_confidence_sidecar(str(tmp_path / "missing.ttl"))
    data = json.loads(raw)
    assert data["success"] is False
    assert "sidecar not found" in data["error"]


# ── R3 Wikidata rank/reference ──────────────────


def test_rank_valid_values_accepted():
    from tools.triple_confidence import RANK, VALID_RANKS
    g = _new_graph()
    s = URIRef(f"{DOMAIN_INST_NS}A")
    p = URIRef(f"{DOMAIN_NS}hasX")
    URIRef(f"{DOMAIN_INST_NS}B")
    for rank_val in ("preferred", "normal", "deprecated"):
        # 각 rank를 다른 object로 부여해 stmt URI 다르게
        annotate_triple(g, s, p, URIRef(f"{DOMAIN_INST_NS}{rank_val}"),
                        provenance="csv_direct", rank=rank_val)
    assert {"preferred", "normal", "deprecated"} == VALID_RANKS
    ranks_in_graph = sorted(str(r) for r in g.objects(None, RANK))
    assert ranks_in_graph == ["deprecated", "normal", "preferred"]


def test_rank_invalid_raises():
    import pytest
    g = _new_graph()
    with pytest.raises(ValueError, match="Invalid rank"):
        annotate_triple(g, URIRef(f"{DOMAIN_INST_NS}A"), URIRef(f"{DOMAIN_NS}p"),
                        URIRef(f"{DOMAIN_INST_NS}B"),
                        provenance="csv_direct", rank="invalid_rank")


def test_reference_string():
    from tools.triple_confidence import REFERENCE
    g = _new_graph()
    annotate_triple(g, URIRef(f"{DOMAIN_INST_NS}A"), URIRef(f"{DOMAIN_NS}p"),
                    URIRef(f"{DOMAIN_INST_NS}B"),
                    provenance="csv_direct",
                    reference="https://example.org/source.pdf")
    refs = list(g.objects(None, REFERENCE))
    assert len(refs) == 1
    # HTTP URL이면 URIRef로 저장
    assert isinstance(refs[0], URIRef)


def test_reference_list():
    from tools.triple_confidence import REFERENCE
    g = _new_graph()
    annotate_triple(g, URIRef(f"{DOMAIN_INST_NS}A"), URIRef(f"{DOMAIN_NS}p"),
                    URIRef(f"{DOMAIN_INST_NS}B"),
                    provenance="csv_direct",
                    reference=["DOI:10.1234/xyz", "csv:row_42"])
    refs = list(g.objects(None, REFERENCE))
    assert len(refs) == 2


def test_filter_by_rank_excludes_deprecated():
    from tools.triple_confidence import filter_by_rank
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}p1"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct",
         "rank": "preferred"},
        {"s": URIRef(f"{DOMAIN_INST_NS}C"), "p": URIRef(f"{DOMAIN_NS}p2"),
         "o": URIRef(f"{DOMAIN_INST_NS}D"), "provenance": "bedrock_llm",
         "rank": "deprecated"},
        {"s": URIRef(f"{DOMAIN_INST_NS}E"), "p": URIRef(f"{DOMAIN_NS}p3"),
         "o": URIRef(f"{DOMAIN_INST_NS}F"), "provenance": "owl_rl_1hop"},
        # rank 없으면 normal로 간주
    ]
    g = build_confidence_sidecar(anns)
    active = filter_by_rank(g)
    # preferred + normal(rank 미지정) = 2개, deprecated 제외
    assert len(active) == 2


def test_get_references():
    from tools.triple_confidence import _stmt_uri, get_references
    s = URIRef(f"{DOMAIN_INST_NS}A")
    p = URIRef(f"{DOMAIN_NS}hasX")
    o = URIRef(f"{DOMAIN_INST_NS}B")
    g = _new_graph()
    annotate_triple(g, s, p, o, provenance="csv_direct",
                    reference=["http://a.com", "DOI:x"])
    stmt = _stmt_uri(s, p, o)
    refs = get_references(g, stmt)
    assert sorted(refs) == ["DOI:x", "http://a.com"]


def test_summarize_includes_rank_and_reference():
    anns = [
        {"s": URIRef(f"{DOMAIN_INST_NS}A"), "p": URIRef(f"{DOMAIN_NS}p"),
         "o": URIRef(f"{DOMAIN_INST_NS}B"), "provenance": "csv_direct",
         "rank": "preferred",
         "reference": "http://source.com/doc1"},
        {"s": URIRef(f"{DOMAIN_INST_NS}C"), "p": URIRef(f"{DOMAIN_NS}p"),
         "o": URIRef(f"{DOMAIN_INST_NS}D"), "provenance": "bedrock_llm",
         "rank": "deprecated"},
    ]
    g = build_confidence_sidecar(anns)
    s = summarize_sidecar(g)
    assert s["by_rank"] == {"preferred": 1, "deprecated": 1}
    assert s["with_reference_count"] == 1
    assert s["reference_coverage_pct"] == 50.0


def test_rank_weights_constant():
    from tools.triple_confidence import RANK_WEIGHTS
    assert RANK_WEIGHTS["preferred"] > RANK_WEIGHTS["normal"]
    assert RANK_WEIGHTS["deprecated"] == 0.0
