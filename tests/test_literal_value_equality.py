"""리터럴 값 비교가 datatype 표기 차이를 흡수하는지 고정.

## 왜

RDF 1.1 에서 ``"x"`` 와 ``"x"^^xsd:string`` 은 **같은 값** 인데 rdflib ``==`` 는
datatype 이 ``None`` vs ``xsd:string`` 이라 다르다고 판정한다. 이 리포의 기본
store 는 **Oxigraph** 이고 그것은 리터럴을 항상 ``xsd:string`` 을 붙여 되돌려주므로
``Literal(val)`` 과 비교하면 **영구히 불일치** 한다.

실측 (2026-08-12): ``_inject_ontoclean_annotations`` 의 ``current == [lit]`` 가 매
실행 실패해 63개 클래스 주석을 지우고 다시 쓰면서 ``added=283 / replaced=283`` 을
보고했다 — 실제 그래프 변경은 **0** (isomorphic 동일, 3회 연속 재현).

Memory store 에서는 1회 후 0 이 나오므로, ``RDFLIB_STORE=default`` 로 도는 테스트는
이 결함을 놓친다. 그래서 이 파일은 **기본 store 로 직접 확인** 한다.
"""
from __future__ import annotations

import pytest
from rdflib import Literal
from rdflib.namespace import XSD

from domain.graph_utils import literal_values_equal
from domain.rules_paths import rules_path


@pytest.mark.parametrize("a,b,expected", [
    # RDF 1.1: plain 과 xsd:string 은 같은 값
    (Literal("x"), Literal("x", datatype=XSD.string), True),
    (Literal("x", datatype=XSD.string), Literal("x"), True),
    (Literal("x"), Literal("x"), True),
    # 값이 다르면 다르다
    (Literal("x"), Literal("y"), False),
    # 언어 태그는 값의 일부다
    (Literal("x", lang="ko"), Literal("x", lang="en"), False),
    (Literal("x", lang="ko"), Literal("x", lang="ko"), True),
    (Literal("x", lang="ko"), Literal("x"), False),
    # 그 외 datatype 은 의미를 바꾸므로 엄격히 비교
    (Literal("1", datatype=XSD.integer), Literal("1"), False),
    (Literal(True), Literal("true"), False),
    (Literal(1, datatype=XSD.integer), Literal(1, datatype=XSD.integer), True),
])
def test_literal_value_equality(a, b, expected):
    assert literal_values_equal(a, b) is expected


def test_non_literals_fall_back_to_identity():
    """리터럴이 아니면 기존 ``==`` 동작을 유지한다."""
    from rdflib import URIRef

    u = URIRef("http://example.com/a")
    assert literal_values_equal(u, URIRef("http://example.com/a")) is True
    assert literal_values_equal(u, URIRef("http://example.com/b")) is False
    assert literal_values_equal(u, Literal("a")) is False


# ── THE REGRESSION: step_25 가 기본 store 에서 허위 보고하지 않는다 ──


def _run_step_25(graph):
    from domain.namespaces import DOMAIN_NS
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_25_ontoclean import apply

    return apply(graph, StepContext(domain_ns=DOMAIN_NS))


def test_ontoclean_is_idempotent_on_the_default_store():
    """핵심 회귀: 기본 store(Oxigraph)에서 2회차부터 0 을 보고해야 한다.

    예전에는 매 회 ``added=283 / replaced=283`` 을 보고하는데 그래프는 변하지
    않았다 — "무언가 고쳤다" 로 읽히지만 영구 no-op 이고, 그 사이 진짜 변경은
    카운터에 묻힌다.
    """
    import os

    from rdflib import compare

    from config import TBOX_PATH
    from domain.tbox_utils import _new_graph

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = _new_graph()
    g.parse(TBOX_PATH, format="turtle")

    first = _run_step_25(g)
    snapshot = compare.to_isomorphic(g)
    second = _run_step_25(g)

    assert compare.to_isomorphic(g) == snapshot, "2회차가 그래프를 바꿨다"
    assert second.stats.get("ontoclean_triples_added", 0) == 0, (
        f"변경이 없는데 added 를 보고했다: {second.stats}"
    )
    assert second.stats.get("ontoclean_triples_replaced", 0) == 0, (
        f"변경이 없는데 replaced 를 보고했다: {second.stats}"
    )
    # 1회차는 배포본 상태에 달렸다 — 주석이 이미 있으면 0, S2 재생성 직후처럼
    # 없으면 주입한다(실측 233). 어느 쪽이든 **음수가 아니어야** 하고, 위의 2회차
    # 멱등이 이 검사의 본질이다. 1회차를 0 으로 고정하면 배포본이 새로 생성될 때마다
    # 깨지는 취약한 전제가 된다 (실제로 S2 재실행 후 깨졌다).
    assert first.triples_delta >= 0
    if first.triples_delta > 0:
        # 주입했다면 그 수가 stats 와 일치해야 한다 (카운터 신뢰성).
        assert first.stats.get("ontoclean_triples_added", 0) > 0


# ── PRESERVATION: 주입·교체는 계속 동작한다 ──────────────────────────


def _stub_graph(class_local: str):
    from domain.namespaces import DOMAIN_NS, NS_PREFIX
    from domain.tbox_utils import _new_graph

    g = _new_graph()
    g.parse(data=(
        f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"{NS_PREFIX}:{class_local} a owl:Class .\n"
    ), format="turtle")
    return g


def _first_configured_class():
    import json
    import os

    path = rules_path("ontoclean_labels.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    classes = data.get("classes", data)
    return next(iter(classes), None)


def test_missing_annotations_are_still_injected():
    """PRESERVATION: 주석이 없으면 주입한다 (값 비교가 주입을 막아선 안 된다)."""
    key = _first_configured_class()
    if not key:
        pytest.skip("ontoclean_labels.json 없음")
    g = _stub_graph(key)
    res = _run_step_25(g)
    assert res.stats.get("ontoclean_triples_added", 0) > 0, (
        f"주석이 없는데 주입하지 않았다: {res.stats}"
    )
    assert res.triples_delta > 0


def test_wrong_annotation_is_still_replaced():
    """PRESERVATION: 값이 틀리면 교체한다."""
    from rdflib import RDFS, URIRef

    from domain.namespaces import DOMAIN_NS

    key = _first_configured_class()
    if not key:
        pytest.skip("ontoclean_labels.json 없음")
    g = _stub_graph(key)
    pred = URIRef(DOMAIN_NS.rstrip("#") + "-ontoclean#rigidity")
    cls = URIRef(DOMAIN_NS + key)
    g.add((cls, pred, Literal("DEFINITELY_WRONG")))

    res = _run_step_25(g)
    values = [str(o) for o in g.objects(cls, pred)]
    assert "DEFINITELY_WRONG" not in values, f"틀린 값을 남겼다: {values}"
    assert len(values) == 1, f"교체가 아니라 중복 추가됐다: {values}"
    assert res.stats.get("ontoclean_triples_replaced", 0) > 0
    assert RDFS  # import 사용 표시
