"""Tests for tools/validation_support/checks/referential.py — Session 11."""
from __future__ import annotations

import json

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.validation_support.checks.referential import (
    check_closed_world_fk_unresolved,
    check_closed_world_master_orphan,
    check_dangling_references,
    check_fk_op_coverage,
    check_fk_referential_integrity,
    load_master_instance_uris,
)


def _cls(n):
    return URIRef(f"{DOMAIN_NS}{n}")


def _inst(n):
    return URIRef(f"{DOMAIN_INST_NS}{n}")


def test_fk_ref_no_ops_fallback():
    g = _new_graph()
    r = check_fk_referential_integrity(g, tbox=_new_graph())
    # fallback 목록이 생성되지만 total=0인 속성은 skip 되므로 passed=True 예상
    assert r["passed"] is True


def test_fk_ref_detects_dangling():
    g = _new_graph()
    tbox = _new_graph()
    has_x = _cls("hasX")
    tbox.add((has_x, RDF.type, OWL.ObjectProperty))
    tbox.add((has_x, RDFS.range, _cls("Thing")))
    # typed subject가 있는 정상 object
    typed = _inst("T1")
    g.add((typed, RDF.type, _cls("Thing")))
    # dangling object (typed 없음)
    g.add((_inst("A"), has_x, _inst("Missing")))
    g.add((_inst("A"), has_x, typed))
    r = check_fk_referential_integrity(g, tbox=tbox)
    assert r["passed"] is False
    assert len(r["dangling"]) == 1


def test_dangling_references_passes_with_typed_targets():
    g = _new_graph()
    typed = _inst("Target")
    g.add((typed, RDF.type, _cls("Thing")))
    g.add((_inst("A"), _cls("hasTarget"), typed))
    r = check_dangling_references(g)
    assert r["passed"] is True
    assert r["dangling_count"] == 0


def test_dangling_references_flags_untyped():
    g = _new_graph()
    g.add((_inst("A"), _cls("hasX"), _inst("MissingTarget")))
    r = check_dangling_references(g)
    assert r["dangling_count"] == 1


def test_fk_op_coverage_no_rules_file_skip(tmp_path):
    r = check_fk_op_coverage(_new_graph(), rules_dir=str(tmp_path),
                              source_rawdata_dir=str(tmp_path))
    assert r["passed"] is True
    assert "건너뜀" in r["message"]


def test_fk_op_coverage_detects_missing(tmp_path):
    """CSV에 FK 컬럼은 있으나 T-Box에 대응 OP가 없음."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "fk_patterns.json").write_text(json.dumps({
        "patterns": {"equipmentid": "EquipmentMaster"},
        "suffix_rules": [],
    }))
    csv_dir = tmp_path / "raw"
    csv_dir.mkdir()
    (csv_dir / "alarm.csv").write_text("id,equipmentID\n1,EQ001\n")
    tbox = _new_graph()  # OP 없음
    r = check_fk_op_coverage(tbox, rules_dir=str(rules_dir),
                              source_rawdata_dir=str(csv_dir))
    assert r["total_fk_columns"] == 1
    assert r["passed"] is False


def test_load_master_instance_uris_missing(tmp_path):
    assert load_master_instance_uris(str(tmp_path / "nope.ttl")) == set()


def test_load_master_instance_uris_parses(tmp_path):
    m = _new_graph()
    m.add((_inst("M1"), RDF.type, _cls("Master")))
    m.add((_inst("M2"), RDF.type, _cls("Master")))
    path = tmp_path / "master.ttl"
    m.serialize(destination=str(path), format="turtle")
    uris = load_master_instance_uris(str(path))
    assert len(uris) == 2


def test_closed_world_master_orphan_all_referenced(tmp_path):
    m = _new_graph()
    m.add((_inst("M1"), RDF.type, _cls("Master")))
    path = tmp_path / "master.ttl"
    m.serialize(destination=str(path), format="turtle")

    g = _new_graph()
    g.add((_inst("M1"), RDF.type, _cls("Master")))
    # transaction에서 M1 참조
    g.add((_inst("A"), _cls("uses"), _inst("M1")))
    r = check_closed_world_master_orphan(g, master_data_path=str(path))
    assert r["passed"] is True
    assert r["orphan_count"] == 0


def test_closed_world_master_orphan_detects(tmp_path):
    m = _new_graph()
    for i in range(10):
        m.add((_inst(f"M{i}"), RDF.type, _cls("Master")))
    path = tmp_path / "master.ttl"
    m.serialize(destination=str(path), format="turtle")

    g = _new_graph()
    for i in range(10):
        g.add((_inst(f"M{i}"), RDF.type, _cls("Master")))
    # 8개만 참조 (2개 고립)
    for i in range(8):
        g.add((_inst("A"), _cls("uses"), _inst(f"M{i}")))
    r = check_closed_world_master_orphan(g, master_data_path=str(path))
    assert r["orphan_count"] == 2


def test_closed_world_master_orphan_tier_threshold(tmp_path):
    """master tier 는 50%, transaction tier 는 60% 기본."""
    m = _new_graph()
    # 10 TrueMaster + 10 TxnMaster
    for i in range(10):
        m.add((_inst(f"TM{i}"), RDF.type, _cls("TrueMaster")))
        m.add((_inst(f"TX{i}"), RDF.type, _cls("TxnMaster")))
    path = tmp_path / "master.ttl"
    m.serialize(destination=str(path), format="turtle")

    g = _new_graph()
    for i in range(10):
        g.add((_inst(f"TM{i}"), RDF.type, _cls("TrueMaster")))
        g.add((_inst(f"TX{i}"), RDF.type, _cls("TxnMaster")))
    # TrueMaster: 9/10 참조 (10% orphan, master 50% 임계치 이내)
    for i in range(9):
        g.add((_inst("A"), _cls("uses"), _inst(f"TM{i}")))
    # TxnMaster: 4/10 참조 (60% orphan, master 50% 초과, transaction 60% 이내)
    for i in range(4):
        g.add((_inst("A"), _cls("uses"), _inst(f"TX{i}")))

    r = check_closed_world_master_orphan(
        g, master_data_path=str(path),
        class_tiers={"TrueMaster": "master", "TxnMaster": "transaction"},
    )
    assert r["passed"] is True, f"expected per-class thresholds honored: {r}"
    assert r["orphan_count"] == 7  # 1 + 6

    # Without class_tiers, both classes default to master (50%); TxnMaster 60% fails.
    r2 = check_closed_world_master_orphan(g, master_data_path=str(path))
    assert r2["passed"] is False, f"expected FAIL when tier info absent: {r2}"


def test_closed_world_fk_unresolved_skip_when_no_ops(tmp_path):
    m = _new_graph()
    m.add((_inst("M1"), RDF.type, _cls("Master")))
    path = tmp_path / "master.ttl"
    m.serialize(destination=str(path), format="turtle")

    g = _new_graph()
    tbox = _new_graph()  # OP 없음
    r = check_closed_world_fk_unresolved(g, tbox, master_data_path=str(path))
    assert r["passed"] is True
    assert "ObjectProperty 없음" in r.get("message", "")


def test_reexports_from_kg_validation():
    """kg_validation의 wrapper가 같은 로직을 수행."""
    from tools.kg_validation import (
        _check_dangling_references,
        _check_fk_op_coverage,
        _check_fk_referential_integrity,
    )
    assert callable(_check_dangling_references)
    assert callable(_check_fk_referential_integrity)
    assert callable(_check_fk_op_coverage)


def test_package_reexports():
    from tools.validation_support.checks import (
        check_closed_world_fk_unresolved as a,
    )
    from tools.validation_support.checks import (
        check_closed_world_master_orphan as b,
    )
    from tools.validation_support.checks import (
        check_dangling_references as c,
    )
    from tools.validation_support.checks import (
        check_fk_op_coverage as d,
    )
    from tools.validation_support.checks import (
        check_fk_referential_integrity as e,
    )
    from tools.validation_support.checks import (
        load_master_instance_uris as f,
    )
    for fn in (a, b, c, d, e, f):
        assert callable(fn)
