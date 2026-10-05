"""Regression: DP range 가 XSD 타입 정확히 하나로 정규화되는가.

2026-08-19 실측: ``equipmentStatusMaintenanceFlag`` 가 **두 번의 S2 실행에서 연속**
SHACL 위반으로 남았고, S3 를 돌려도 고쳐지지 않았다 (S2 원본에서 S3 전체 재실행으로
확인):

    S2 (00:34)  range = xsd:boolean + 리터럴 "xsd:string"   -> S3 후에도 그대로
    S2 (06:46)  range = xsd:boolean + xsd:string            -> S3 후에도 그대로

원인은 ``step_09c`` 의 두 사각지대였다:

1. ``isinstance(range_val, URIRef)`` 안에서만 교정 → 리터럴 range 를 아예 안 봤다.
2. 안전 prefix 값이 여러 개면 전부 ``continue`` → 다중 선언을 정리하지 않았다.

SHACL shape 은 ``sh:maxCount 1`` 을 요구한다 (rules/policy/tbox_shapes.ttl). 즉 게이트는
매번 잡았지만 S3 가 못 고쳐 사람이 손으로 지워야 했다.

**이 파일이 주장하는 것**: 카운터가 아니라 **그래프 산출물** 이다 —
 (1) 리터럴 range 가 IRI 로 복원되는가 (정보 손실 없이),
 (2) 다중 선언에서 **의도된 타입** 이 남는가 (폴백 xsd:string 이 아니라),
 (3) 판정 근거가 없으면 **건드리지 않고 보고** 하는가.
"""

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

from tools.quality_steps import step_09c_dp_range_xsd as step
from tools.quality_steps._base import StepContext

NS = Namespace("http://example.com/steel-ontology#")


def _graph(*range_values, prop="flagProp"):
    """DP 하나에 주어진 range 값들을 붙인 그래프."""
    g = Graph()
    p = NS[prop]
    g.add((p, RDF.type, OWL.DatatypeProperty))
    for v in range_values:
        g.add((p, RDFS.range, v))
    return g, p


def _run(g):
    return step.apply(g, StepContext(domain_ns=str(NS)))


def _ranges(g, p):
    return list(g.objects(p, RDFS.range))


# ── 리터럴 range: IRI 로 복원하는가 ──────────────────────────────────────

def test_literal_range_recovered_to_xsd_iri():
    """``rdfs:range "xsd:string"`` (리터럴) 이 IRI ``xsd:string`` 으로 복원된다.

    예전 구현은 URIRef 만 봤으므로 이 값이 그대로 남아 OWL 2 DL 위반이 됐다.
    """
    g, p = _graph(Literal("xsd:string"))
    res = _run(g)
    assert _ranges(g, p) == [XSD.string]
    assert res.stats["dp_range_literal_fixed"] == 1


def test_literal_range_preserves_the_named_type():
    """리터럴이 담고 있던 타입 이름을 살린다 — 폴백으로 뭉개지 않는다.

    ``"xsd:decimal"`` 을 xsd:string 으로 바꾸면 LLM 이 고른 타입 정보가 사라진다.

    예시가 원래 ``"xsd:date"`` 였는데 그 타입은 **OWL 2 datatype map 밖**이라
    (아래 ``test_owl2_unsupported_*``) 복원 즉시 ``xsd:dateTime`` 으로 대체된다.
    두 계약("정보를 살린다" / "OWL 2 안의 타입만 남긴다")이 그 한 값에서만
    충돌했으므로, map 안에 있는 타입으로 예시를 옮겨 **정보 보존 계약 자체**를
    검사한다. date 의 거동은 전용 테스트가 따로 고정한다.
    """
    g, p = _graph(Literal("xsd:decimal"))
    _run(g)
    assert _ranges(g, p) == [XSD.decimal], "리터럴의 타입 이름이 유실됐다"


@pytest.mark.parametrize("lex,expected", [
    ("xsd:boolean", XSD.boolean),
    ("boolean", XSD.boolean),
    ("http://www.w3.org/2001/XMLSchema#integer", XSD.integer),
])
def test_literal_range_forms(lex, expected):
    """prefixed / bare / 전체 IRI 문자열 형태를 모두 복원한다."""
    g, p = _graph(Literal(lex))
    _run(g)
    assert _ranges(g, p) == [expected]


# ── 다중 range: 의도된 타입을 남기는가 ──────────────────────────────────

def test_multi_range_keeps_intended_type_over_fallback():
    """xsd:string(폴백) + xsd:boolean(의도) → boolean 을 남긴다.

    실측 케이스. CSV 값이 0/1 이고 프롬프트가 ``*flag → xsd:boolean`` 을 지시하므로
    boolean 이 정답이고 string 이 잉여였다.
    """
    g, p = _graph(XSD.boolean, XSD.string)
    res = _run(g)
    assert _ranges(g, p) == [XSD.boolean], (
        "폴백(xsd:string) 이 남고 의도된 타입이 지워졌다"
    )
    assert res.stats["dp_range_multi_pruned"] == 1


def test_multi_range_literal_and_iri_combined():
    """리터럴 + IRI 혼재도 단일 IRI 로 수렴한다 (실측 08-19 00:34 형태)."""
    g, p = _graph(XSD.boolean, Literal("xsd:string"))
    _run(g)
    assert _ranges(g, p) == [XSD.boolean]


def test_shacl_maxcount_satisfied_after_step():
    """정리 후 어떤 DP 도 range 2개 이상을 갖지 않는다 (전역 불변식)."""
    g = Graph()
    for i, vals in enumerate([
        (XSD.boolean, XSD.string),
        (XSD.string, Literal("xsd:date")),
        (XSD.integer,),
    ]):
        p = NS[f"p{i}"]
        g.add((p, RDF.type, OWL.DatatypeProperty))
        for v in vals:
            g.add((p, RDFS.range, v))
    _run(g)
    for p in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        assert len(list(g.objects(p, RDFS.range))) <= 1, (
            f"{p} 가 range 를 2개 이상 갖는다 — SHACL maxCount 1 위반"
        )


# ── NEGATIVE: 판정 근거가 없으면 건드리지 않는가 ────────────────────────

def test_ambiguous_multi_range_left_untouched_and_reported():
    """폴백이 아닌 타입이 둘 이상이면 **고르지 않고 보고** 한다.

    조용히 하나를 고르면 데이터 타입이 뒤바뀌어 A-Box 적재가 깨지고 원인을 추적할
    수 없다. SHACL 위반으로 남겨 사람이 CSV 를 보고 정하게 한다.
    """
    g, p = _graph(XSD.boolean, XSD.integer)
    res = _run(g)
    assert set(_ranges(g, p)) == {XSD.boolean, XSD.integer}, (
        "판정 근거가 없는데 임의로 하나를 골랐다"
    )
    assert res.stats["dp_range_multi_ambiguous"] == 1
    assert res.stats["dp_range_multi_ambiguous_samples"], (
        "판정 불가 항목이 보고되지 않았다 — 조용한 실패"
    )


def test_single_valid_range_untouched():
    """정상 단일 range 는 건드리지 않는다 (동작 보존)."""
    g, p = _graph(XSD.boolean)
    res = _run(g)
    assert _ranges(g, p) == [XSD.boolean]
    assert res.triples_delta == 0
    assert res.stats["dp_range_multi_pruned"] == 0


def test_class_uri_range_still_normalized():
    """기존 동작 — 클래스 URI range 는 xsd:string 으로 교정한다 (HermiT 크래시 방지)."""
    g, p = _graph(NS.Supplier)
    res = _run(g)
    assert _ranges(g, p) == [XSD.string]
    assert res.stats["dp_range_fixed"] == 1


def test_foreign_namespace_dp_untouched():
    """도메인 밖 DP 는 손대지 않는다 (외래 온톨로지 보존)."""
    g = Graph()
    foreign = URIRef("https://spec.industrialontologies.org/ontology/core/Core/x")
    g.add((foreign, RDF.type, OWL.DatatypeProperty))
    g.add((foreign, RDFS.range, XSD.boolean))
    g.add((foreign, RDFS.range, XSD.string))
    res = _run(g)
    assert len(list(g.objects(foreign, RDFS.range))) == 2, (
        "외래 DP 를 수정했다 — 도메인-중립 원칙 위반"
    )
    assert res.triples_delta == 0


# ── OWL 2 datatype map: HermiT 이 온톨로지를 열 수 있는가 ───────────────
#
# 2026-08-30: S2 가 DP 4개(``purchaseOrderOrderDate`` 등)에 ``xsd:date`` 를 남겨
# HermiT 이 온톨로지를 **아예 열지 못했다**:
#
#     UnsupportedDatatypeException: The datatype 'xsd:date' is not part of the
#     OWL 2 datatype map ... therefore, HermiT cannot handle this datatype.
#
# 검증기가 열지 못하면 S4·S9.1 이 통째로 사라진다 — 이 리포의 "검증기 실패 =
# 검증 없음" 과 같은 형태다 (owl:imports 한 줄이 unsat 4건을 덮었다).


def test_owl2_unsupported_range_is_replaced():
    """THE REGRESSION: ``xsd:date`` 는 남지 않는다 (HermiT 이 열 수 없다)."""
    g, p = _graph(XSD.date)
    res = _run(g)
    assert _ranges(g, p) == [XSD.dateTime], (
        "xsd:date 가 남았다 — HermiT 이 온톨로지를 열지 못해 S4 검증이 사라진다"
    )
    assert res.stats["dp_range_owl2_unsupported_fixed"] == 1


def test_owl2_unsupported_date_and_datetime_both_declared():
    """실측 형태: ``date`` + ``dateTime`` 을 함께 선언한 경우.

    이전에는 다중 range 정리가 "폴백(xsd:string) 아닌 타입이 둘이라 판정 근거가
    없다" 며 **둘 다 남겼고**, 남은 ``date`` 가 HermiT 을 죽였다. 비지원 타입
    대체가 다중 판정 **앞** 이어야 이 경우가 dateTime 하나로 수렴한다.
    """
    g, p = _graph(XSD.date, XSD.dateTime)
    res = _run(g)
    assert _ranges(g, p) == [XSD.dateTime], (
        f"date/dateTime 이 하나로 수렴하지 않았다: {_ranges(g, p)}"
    )
    assert res.stats["dp_range_multi_ambiguous"] == 0, (
        "판정 불가로 분류해 둘 다 남겼다 — 대체가 다중 판정보다 뒤에 있다"
    )


@pytest.mark.parametrize("unsupported,expected", [
    (XSD.date, XSD.dateTime),      # 값 승격 가능 → 시각 의미 보존
    (XSD.duration, XSD.string),
    (XSD.gYear, XSD.string),
    (XSD.gMonthDay, XSD.string),
])
def test_owl2_unsupported_types_replaced(unsupported, expected):
    """OWL 2 map 밖 타입 전부가 map 안의 타입으로 대체된다."""
    g, p = _graph(unsupported)
    _run(g)
    assert _ranges(g, p) == [expected]


def test_literal_date_resurrection_is_caught():
    """THE ROOT CAUSE: 리터럴에서 복원한 ``date`` 도 대체된다.

    ``step_08_xsd_date_to_datetime`` (파이프라인 index 11) 은 URIRef ``XSD.date``
    만 매칭하므로 리터럴 ``"xsd:date"`` 를 지나친다. 그 뒤 이 스텝(index 16)의
    ``_recover_from_literal`` 이 그것을 URIRef ``XSD.date`` 로 **되살리는데**,
    치울 스텝은 더 이상 없다. 2026-08-30 실측 재현::

        start          : "xsd:date" (리터럴)
        after step_08  : "xsd:date" (08 은 URIRef 만 본다)
        after step_09c : xsd:date   ← 이 스텝이 만들어 냈다

    즉 결함의 생산자가 이 스텝이므로 여기서 막아야 한다.
    """
    g, p = _graph(Literal("xsd:date"))
    res = _run(g)
    assert _ranges(g, p) == [XSD.dateTime], (
        "리터럴에서 복원한 xsd:date 가 그대로 남았다 — 이 스텝이 비지원 타입의 "
        "생산자다"
    )
    assert res.stats["dp_range_literal_fixed"] == 1, "복원 자체는 일어나야 한다"
    assert res.stats["dp_range_owl2_unsupported_fixed"] == 1


def test_full_step_order_leaves_no_unsupported_datatype():
    """배선 계약: step_08 → step_09c 순서로 돌려도 비지원 타입이 남지 않는다.

    단위 테스트만으로는 이 결함을 못 잡는다 — 이 리포에는 "산출물로 확인하라"
    (단위 테스트 14건이 초록인데 산출물이 안 바뀌었다) 는 기록이 있다. 두 스텝을
    **등록 순서대로** 실행해 최종 그래프를 검사한다.
    """
    import tools.quality_steps as qs
    from tools.quality_steps import step_08_xsd_date_to_datetime as s08

    order = [fn.__module__.rsplit(".", 1)[-1]
             for lst in (qs._PRE_STEPS, qs._MAIN_PRE_STEP9, qs._STEP9_GROUP,
                         qs._MAIN_POST_STEP9, qs._POST_STEPS) for fn in lst]
    assert order.index("step_08_xsd_date_to_datetime") < \
        order.index("step_09c_dp_range_xsd"), (
            "step_08 이 09c 뒤로 옮겨졌다 — 그렇다면 08 이 09c 의 산출물을 치우므로 "
            "이 테스트의 전제가 달라진다"
        )

    ctx = StepContext(domain_ns=str(NS))
    g = Graph()
    for i, val in enumerate([Literal("xsd:date"), XSD.date,
                             XSD.duration, XSD.dateTime]):
        p = NS[f"dateProp{i}"]
        g.add((p, RDF.type, OWL.DatatypeProperty))
        g.add((p, RDFS.range, val))

    s08.apply(g, ctx)
    step.apply(g, ctx)

    unsupported = [
        (str(s).split("#")[-1], str(o).split("#")[-1])
        for s, _, o in g.triples((None, RDFS.range, None))
        if o in step._OWL2_UNSUPPORTED_RANGE_REPLACEMENT
    ]
    assert not unsupported, (
        f"두 스텝을 순서대로 돌린 뒤에도 OWL 2 비지원 타입이 남았다: {unsupported}"
    )


def test_supported_datatypes_untouched():
    """NEGATIVE: OWL 2 map **안** 의 타입은 건드리지 않는다.

    이 리포의 규칙 — 파괴적 스텝은 "카운터 >= 1" 이 아니라 "정당한 입력을
    보존하는가" 를 주장해야 한다. 대체 표를 넓게 잡으면 정상 타입까지 문자열로
    뭉개져 A-Box 적재의 타입 정보가 사라진다.
    """
    supported = [XSD.dateTime, XSD.string, XSD.boolean, XSD.integer,
                 XSD.decimal, XSD.double, XSD.float, XSD.long, XSD.int,
                 XSD.anyURI, XSD.dateTimeStamp, XSD.nonNegativeInteger]
    g = Graph()
    for i, val in enumerate(supported):
        p = NS[f"ok{i}"]
        g.add((p, RDF.type, OWL.DatatypeProperty))
        g.add((p, RDFS.range, val))
    res = _run(g)
    for i, val in enumerate(supported):
        assert _ranges(g, NS[f"ok{i}"]) == [val], (
            f"{val} 는 OWL 2 map 안인데 대체됐다"
        )
    assert res.stats["dp_range_owl2_unsupported_fixed"] == 0
    assert res.triples_delta == 0


def test_foreign_namespace_unsupported_range_untouched():
    """외래 DP 의 비지원 타입은 손대지 않는다 (도메인-중립 원칙).

    외래 온톨로지의 공리를 우리가 고칠 권한은 없다. 이 리포에는 외래 IRI 를
    도메인 IRI 로 뭉갠 실패가 기록돼 있다 (IOF 42 트리플 소실).
    """
    g = Graph()
    foreign = URIRef("https://spec.industrialontologies.org/ontology/core/Core/d")
    g.add((foreign, RDF.type, OWL.DatatypeProperty))
    g.add((foreign, RDFS.range, XSD.date))
    res = _run(g)
    assert list(g.objects(foreign, RDFS.range)) == [XSD.date], (
        "외래 DP 의 range 를 바꿨다"
    )
    assert res.stats["dp_range_owl2_unsupported_fixed"] == 0


# ── 배선: 파이프라인에 등록돼 있는가 ────────────────────────────────────

def test_step_registered_in_pipeline():
    """스텝이 실제 S3 파이프라인에 등록돼 있다 (배선 누락 방지)."""
    import tools.quality_steps as qs

    registered = set()
    for lst in (qs._PRE_STEPS, qs._MAIN_PRE_STEP9, qs._STEP9_GROUP,
                qs._MAIN_POST_STEP9, qs._POST_STEPS):
        for fn in lst:
            registered.add(getattr(fn, "__module__", "").split(".")[-1])
    assert "step_09c_dp_range_xsd" in registered


def test_step_result_contract():
    """StepResult 계약 (코디네이터가 stats 를 병합한다)."""
    g, _ = _graph(XSD.boolean)
    res = _run(g)
    assert res.name == "step_09c_dp_range_xsd"
    assert res.step_number == "9c"
    for key in ("dp_range_fixed", "dp_range_literal_fixed",
                "dp_range_multi_pruned", "dp_range_multi_ambiguous"):
        assert key in res.stats, f"stats 에 {key} 가 없다"
