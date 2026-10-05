"""domain/range 타입 정합성 — DatatypeProperty 축을 보는가.

## 왜 (2026-09-04, S9.5 전량 노출 실측)

이 검사는 ``subjects(RDF.type, OWL.ObjectProperty)`` 만 순회했다. KG mutation 감사
(``sample_size=12`` → applied 10 / caught 2) 에서 미검출 8건 중 **3건이 이 갭**이었고
셋 다 같은 DP(``airEmissionMonitoringConcentration``) 를 표적으로 했다::

    M1/swap_domain    DP domain 을 무관한 클래스로 교체       → 무발화
    M1/delete_domain  DP domain 선언 제거                     → 무발화
    M6/swap_range     DP range 를 datatype 대신 **클래스** 로  → 무발화

SHACL(S4)이 DP domain/range 필수를 보지만 S9.5 는 validate_kg 만 돌린다 — KG 축에는
이 판정이 전무했다.

## 이 파일이 주장하는 것

세 축이 각각 발화하는 것과, **오탐하지 않는 것**. 특히 ``owl:Thing`` domain 은
universal(제약 없음)이고 미선언과 다르다 — ``_axis`` 가 둘 다 None 으로 내므로 갈라
주지 않으면 정상 DP 를 결함으로 보고한다 (이 리포가 세 번 물린 부류).
"""
from __future__ import annotations

import os

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL, XSD

from domain.namespaces import DOMAIN_NS
from tools.validation_support.checks.semantic import check_domain_range_conformance

INST = str(DOMAIN_NS).rstrip("#") + "/instances#"


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def I(name: str) -> URIRef:  # noqa: E743 — 인스턴스 IRI 헬퍼
    return URIRef(INST + name)


def _tbox(*, domain=None, range_=XSD.decimal, declare_domain=True) -> Graph:
    """``Air`` 클래스 + DP ``airConc``. domain/range 를 인자로 바꾼다."""
    tb = Graph()
    for cls in ("Air", "Alarm", "Monitoring"):
        tb.add((D(cls), RDF.type, OWL.Class))
    tb.add((D("Air"), RDFS.subClassOf, D("Monitoring")))
    dp = D("airConc")
    tb.add((dp, RDF.type, OWL.DatatypeProperty))
    if declare_domain:
        tb.add((dp, RDFS.domain, domain if domain is not None else D("Air")))
    if range_ is not None:
        tb.add((dp, RDFS.range, range_))
    return tb


def _abox() -> Graph:
    """``Air`` 개체 하나가 ``airConc`` 리터럴을 갖는다."""
    g = Graph()
    g.add((I("Air_1"), RDF.type, D("Air")))
    g.add((I("Air_1"), D("airConc"), Literal("1.5", datatype=XSD.decimal)))
    return g


def _run(tb: Graph, g: Graph | None = None) -> dict:
    return check_domain_range_conformance(g if g is not None else _abox(), tb)


# ── 세 축이 발화하는가 (심은 결함) ──────────────────────────────────────


def test_dp_domain_mismatch_is_caught():
    """M1/swap_domain 재현 — domain 이 실제 주어 타입과 다르면 위반."""
    res = _run(_tbox(domain=D("Alarm")))
    assert res["passed"] is False
    assert len(res["dp_violations"]) == 1, res["dp_violations"]
    v = res["dp_violations"][0]
    assert v["issue"] == "DP domain 불일치"
    assert v["expected"] == "Alarm" and v["actual"] == ["Air"]


def test_dp_used_without_domain_is_caught():
    """M1/delete_domain 재현 — A-Box 가 쓰는데 domain 선언이 없으면 위반."""
    res = _run(_tbox(declare_domain=False))
    assert res["passed"] is False
    assert res["dp_used_without_domain"] == ["airConc"], res


def test_dp_range_pointing_at_class_is_caught():
    """M6/swap_range 재현 — DP range 가 datatype 이 아니면 위반."""
    res = _run(_tbox(range_=D("Air")))
    assert res["passed"] is False
    assert len(res["dp_non_datatype_range"]) == 1
    assert res["dp_non_datatype_range"][0]["actual"] == "Air"


# ── PRESERVATION: 오탐하지 않는가 ──────────────────────────────────────


def test_correct_dp_declaration_passes():
    res = _run(_tbox())
    assert res["passed"] is True, res
    assert res["declared_datatype_properties"] == 1
    assert res["dp_domain_checkable"] == 1
    assert res["dp_checked_triples"] == 1


def test_subclass_subject_is_compatible_with_parent_domain():
    """domain 이 부모 클래스면 자식 개체도 정합이다 (계층을 무시하면 대량 오탐)."""
    res = _run(_tbox(domain=D("Monitoring")))
    assert res["passed"] is True, res["dp_violations"]


def test_owl_thing_domain_is_universal_not_undeclared():
    """``owl:Thing`` domain 은 제약 없음(통과)이고 미선언이 **아니다**.

    ``_axis`` 는 둘 다 None 으로 내므로 갈라 주지 않으면 정상 DP 를 "domain 없이
    사용" 으로 오보고한다 — 이 리포가 세 번 물린 버그 부류다.
    """
    res = _run(_tbox(domain=OWL.Thing))
    assert res["passed"] is True, res
    assert res["dp_used_without_domain"] == [], (
        "universal(owl:Thing) domain 을 미선언으로 오판했다"
    )


def test_unused_dp_without_domain_is_not_reported():
    """A-Box 가 쓰지 않는 DP 는 이 축의 대상이 아니다 (선언 완전성은 SHACL 소관)."""
    tb = _tbox(declare_domain=False)
    empty = Graph()
    empty.add((I("Air_1"), RDF.type, D("Air")))
    res = _run(tb, empty)
    assert res["passed"] is True, res
    assert res["dp_used_without_domain"] == []


def test_untyped_subject_is_not_a_violation():
    """주어에 rdf:type 이 없으면 판정 불가다 — 위반으로 세지 않는다."""
    g = Graph()
    g.add((I("X"), D("airConc"), Literal("1.5", datatype=XSD.decimal)))
    res = _run(_tbox(), g)
    assert res["passed"] is True, res["dp_violations"]


def test_foreign_dp_is_out_of_scope():
    """외래 네임스페이스 DP 는 이 T-Box 의 책임이 아니다."""
    tb = _tbox()
    foreign = URIRef("http://purl.org/dc/terms/identifier")
    tb.add((foreign, RDF.type, OWL.DatatypeProperty))
    res = _run(tb)
    assert res["passed"] is True
    assert res["declared_datatype_properties"] == 1, "외래 DP 를 세었다"


def test_op_axis_unchanged_by_dp_addition():
    """OP 축 필드는 그대로 유지된다 (기존 소비자 형태 보존)."""
    res = _run(_tbox())
    for key in ("checked_properties", "checked_triples",
                "declared_object_properties", "domain_checkable_ops",
                "range_checkable_ops", "universal_or_undeclared_skipped",
                "violations"):
        assert key in res, f"기존 OP 축 필드 {key} 가 사라졌다"


# ── 산출물 확인 ────────────────────────────────────────────────────────


def test_shipped_artifacts_pass_dp_axis():
    """배포 T-Box/A-Box 에서 DP 축이 깨끗한가.

    baseline 이 FAIL 이면 그 축은 mutation catch 를 등록할 수 없다 (이 리포의
    "고정 예산이 표본을 굶겼다" 와 같은 계측 눈멂). 추가 전 실측: DP 264개 전부
    domain 보유 / range 가 클래스인 것 0 / 리터럴 445,044건 위반 0.
    """
    from config import ABOX_PATH, TBOX_PATH

    if not (os.path.exists(TBOX_PATH) and os.path.exists(ABOX_PATH)):
        pytest.skip("배포 산출물 없음")
    from domain.tbox_utils import load_graph

    tb = Graph()
    tb.parse(TBOX_PATH, format="turtle")
    g, _ = load_graph()
    res = check_domain_range_conformance(g, tb)
    assert res["dp_violations"] == [], res["dp_violations"]
    assert res["dp_used_without_domain"] == [], res["dp_used_without_domain"]
    assert res["dp_non_datatype_range"] == [], res["dp_non_datatype_range"]
    assert res["dp_domain_checkable"] > 0, "DP 축이 한 건도 검사되지 않았다"
    assert res["dp_checked_triples"] > 0, "DP 리터럴 트리플을 하나도 안 봤다"


def test_real_mutators_are_now_caught():
    """카탈로그의 실제 mutator 3개를 적용해 이번엔 잡히는지 확인한다.

    단위 픽스처가 통과해도 실제 mutant 형태에서 무발화일 수 있다 (이 리포의
    "산출물로 확인하라"). 이 셋이 바로 2026-09-04 S9.5 의 미검출 3건이다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from domain.tbox_utils import load_graph
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    wanted = ["M1_domain_range/swap_domain", "M1_domain_range/delete_domain",
              "M6_reference/swap_range"]
    missing = [w for w in wanted if w not in catalog]
    if missing:
        pytest.skip(f"mutator 카탈로그에 없음: {missing}")

    g, _ = load_graph()
    for name in wanted:
        base = Graph()
        base.parse(TBOX_PATH, format="turtle")
        mutated, info = apply_mutator(base, catalog[name])
        if not info.get("applied"):
            pytest.skip(f"{name} 적용 불가: {info.get('reason')}")
        res = check_domain_range_conformance(g, mutated)
        assert res["passed"] is False, (
            f"{name} 을 심었는데 domain/range 정합성이 통과했다 — DP 축이 무발화다"
        )
