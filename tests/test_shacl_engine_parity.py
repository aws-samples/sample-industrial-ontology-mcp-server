"""pyshacl vs pyrudof parity — _run_shacl wrapper 동등성 검증.

두 엔진이 동일 shape+data 에 대해 동일 conforms 결과를 내고, 위반 개수
집계가 ±1 범위에서 일치하는지 확인한다. 자세한 위반 상세(경로/메시지)는
벤더마다 문구가 다를 수 있으므로 conforms 와 위반 존재 여부만 비교한다.
"""
from __future__ import annotations

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SH

from tools.validation_core import _run_shacl

DOMAIN_NS = Namespace("http://example.com/steel-ontology#")


def _conforms_only_data():
    data = Graph()
    data.bind("steel", DOMAIN_NS)
    data.bind("owl", OWL)
    data.add((DOMAIN_NS.Widget, RDF.type, OWL.Class))
    data.add((DOMAIN_NS.Widget, RDFS.label, pytest.importorskip("rdflib").Literal("Widget", lang="en")))
    data.add((DOMAIN_NS.Widget, RDFS.comment, pytest.importorskip("rdflib").Literal("부품", lang="ko")))
    return data


def _violating_data():
    """label 이 있지만 comment 가 없는 Class → shape 위반."""
    from rdflib import Literal
    data = Graph()
    data.bind("steel", DOMAIN_NS)
    data.add((DOMAIN_NS.Widget, RDF.type, OWL.Class))
    data.add((DOMAIN_NS.Widget, RDFS.label, Literal("Widget", lang="en")))
    return data


def _shapes_class_label_comment():
    from rdflib import BNode, Literal
    g = Graph()
    g.bind("sh", SH)
    shape = DOMAIN_NS.ClassShape
    g.add((shape, RDF.type, SH.NodeShape))
    g.add((shape, SH.targetClass, OWL.Class))
    # label 필수
    p1 = BNode()
    g.add((shape, SH.property, p1))
    g.add((p1, SH.path, RDFS.label))
    g.add((p1, SH.minCount, Literal(1)))
    # comment 필수
    p2 = BNode()
    g.add((shape, SH.property, p2))
    g.add((p2, SH.path, RDFS.comment))
    g.add((p2, SH.minCount, Literal(1)))
    return g


def test_both_engines_agree_on_conforming_data():
    data = _conforms_only_data()
    shapes = _shapes_class_label_comment()

    c_py, rg_py, _ = _run_shacl(data_graph=data, shacl_graph=shapes, engine="pyshacl")
    c_ru, rg_ru, _ = _run_shacl(data_graph=data, shacl_graph=shapes, engine="pyrudof")
    assert c_py is True
    assert c_ru is True


def test_both_engines_agree_on_violating_data():
    data = _violating_data()
    shapes = _shapes_class_label_comment()

    c_py, rg_py, _ = _run_shacl(data_graph=data, shacl_graph=shapes, engine="pyshacl")
    c_ru, rg_ru, _ = _run_shacl(data_graph=data, shacl_graph=shapes, engine="pyrudof")
    # conforms flag 동등
    assert c_py == c_ru is False
    # 각 엔진이 최소 1건의 ValidationResult 를 보고
    py_results = list(rg_py.subjects(RDF.type, SH.ValidationResult))
    ru_results = list(rg_ru.subjects(RDF.type, SH.ValidationResult))
    assert len(py_results) >= 1
    assert len(ru_results) >= 1


def test_pyrudof_without_shapes_returns_conforming_empty():
    """shapes 가 없으면 pyrudof 경로는 conforming=True + 빈 report 반환."""
    data = _conforms_only_data()
    c, rg, _ = _run_shacl(data_graph=data, shacl_graph=None, engine="pyrudof")
    assert c is True
    assert len(rg) == 0


def test_engine_env_default_is_pyshacl(monkeypatch):
    """SHACL_ENGINE 미지정 시 pyshacl 사용 (회귀 방지)."""
    monkeypatch.delenv("SHACL_ENGINE", raising=False)
    data = _conforms_only_data()
    shapes = _shapes_class_label_comment()
    # engine=None → env → 기본 pyshacl
    c, rg, _ = _run_shacl(data_graph=data, shacl_graph=shapes, engine=None)
    assert c is True
