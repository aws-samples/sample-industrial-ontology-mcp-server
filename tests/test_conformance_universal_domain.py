"""domain/range 정합성 검사가 ``owl:Thing`` 을 universal 로 다루는지 고정.

## 왜

``check_domain_range_conformance`` 는 등록 조건이 ``if domains and ranges`` 였다.
그래서 domain 이 ``owl:Thing`` 인 OP 는 **range 검사까지 함께 잃었다** — 두 축을
하나의 AND 로 묶었기 때문이다.

실측 (2026-08-11 배포 T-Box): ``owl:Thing`` domain OP 62개가 **전부 range 는 구체
클래스** 였고, 그 62개에 실린 A-Box 트리플 **12,846건** 이 무검사 상태였다
(``steelmakingUsesEquipment`` 4,320 / ``rollingUsesEquipment`` 4,320 /
``hasMonitoringPoint`` 1,560 …). 검사 커버리지 167/229 = 72.9%.

``owl:Thing`` 은 **universal class** 라 제약이 없다 (통과) — 미선언(판정 불가) 과
구분해야 한다. 이 리포는 그 혼동으로 같은 버그가 여러 곳에 퍼진 이력이 있어
``structural.py`` 가 ``None`` 센티넬로 구분한다. 같은 의미론을 이 검사에도 쓴다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, Graph, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, NS_PREFIX
from tools.validation_support.checks.semantic import check_domain_range_conformance

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def C(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def I(name: str) -> URIRef:                                    # noqa: E743
    return URIRef(DOMAIN_INST_NS + name)


def _tbox(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


_CONCRETE = _tbox(
    f"{NS_PREFIX}:Alpha a owl:Class .\n{NS_PREFIX}:Beta a owl:Class .\n"
    f"{NS_PREFIX}:Other a owl:Class .\n"
    f"{NS_PREFIX}:concreteOp a owl:ObjectProperty ; "
    f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
)

_THING_DOMAIN = _tbox(
    f"{NS_PREFIX}:Alpha a owl:Class .\n{NS_PREFIX}:Beta a owl:Class .\n"
    f"{NS_PREFIX}:Other a owl:Class .\n"
    f"{NS_PREFIX}:thingOp a owl:ObjectProperty ; "
    f"rdfs:domain owl:Thing ; rdfs:range {NS_PREFIX}:Beta .\n"
)


def _abox(*triples) -> Graph:
    g = Graph()
    g.add((I("a1"), RDF.type, C("Alpha")))
    g.add((I("b1"), RDF.type, C("Beta")))
    g.add((I("x1"), RDF.type, C("Other")))
    for t in triples:
        g.add(t)
    return g


# ── THE REGRESSION: owl:Thing domain 이어도 range 는 검사한다 ──────────


def test_thing_domain_op_still_gets_its_range_checked():
    """핵심 회귀: domain 이 universal 이어도 range 위반은 잡아야 한다.

    두 축을 AND 로 묶으면 이 위반이 조용히 통과한다 — 실측 12,846 트리플이 그
    상태였다.
    """
    res = check_domain_range_conformance(
        _abox((I("a1"), C("thingOp"), I("x1"))), _THING_DOMAIN,
    )
    assert res["passed"] is False, "universal domain OP 의 range 위반을 놓쳤다"
    assert res["violations"][0]["issue"] == "range 불일치"
    assert res["violations"][0]["expected"] == "Beta"


def test_thing_domain_op_is_registered_for_checking():
    """등록 자체가 돼야 한다 — 등록에서 빠지면 어떤 위반도 볼 수 없다."""
    res = check_domain_range_conformance(
        _abox((I("a1"), C("thingOp"), I("b1"))), _THING_DOMAIN,
    )
    assert res["checked_properties"] == 1, "universal domain OP 가 등록에서 빠졌다"
    assert res["range_checkable_ops"] == 1
    assert res["domain_checkable_ops"] == 0
    assert res["universal_or_undeclared_skipped"] == 0


# ── PRESERVATION: universal 은 "통과" 이고 "판정 불가" 가 아니다 ────────


def test_universal_domain_never_reports_a_domain_violation():
    """PRESERVATION: ``owl:Thing`` domain 은 어떤 주어 타입도 허용한다.

    universal 을 "미선언" 으로 오판하면 정상 데이터가 전부 위반으로 뒤집힌다.
    """
    res = check_domain_range_conformance(
        _abox(
            (I("a1"), C("thingOp"), I("b1")),
            (I("x1"), C("thingOp"), I("b1")),      # 전혀 다른 타입의 주어
        ),
        _THING_DOMAIN,
    )
    assert res["passed"] is True, f"universal domain 을 제약으로 오판했다: {res['violations']}"


def test_undeclared_axis_is_skipped_not_guessed():
    """domain/range 가 아예 없으면 그 축은 건너뛴다 (추측하지 않는다)."""
    tbox = _tbox(
        f"{NS_PREFIX}:Alpha a owl:Class .\n{NS_PREFIX}:Beta a owl:Class .\n"
        f"{NS_PREFIX}:noDomain a owl:ObjectProperty ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    res = check_domain_range_conformance(
        _abox((I("x1"), C("noDomain"), I("b1"))), tbox,
    )
    assert res["passed"] is True, "미선언 축을 근거로 위반을 만들었다"
    assert res["domain_checkable_ops"] == 0
    assert res["range_checkable_ops"] == 1


def test_op_with_neither_axis_is_not_registered():
    """두 축 모두 검사 불가면 등록하지 않는다 (checked_properties 를 부풀리지 않는다)."""
    tbox = _tbox(
        f"{NS_PREFIX}:Alpha a owl:Class .\n"
        f"{NS_PREFIX}:bare a owl:ObjectProperty .\n"
    )
    res = check_domain_range_conformance(_abox(), tbox)
    assert res["checked_properties"] == 0
    assert res["universal_or_undeclared_skipped"] == 1


# ── PRESERVATION: 구체 domain·range 검사는 그대로 동작한다 ────────────


@pytest.mark.parametrize("triple,issue", [
    ((I("x1"), C("concreteOp"), I("b1")), "domain 불일치"),
    ((I("a1"), C("concreteOp"), I("x1")), "range 불일치"),
])
def test_concrete_axes_still_catch_violations(triple, issue):
    """기존 계약 보존: 두 축이 구체적이면 양쪽 위반을 모두 잡는다."""
    res = check_domain_range_conformance(_abox(triple), _CONCRETE)
    assert res["passed"] is False
    assert res["violations"][0]["issue"] == issue


def test_concrete_axes_pass_on_valid_data():
    """PRESERVATION: 정당한 데이터는 통과한다."""
    res = check_domain_range_conformance(
        _abox((I("a1"), C("concreteOp"), I("b1"))), _CONCRETE,
    )
    assert res["passed"] is True, f"정상 데이터를 위반으로 봤다: {res['violations']}"
    assert res["domain_checkable_ops"] == 1
    assert res["range_checkable_ops"] == 1


def test_subclass_compatibility_is_preserved():
    """PRESERVATION: 하위 클래스 인스턴스는 상위 클래스 domain 을 만족한다."""
    tbox = _tbox(
        f"{NS_PREFIX}:Alpha a owl:Class .\n{NS_PREFIX}:Beta a owl:Class .\n"
        f"{NS_PREFIX}:SubAlpha a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Alpha .\n"
        f"{NS_PREFIX}:concreteOp a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    g = Graph()
    g.add((I("s1"), RDF.type, C("SubAlpha")))
    g.add((I("b1"), RDF.type, C("Beta")))
    g.add((I("s1"), C("concreteOp"), I("b1")))
    res = check_domain_range_conformance(g, tbox)
    assert res["passed"] is True, f"subclass 호환을 깨뜨렸다: {res['violations']}"


def test_deployed_tbox_reaches_full_op_coverage():
    """실측 고정: 배포 T-Box 의 OP 전수가 최소 한 축은 검사 대상이 된다.

    커버리지 167/229 (72.9%) → 229/229 (100%). 이 수치가 내려가면 어떤 OP 가
    검사에서 빠진 것이고, 그것은 A-Box 트리플이 무검사로 통과한다는 뜻이다.
    """
    import os

    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    tbox = Graph()
    tbox.parse(TBOX_PATH, format="turtle")
    res = check_domain_range_conformance(Graph(), tbox)
    declared = res["declared_object_properties"]
    assert declared > 0
    assert res["checked_properties"] == declared, (
        f"OP {declared - res['checked_properties']}개가 검사에서 빠졌다"
    )
    # range 축은 전수 검사 가능해야 한다 (owl:Thing domain OP 도 range 는 구체적).
    assert res["range_checkable_ops"] == declared


def test_domain_only_op_does_not_get_a_phantom_range_check():
    """range 가 없고 domain 만 구체인 OP — range 축 가드가 필요한 경로.

    등록 조건이 "둘 다 None 이면 skip" 이므로 이 OP 는 등록된다. 그때 range 축
    가드가 없으면 ``expected["range"] = None`` 을 기대 클래스로 비교해 정상
    데이터를 위반으로 만든다.
    """
    tbox = _tbox(
        f"{NS_PREFIX}:Alpha a owl:Class .\n{NS_PREFIX}:Beta a owl:Class .\n"
        f"{NS_PREFIX}:domainOnly a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Alpha .\n"
    )
    res = check_domain_range_conformance(
        _abox((I("a1"), C("domainOnly"), I("b1"))), tbox,
    )
    assert res["passed"] is True, (
        f"range 미선언인데 range 위반을 만들었다: {res['violations']}"
    )
    assert res["checked_properties"] == 1
    assert res["domain_checkable_ops"] == 1
    assert res["range_checkable_ops"] == 0
    # 그리고 domain 축은 여전히 살아 있어야 한다.
    bad = check_domain_range_conformance(
        _abox((I("x1"), C("domainOnly"), I("b1"))), tbox,
    )
    assert bad["passed"] is False, "domain-only OP 의 domain 검사를 잃었다"
    assert bad["violations"][0]["issue"] == "domain 불일치"
