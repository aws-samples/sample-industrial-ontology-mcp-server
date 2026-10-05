"""ObjectProperty 양방향 연결 — ``owl:inverseOf`` 타입 오류를 잡는가.

## 왜 (2026-09-04 S9.5 실측)

``owl:inverseOf`` 는 **프로퍼티끼리** 맺는 관계다. 대상이 클래스면 OWL 2 DL 위반이고
역방향 자재화가 깨진다. 그런데 이 검사는 그것을 호환성 필터의 ``incompatible_pairs``
로 흘려보냈고, 그 버킷은 **의도적으로 passed 에 반영되지 않는다** (T-Box 결함이 A-Box
양방향성 실패로 번지지 않게 하려는 설계). 그래서 mutation 이 통과했다::

    add_spurious_inverse: equipmentStatusRefersToEquipment
                          owl:inverseOf AirEmissionMonitoring (클래스!)  → 무발화

표적은 A-Box **6,000행이 흐르는 살아있는** OP 였다. 즉 계측 문제(0행 표적)가 아니라
판정 문제였고, KG mutation 감사 미검출 8건 중 유일한 순수 탐지기 갭이었다.

## 이 파일이 주장하는 것

두 사유를 갈랐다는 것: **타입 오류는 passed 에 반영**하고, 실재하는 두 프로퍼티의
**domain/range 이견은 기존대로 보고만** 한다. 후자를 함께 실패시키면 원래 설계 의도를
깨고 A-Box 검증이 T-Box 이견에 오염된다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

from domain.namespaces import DOMAIN_NS
from tools.validation_support.checks.structural import check_bidirectional_op

INST = str(DOMAIN_NS).rstrip("#") + "/instances#"
_IOF = "https://spec.industrialontologies.org/ontology/construct/"


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _tbox_pair() -> Graph:
    """정상 OP 쌍 ``aToB`` ↔ ``bToA`` (domain/range 가 맞물린다)."""
    tb = Graph()
    for cls in ("ClsA", "ClsB"):
        tb.add((D(cls), RDF.type, OWL.Class))
    tb.add((D("aToB"), RDF.type, OWL.ObjectProperty))
    tb.add((D("aToB"), RDFS.domain, D("ClsA")))
    tb.add((D("aToB"), RDFS.range, D("ClsB")))
    tb.add((D("bToA"), RDF.type, OWL.ObjectProperty))
    tb.add((D("bToA"), RDFS.domain, D("ClsB")))
    tb.add((D("bToA"), RDFS.range, D("ClsA")))
    tb.add((D("aToB"), OWL.inverseOf, D("bToA")))
    return tb


def _abox_symmetric() -> Graph:
    # 프로덕션과 **같은 store** 를 써야 한다. 이 검사는 ``SUM(IF(EXISTS {...}))`` 를
    # 쓰는데 기본 rdflib store 의 SPARQL 파서가 그 형태에서 예외를 낸다 (Oxigraph
    # 에서는 정상). 데이터 그래프만 해당된다 — T-Box 는 쿼리 대상이 아니다.
    from domain.tbox_utils import _new_graph

    g = _new_graph()
    a, b = URIRef(INST + "a1"), URIRef(INST + "b1")
    g.add((a, RDF.type, D("ClsA")))
    g.add((b, RDF.type, D("ClsB")))
    g.add((a, D("aToB"), b))
    g.add((b, D("bToA"), a))
    return g


def _run(tb: Graph, g: Graph | None = None) -> dict:
    return check_bidirectional_op(g if g is not None else _abox_symmetric(), tb)


# ── THE REGRESSION: 타입 오류가 passed 를 떨어뜨리는가 ────────────────────


def test_inverse_of_class_is_caught():
    """add_spurious_inverse 재현 — inverse 대상이 클래스면 위반."""
    tb = _tbox_pair()
    tb.add((D("aToB"), OWL.inverseOf, D("ClsB")))          # 클래스를 가리킴
    res = _run(tb)
    assert res["passed"] is False, res
    assert res["inverseof_type_error_count"] == 1, res["inverseof_type_errors"]
    err = res["inverseof_type_errors"][0]
    assert err["side"] == "object" and err["target"] == "ClsB"


def test_inverse_of_datatype_property_is_caught():
    tb = _tbox_pair()
    tb.add((D("someValue"), RDF.type, OWL.DatatypeProperty))
    tb.add((D("aToB"), OWL.inverseOf, D("someValue")))
    res = _run(tb)
    assert res["passed"] is False
    assert res["inverseof_type_error_count"] == 1


def test_inverse_of_undeclared_iri_is_caught():
    tb = _tbox_pair()
    tb.add((D("aToB"), OWL.inverseOf, D("GhostProp")))
    res = _run(tb)
    assert res["passed"] is False
    assert res["inverseof_type_error_count"] == 1


def test_type_error_fails_even_with_no_valid_pairs():
    """유효 쌍이 0이어도 타입 오류는 보고된다 (이른 반환 경로).

    수정 전 이 경로는 ``passed: True`` 를 고정 반환해 타입 오류를 삼켰다.
    """
    tb = Graph()
    tb.add((D("ClsB"), RDF.type, OWL.Class))
    tb.add((D("orphanOp"), RDF.type, OWL.ObjectProperty))
    tb.add((D("orphanOp"), OWL.inverseOf, D("ClsB")))
    from domain.tbox_utils import _new_graph
    res = _run(tb, _new_graph())
    assert res["passed"] is False, res
    assert res["inverseof_type_error_count"] == 1
    assert res["checked_pairs"] == 0


# ── PRESERVATION ───────────────────────────────────────────────────────


def test_normal_inverse_pair_passes():
    res = _run(_tbox_pair())
    assert res["passed"] is True, res
    assert res["inverseof_type_error_count"] == 0
    assert res["checked_pairs"] == 1


def test_foreign_namespace_target_is_not_judged():
    """IOF 프로퍼티의 역관계 선언은 정당하다 — 이 T-Box 에 선언이 없는 것이 정상."""
    tb = _tbox_pair()
    tb.add((D("aToB"), OWL.inverseOf, URIRef(_IOF + "hasSomething")))
    res = _run(tb)
    assert res["inverseof_type_error_count"] == 0, res["inverseof_type_errors"]
    assert res["passed"] is True


def test_domain_range_mismatch_still_only_reported():
    """실재하는 두 프로퍼티의 domain/range 이견은 **기존대로 보고만** 한다.

    이것까지 실패시키면 원래 설계(T-Box 이견이 A-Box 양방향성 실패로 번지지 않게)를
    깬다. 타입 오류와 달리 모델링 판단이 필요한 사안이다.
    """
    tb = _tbox_pair()
    tb.remove((D("bToA"), RDFS.range, D("ClsA")))
    tb.add((D("bToA"), RDFS.range, D("ClsB")))             # 맞물리지 않음
    res = _run(tb)
    assert res["inverseof_type_error_count"] == 0
    assert res["incompatible_count"] == 1, res
    assert res["passed"] is True, "domain/range 이견까지 실패시켰다 — 설계가 바뀌었다"


def test_type_error_is_not_double_classified():
    """타입 오류는 incompatible_pairs 에 중복 분류되지 않는다."""
    tb = _tbox_pair()
    tb.add((D("aToB"), OWL.inverseOf, D("ClsB")))
    res = _run(tb)
    assert res["inverseof_type_error_count"] == 1
    assert res["incompatible_count"] == 0, res["incompatible_pairs"]


def test_existing_fields_preserved():
    res = _run(_tbox_pair())
    for key in ("checked_pairs", "total_pairs", "incompatible_pairs",
                "incompatible_count", "missing", "missing_inverse_count"):
        assert key in res, f"기존 필드 {key} 가 사라졌다"


# ── 산출물 확인 ────────────────────────────────────────────────────────


def test_shipped_tbox_has_no_inverseof_type_error():
    """배포 T-Box baseline — 타입 오류 0이어야 catch 를 등록할 수 있다.

    실측 2026-09-04: inverseOf 82건 전부 도메인-도메인, 타입 오류 0.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from domain.tbox_utils import load_graph

    tb = Graph()
    tb.parse(TBOX_PATH, format="turtle")
    g, _ = load_graph()
    res = check_bidirectional_op(g, tb)
    assert res["inverseof_type_errors"] == [], res["inverseof_type_errors"]
    assert res["passed"] is True, res


def test_real_add_spurious_inverse_mutant_is_caught():
    """카탈로그의 실제 mutator 를 적용해 이번엔 잡히는지 확인한다.

    이것이 2026-09-04 S9.5 의 유일한 순수 탐지기 갭이었다 (표적은 A-Box 6,000행이
    흐르는 살아있는 OP).
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from domain.tbox_utils import load_graph
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    name = "M2_inverse/add_spurious_inverse"
    if name not in catalog:
        pytest.skip(f"mutator 없음: {name}")
    base = Graph()
    base.parse(TBOX_PATH, format="turtle")
    mutated, info = apply_mutator(base, catalog[name])
    if not info.get("applied"):
        pytest.skip(f"적용 불가: {info.get('reason')}")
    g, _ = load_graph()
    res = check_bidirectional_op(g, mutated)
    assert res["passed"] is False, (
        "add_spurious_inverse 를 심었는데 양방향 연결 검사가 통과했다"
    )
    assert res["inverseof_type_error_count"] >= 1, res
