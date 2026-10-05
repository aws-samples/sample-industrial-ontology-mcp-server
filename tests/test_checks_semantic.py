"""Tests for tools/validation_support/checks/semantic.py — Session 12."""
from __future__ import annotations

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.validation_support.checks.semantic import (
    check_domain_range_conformance,
    check_property_coverage,
    check_tbox_fitness,
    invalidate_tacit_cache,
    load_tacit_classes_cached,
)


def _cls(n):
    return URIRef(f"{DOMAIN_NS}{n}")


def _inst(n):
    return URIRef(f"{DOMAIN_INST_NS}{n}")


def test_domain_range_matches():
    g = _new_graph()
    tbox = _new_graph()
    has_x = _cls("hasX")
    eq_cls = _cls("Equipment")
    st_cls = _cls("Status")
    tbox.add((eq_cls, RDF.type, OWL.Class))
    tbox.add((st_cls, RDF.type, OWL.Class))
    tbox.add((has_x, RDF.type, OWL.ObjectProperty))
    tbox.add((has_x, RDFS.domain, eq_cls))
    tbox.add((has_x, RDFS.range, st_cls))

    eq = _inst("E1")
    st = _inst("S1")
    g.add((eq, RDF.type, eq_cls))
    g.add((st, RDF.type, st_cls))
    g.add((eq, has_x, st))
    r = check_domain_range_conformance(g, tbox)
    assert r["passed"] is True
    assert r["violations"] == []


def test_domain_range_violation():
    g = _new_graph()
    tbox = _new_graph()
    has_x = _cls("hasX")
    eq = _cls("Equipment")
    wrong = _cls("WrongClass")
    st = _cls("Status")
    for c in (eq, wrong, st):
        tbox.add((c, RDF.type, OWL.Class))
    tbox.add((has_x, RDF.type, OWL.ObjectProperty))
    tbox.add((has_x, RDFS.domain, eq))
    tbox.add((has_x, RDFS.range, st))
    # subject가 WrongClass 타입 → domain 불일치
    bad = _inst("B1")
    good_obj = _inst("S1")
    g.add((bad, RDF.type, wrong))
    g.add((good_obj, RDF.type, st))
    g.add((bad, has_x, good_obj))
    r = check_domain_range_conformance(g, tbox)
    assert r["passed"] is False
    assert any(v["issue"] == "domain 불일치" for v in r["violations"])


def test_property_coverage_empty_tbox():
    r = check_property_coverage(_new_graph(), _new_graph())
    assert r["passed"] is True
    assert "프로퍼티 정의 없음" in r["message"]


def test_property_coverage_pct():
    g = _new_graph()
    tbox = _new_graph()
    used = _cls("used_prop")
    unused = _cls("unused_prop")
    tbox.add((used, RDF.type, OWL.DatatypeProperty))
    tbox.add((unused, RDF.type, OWL.DatatypeProperty))
    # used_prop만 A-Box에서 사용
    g.add((_inst("A"), used, URIRef(f"{DOMAIN_NS}something")))
    r = check_property_coverage(g, tbox)
    assert r["defined_properties"] == 2
    assert r["used_properties"] == 1
    assert r["coverage_pct"] == 50.0


def test_tbox_fitness_no_classes():
    r = check_tbox_fitness(_new_graph())
    assert r["passed"] is True
    assert "클래스 없음" in r["message"]


def test_tbox_fitness_with_csv(tmp_path):
    tbox = _new_graph()
    tbox.add((_cls("Equipment"), RDF.type, OWL.Class))
    tbox.add((_cls("Alarm"), RDF.type, OWL.Class))
    tbox.add((_cls("Orphan"), RDF.type, OWL.Class))
    csv_dir = tmp_path / "raw"
    csv_dir.mkdir()
    (csv_dir / "Equipment.csv").write_text("id\n")
    (csv_dir / "Alarm.csv").write_text("id\n")
    # Orphan은 CSV 없음

    invalidate_tacit_cache()
    r = check_tbox_fitness(
        tbox, source_rawdata_dir=str(csv_dir),
        source_tacit_dir=str(tmp_path / "_tacit_nope"),
    )
    assert r["tbox_classes"] == 3
    assert r["classes_with_source"] == 2
    assert "Orphan" in r["over_engineered"]


def test_load_tacit_classes_no_dir(tmp_path):
    invalidate_tacit_cache()
    r = load_tacit_classes_cached(str(tmp_path / "nope"))
    assert r == set()


def test_load_tacit_classes_cache(tmp_path):
    tacit_dir = tmp_path / "tacit"
    tacit_dir.mkdir()
    t = _new_graph()
    t.add((_inst("I1"), RDF.type, _cls("TacitClass")))
    (tacit_dir / "a.ttl").write_text(t.serialize(format="turtle"))
    invalidate_tacit_cache()
    r1 = load_tacit_classes_cached(str(tacit_dir))
    r2 = load_tacit_classes_cached(str(tacit_dir))
    assert "TacitClass" in r1
    assert r1 == r2


def test_reexports_from_kg_validation():
    from tools.kg_validation import (
        _check_domain_range_conformance,
        _check_property_coverage,
        _check_tbox_fitness,
        _load_tacit_classes_cached,
    )
    assert callable(_check_domain_range_conformance)
    assert callable(_check_property_coverage)
    assert callable(_check_tbox_fitness)
    assert callable(_load_tacit_classes_cached)


def test_package_reexports():
    from tools.validation_support.checks import (
        check_domain_range_conformance as a,
    )
    from tools.validation_support.checks import (
        check_property_coverage as b,
    )
    from tools.validation_support.checks import (
        check_tbox_fitness as c,
    )
    from tools.validation_support.checks import (
        load_tacit_classes_cached as d,
    )
    for fn in (a, b, c, d):
        assert callable(fn)
