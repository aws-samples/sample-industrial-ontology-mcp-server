"""Tests for tools/roundtrip.py — Q4."""
from __future__ import annotations

import csv
import json
from unittest.mock import patch

from rdflib import OWL, RDF, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph


def test_guess_pk_column():
    from tools.roundtrip import _guess_pk_column
    assert _guess_pk_column(["Name", "equipmentId", "Value"]) == "equipmentId"
    assert _guess_pk_column(["id", "x"]) == "id"
    assert _guess_pk_column(["x_id", "y"]) == "x_id"
    assert _guess_pk_column([]) is None


def test_read_csv(tmp_path):
    from tools.roundtrip import _read_csv
    p = tmp_path / "t.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "value"])
        w.writerow(["1", "a"])
        w.writerow(["2", "b"])
    h, rows = _read_csv(str(p))
    assert h == ["id", "value"]
    assert len(rows) == 2
    assert rows[0]["id"] == "1"


def test_reconstruct_class_rows():
    from tools.roundtrip import _reconstruct_class_rows
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS
    cls = URIRef(f"{ns}Item")
    g.add((cls, RDF.type, OWL.Class))
    u1 = URIRef(f"{inst}Item_001")
    g.add((u1, RDF.type, cls))
    g.add((u1, URIRef(f"{ns}itemId"), Literal("I001")))
    g.add((u1, URIRef(f"{ns}itemName"), Literal("Widget")))

    recon = _reconstruct_class_rows(g, "Item")
    assert len(recon) == 1
    assert recon[0]["itemId"] == "I001"
    assert recon[0]["itemName"] == "Widget"


def test_cell_diff_perfect_match():
    from tools.roundtrip import _cell_diff
    orig = [{"id": "A", "name": "x"}, {"id": "B", "name": "y"}]
    recon = [{"id": "A", "name": "x"}, {"id": "B", "name": "y"}]
    r = _cell_diff(orig, recon, pk="id")
    assert r["row_recall_pct"] == 100.0
    assert r["cell_recall_pct"] == 100.0
    assert r["missing_rows"] == 0


def test_cell_diff_missing_and_extras():
    from tools.roundtrip import _cell_diff
    orig = [{"id": "A", "name": "x"}, {"id": "B", "name": "y"}]
    recon = [{"id": "A", "name": "x", "bonus": "derived"}]
    r = _cell_diff(orig, recon, pk="id")
    assert r["matched_rows"] == 1
    assert r["missing_rows"] == 1  # B 없음
    assert r["cell_extras"] >= 1  # bonus


def test_roundtrip_class_full(tmp_path):
    from tools.roundtrip import roundtrip_class
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS
    cls = URIRef(f"{ns}Item")
    g.add((cls, RDF.type, OWL.Class))
    for i in range(3):
        u = URIRef(f"{inst}Item_{i:03d}")
        g.add((u, RDF.type, cls))
        g.add((u, URIRef(f"{ns}itemId"), Literal(f"I{i:03d}")))
        g.add((u, URIRef(f"{ns}itemName"), Literal(f"name_{i}")))

    csv_path = tmp_path / "items.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["itemId", "itemName"])
        for i in range(3):
            w.writerow([f"I{i:03d}", f"name_{i}"])

    r = roundtrip_class(g, str(csv_path), "Item", limit=10)
    assert r["row_recall_pct"] == 100.0
    assert r["cell_recall_pct"] == 100.0


def test_run_roundtrip_fidelity_skips_missing(tmp_path):
    from tools.roundtrip import run_roundtrip_fidelity
    g = _new_graph()
    mapping = {"FakeTable": "FakeClass"}
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps({"table_class_mapping": mapping}))
    with patch("tools.roundtrip._MAPPING_PATH", str(mapping_file)), \
         patch("tools.roundtrip.SOURCE_RAWDATA_DIR", str(tmp_path)), \
         patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
        r = run_roundtrip_fidelity()
    assert r["per_class"][0]["skipped"] == "csv missing"


def test_mcp_tool_runs(tmp_path):
    from tools.roundtrip import verify_roundtrip_fidelity
    g = _new_graph()
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps({"table_class_mapping": {}}))
    with patch("tools.roundtrip._MAPPING_PATH", str(mapping_file)), \
         patch("tools.roundtrip.GENERATED_REPORTS_DIR", str(tmp_path)), \
         patch("domain.tbox_utils.load_graph", return_value=(g, 0)):
        raw = verify_roundtrip_fidelity()
    data = json.loads(raw)
    assert data["success"] is True
