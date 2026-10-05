"""Tests for tools/farber_dimensions.py — R2."""
from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from rdflib import XSD, Literal, URIRef
from rdflib.namespace import OWL, RDF

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, PROV
from domain.tbox_utils import _new_graph
from tools.farber_dimensions import (
    _score_amount_of_data,
    _score_representational_consistency,
    _score_timeliness,
    _score_trustworthiness,
    _score_verifiability,
    evaluate_farber,
)


def _basic_graph(instance_count: int = 5):
    g = _new_graph()
    ns = DOMAIN_NS
    cls = URIRef(f"{ns}Eq")
    g.add((cls, RDF.type, OWL.Class))
    op = URIRef(f"{ns}has")
    g.add((op, RDF.type, OWL.ObjectProperty))
    dp = URIRef(f"{ns}val")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    for i in range(instance_count):
        u = URIRef(f"{DOMAIN_INST_NS}E{i}")
        g.add((u, RDF.type, cls))
        g.add((u, dp, Literal(i)))
    return g


def test_amount_of_data_empty():
    r = _score_amount_of_data(_new_graph())
    assert r["score"] == 0


def test_amount_of_data_small():
    g = _basic_graph(5)
    r = _score_amount_of_data(g)
    # 트리플 수십개 수준 → 낮은 점수지만 0은 아님
    assert 0 < r["score"] < 60
    assert r["class_count"] == 1
    assert r["op_count"] == 1


def test_timeliness_no_files(tmp_path):
    r = _score_timeliness(
        str(tmp_path / "tbox.ttl"), str(tmp_path / "abox.ttl"),
        str(tmp_path / "inf.ttl"), str(tmp_path / "rawdata"),
    )
    assert r["score"] == 0
    assert r["per_artifact_score"]["tbox"] == 0


def test_timeliness_fresh_files(tmp_path):
    # 최근에 생성된 파일들
    for name in ("tbox.ttl", "abox.ttl", "inf.ttl"):
        (tmp_path / name).write_text("@prefix : <http://x/> .")
    rawdir = tmp_path / "rawdata"
    rawdir.mkdir()
    (rawdir / "a.csv").write_text("id,val\n1,2\n")
    r = _score_timeliness(
        str(tmp_path / "tbox.ttl"), str(tmp_path / "abox.ttl"),
        str(tmp_path / "inf.ttl"), str(rawdir),
    )
    # 30일 이내 → 100점 근처
    assert r["score"] >= 70


def test_timeliness_staleness_flag(tmp_path):
    # artifact를 먼저 만들고 오래된 mtime 설정, CSV는 최신
    for name in ("tbox.ttl", "abox.ttl"):
        (tmp_path / name).write_text("x")
    # 산출물 mtime 오래 전으로
    old = time.time() - 86400 * 60  # 60일 전
    os.utime(tmp_path / "tbox.ttl", (old, old))
    os.utime(tmp_path / "abox.ttl", (old, old))

    rawdir = tmp_path / "rawdata"
    rawdir.mkdir()
    (rawdir / "a.csv").write_text("x")  # 방금 생성
    r = _score_timeliness(
        str(tmp_path / "tbox.ttl"), str(tmp_path / "abox.ttl"),
        str(tmp_path / "nope.ttl"), str(rawdir),
    )
    # CSV가 더 최신 → staleness_flags 있음
    assert len(r["staleness_flags"]) >= 2
    flagged = {f["artifact"] for f in r["staleness_flags"]}
    assert "tbox" in flagged and "abox" in flagged


def test_trustworthiness_no_sidecar():
    r = _score_trustworthiness(_new_graph())
    assert r["score"] == 0
    assert r["annotated_statements"] == 0


def test_trustworthiness_with_reified():
    from tools.triple_confidence import annotate_triple
    g = _new_graph()
    s = URIRef(f"{DOMAIN_INST_NS}A")
    p = URIRef(f"{DOMAIN_NS}has")
    o = URIRef(f"{DOMAIN_INST_NS}B")
    annotate_triple(g, s, p, o, provenance="csv_direct")
    annotate_triple(g, s, p, URIRef(f"{DOMAIN_INST_NS}C"),
                    provenance="bedrock_llm", confidence=0.4)
    annotate_triple(g, s, p, URIRef(f"{DOMAIN_INST_NS}D"),
                    provenance="owl_rl_1hop")
    r = _score_trustworthiness(g)
    assert r["annotated_statements"] == 3
    assert r["provenance_source_count"] == 3
    assert r["score"] > 0


def test_verifiability_no_prov():
    r = _score_verifiability(_new_graph())
    assert r["score"] == 0


def test_verifiability_with_prov():
    g = _new_graph()
    act = URIRef(f"{PROV}act1")
    g.add((act, RDF.type, URIRef(f"{PROV}Activity")))
    for i in range(5):
        ent = URIRef(f"{PROV}ent{i}")
        g.add((ent, RDF.type, URIRef(f"{PROV}Entity")))
    r = _score_verifiability(g)
    assert r["score"] > 0
    assert r["prov_activity_count"] == 1
    assert r["prov_entity_count"] == 5


def test_representational_consistency_basic():
    g = _basic_graph(10)
    r = _score_representational_consistency(g)
    assert r["score"] > 0
    assert r["namespace_count"] >= 1


def test_representational_consistency_mixed_datatypes():
    g = _new_graph()
    p = URIRef(f"{DOMAIN_NS}measure")
    # 같은 predicate인데 decimal/string 혼용
    g.add((URIRef(f"{DOMAIN_INST_NS}A"), p, Literal("5", datatype=XSD.decimal)))
    g.add((URIRef(f"{DOMAIN_INST_NS}B"), p, Literal("hello", datatype=XSD.string)))
    r = _score_representational_consistency(g)
    assert r["predicates_with_mixed_datatypes"] >= 1


def test_evaluate_farber_end_to_end(tmp_path):
    tbox = tmp_path / "tbox.ttl"
    abox = tmp_path / "abox.ttl"
    g = _basic_graph(10)
    g.serialize(destination=str(tbox), format="turtle")
    # abox는 복사
    import shutil
    shutil.copy(str(tbox), str(abox))
    rawdir = tmp_path / "raw"
    rawdir.mkdir()
    (rawdir / "x.csv").write_text("c\n1\n")

    with patch("tools.farber_dimensions.TBOX_PATH", str(tbox)), \
         patch("tools.farber_dimensions.ABOX_PATH", str(abox)), \
         patch("tools.farber_dimensions.INFERRED_PATH", str(tmp_path / "_nope.ttl")), \
         patch("tools.farber_dimensions.SOURCE_RAWDATA_DIR", str(rawdir)):
        r = evaluate_farber()
    assert "dimensions" in r
    assert "overall_score" in r
    assert len(r["dimensions"]) == 5


def test_mcp_tool(tmp_path):
    from tools.farber_dimensions import evaluate_farber_dimensions
    tbox = tmp_path / "tbox.ttl"
    g = _basic_graph(3)
    g.serialize(destination=str(tbox), format="turtle")
    with patch("tools.farber_dimensions.TBOX_PATH", str(tbox)), \
         patch("tools.farber_dimensions.ABOX_PATH", str(tmp_path / "nope.ttl")), \
         patch("tools.farber_dimensions.INFERRED_PATH", str(tmp_path / "nope.ttl")), \
         patch("tools.farber_dimensions.SOURCE_RAWDATA_DIR", str(tmp_path / "_raw")), \
         patch("tools.farber_dimensions.GENERATED_REPORTS_DIR", str(tmp_path)):
        raw = evaluate_farber_dimensions()
    data = json.loads(raw)
    assert data["success"] is True
    assert data["dimensions"]["timeliness"]["dimension"] == "Timeliness"


def test_citation_in_report(tmp_path):
    with patch("tools.farber_dimensions.TBOX_PATH", str(tmp_path / "nope.ttl")), \
         patch("tools.farber_dimensions.ABOX_PATH", str(tmp_path / "nope.ttl")), \
         patch("tools.farber_dimensions.INFERRED_PATH", str(tmp_path / "nope.ttl")), \
         patch("tools.farber_dimensions.SOURCE_RAWDATA_DIR", str(tmp_path / "_raw")):
        r = evaluate_farber()
    assert "Färber" in r["citation"]
