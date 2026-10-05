"""Step 15c — A-Box 가 쓰는 미선언 OP 를 T-Box 에 선언하는지 고정.

## 왜

A-Box 생성기는 FK 컬럼 이름에서 OP 이름을 **동적으로 만들고 T-Box 선언을 검사하지
않는다**. tacit 도 ``tacit_rules.json`` 이 정한 이름을 그대로 쓴다.

실측 (2026-08-12, 병합 A-Box 725,730 트리플 rdflib 파싱): object 가 IRI 인 도메인
술어 51종 중 **37종이 미선언** 이고 **52,471 트리플** 을 쓴다. 같은 클래스쌍에
선언된 OP 는 값이 0건인 경우가 많다 — 즉 이름만 어긋났다:

  EquipmentMaster→EquipmentStatus: 선언 ``hasEquipmentStatus`` 0행 /
                                   미선언 ``equipmentStatusOfEquipment`` 6,000행

미선언 술어는 domain/range 제약이 없어 어떤 데이터든 위반 없이 통과하고,
딕셔너리·CQ 는 그 이름을 몰라 질의가 0행을 반환한다.

rename 이 아니라 **선언 주입 + owl:equivalentProperty** 다 — 유령 OP 다수가
``owl:Restriction`` 의 ``onProperty`` 대상이라 이름을 바꾸면 dangling 이 된다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, NS_PREFIX
from tools.quality_steps import step_15c_undeclared_op_backfill as step
from tools.quality_steps._base import StepContext

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def I(name: str) -> URIRef:                                     # noqa: E743
    return URIRef(DOMAIN_INST_NS + name)


def _tbox(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


_CLASSES = (
    f"{NS_PREFIX}:Alpha a owl:Class .\n"
    f"{NS_PREFIX}:Beta a owl:Class .\n"
)


def _observe(monkeypatch, usage: dict | None):
    """A-Box 관측을 스텁한다 (725,730 트리플 파싱을 피한다)."""
    monkeypatch.setattr(step, "_observed_op_usage", lambda ns: usage)


def _usage(name: str, count: int, doms=("Alpha",), rngs=("Beta",)) -> dict:
    return {name: {"count": count, "domains": set(doms), "ranges": set(rngs)}}


# ── THE REGRESSION: 미선언 OP 를 선언한다 ─────────────────────────────


def test_undeclared_op_gets_declared(monkeypatch):
    """핵심 회귀: A-Box 가 쓰는 이름이 T-Box 에 없으면 선언한다."""
    _observe(monkeypatch, _usage("observedOp", 6000))
    g = _tbox(_CLASSES)
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("observedOp"), RDF.type, OWL.ObjectProperty) in g, "선언하지 않았다"
    assert res.stats["undeclared_ops_declared"] == 1
    # 관측된 타입으로 domain/range 를 붙인다.
    assert D("Alpha") in set(g.objects(D("observedOp"), RDFS.domain))
    assert D("Beta") in set(g.objects(D("observedOp"), RDFS.range))


def test_already_declared_op_is_left_alone(monkeypatch):
    """PRESERVATION: 이미 선언된 OP 는 손대지 않는다 (멱등성도 여기서 온다)."""
    _observe(monkeypatch, _usage("existingOp", 100))
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:existingOp a owl:ObjectProperty ; "
          f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta ; "
          f'rdfs:label "curated"@en .\n'
    )
    snapshot = set(g)
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert set(g) == snapshot, "이미 선언된 OP 를 변경했다"
    assert res.stats["undeclared_ops_declared"] == 0


def test_equivalent_property_links_the_empty_synonym(monkeypatch):
    """같은 쌍의 **값 0건** 선언 OP 와 ``owl:equivalentProperty`` 로 잇는다.

    기존 이름으로 질의해도 추론이 연결하게 하는 것이 목적이다 — rename 하면
    Restriction 이 dangling 이 된다.
    """
    _observe(monkeypatch, _usage("observedOp", 6000))
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:emptySynonym a owl:ObjectProperty ; "
          f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("observedOp"), OWL.equivalentProperty, D("emptySynonym")) in g
    assert (D("emptySynonym"), OWL.equivalentProperty, D("observedOp")) in g
    assert res.stats["undeclared_op_equiv_linked"] == 1
    # 기존 OP 는 살아 있어야 한다 (rename 아님).
    assert (D("emptySynonym"), RDF.type, OWL.ObjectProperty) in g


def test_op_with_data_is_not_linked_as_a_synonym(monkeypatch):
    """PRESERVATION: **값이 있는** 선언 OP 와는 equivalent 로 잇지 않는다.

    둘 다 실데이터를 가진 서로 다른 관계일 수 있다 — 동일시하면 추론이 두 관계를
    섞어 잘못된 결론을 만든다.
    """
    usage = _usage("observedOp", 6000)
    usage["busySynonym"] = {"count": 500, "domains": {"Alpha"}, "ranges": {"Beta"}}
    _observe(monkeypatch, usage)
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:busySynonym a owl:ObjectProperty ; "
          f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("observedOp"), OWL.equivalentProperty, D("busySynonym")) not in g, (
        "값이 있는 OP 를 동의어로 묶었다"
    )
    assert res.stats["undeclared_op_equiv_linked"] == 0


# ── 틀린 제약을 만들지 않는다 ─────────────────────────────────────────


def test_ambiguous_types_do_not_get_domain_or_range(monkeypatch):
    """관측 타입이 여러 개면 domain/range 를 붙이지 않는다.

    틀린 제약은 없는 제약보다 나쁘다 — A-Box 인스턴스를 위반으로 만들거나
    unsatisfiable 을 유발한다.
    """
    _observe(monkeypatch, _usage("multiOp", 50, doms=("Alpha", "Beta"), rngs=("Beta",)))
    g = _tbox(_CLASSES)
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("multiOp"), RDF.type, OWL.ObjectProperty) in g, "선언은 해야 한다"
    assert list(g.objects(D("multiOp"), RDFS.domain)) == [], (
        "관측 타입이 여러 개인데 domain 을 추측했다"
    )
    assert D("Beta") in set(g.objects(D("multiOp"), RDFS.range)), (
        "range 는 유일하므로 붙여야 한다"
    )
    assert res.stats["undeclared_op_ambiguous_types"] == 1


def test_name_declared_as_datatype_property_is_skipped(monkeypatch):
    """DP 로 이미 선언된 이름은 건드리지 않는다 — 타입 충돌은 HermiT 가 막는다."""
    _observe(monkeypatch, _usage("clashName", 10))
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:clashName a owl:DatatypeProperty ; "
          f"rdfs:domain {NS_PREFIX}:Alpha .\n"
    )
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("clashName"), RDF.type, OWL.ObjectProperty) not in g, (
        "DP 이름을 OP 로 중복 선언했다 (타입 충돌)"
    )
    assert res.stats["undeclared_ops_declared"] == 0


# ── FAIL-CLOSED: 관측 불가면 주입하지 않는다 ──────────────────────────


def test_scan_failure_withholds_injection(monkeypatch):
    """A-Box 파싱 실패(``None``)면 아무것도 주입하지 않는다.

    0건과 판정 불가를 구분하지 않으면 "미선언 없음" 으로 오독한다.
    """
    _observe(monkeypatch, None)
    g = _tbox(_CLASSES)
    snapshot = set(g)
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert set(g) == snapshot
    assert res.stats.get("undeclared_op_withheld_scan_failed") is True
    assert res.triples_delta == 0


def test_no_abox_yet_is_a_clean_no_op(monkeypatch):
    """A-Box 가 아직 없으면(빈 관측) 조용히 no-op — S3 는 S7 보다 앞선다."""
    _observe(monkeypatch, {})
    g = _tbox(_CLASSES)
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert res.stats["undeclared_ops_declared"] == 0
    assert res.triples_delta == 0


def test_env_toggle_disables_the_step(monkeypatch):
    """``TBOX_UNDECLARED_OP_BACKFILL=false`` 로 끌 수 있다."""
    monkeypatch.setenv("TBOX_UNDECLARED_OP_BACKFILL", "false")
    _observe(monkeypatch, _usage("observedOp", 6000))
    g = _tbox(_CLASSES)
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("observedOp"), RDF.type, OWL.ObjectProperty) not in g
    assert res.stats.get("undeclared_op_backfill_disabled") is True


# ── 라벨이 기존 것과 겹치지 않는다 ───────────────────────────────────


def test_injected_labels_do_not_duplicate_existing_ones(monkeypatch):
    """라벨이 겹치면 ``duplicate_label`` 경고가 늘어난다 (실측 2건).

    동의어라 의미는 같지만 게이트는 그것을 알 수 없으므로 출처를 접미로 남긴다.
    """
    _observe(monkeypatch, _usage("maintenanceHistoryEquipment", 500))
    # 기존 OP 가 같은 humanize 결과를 이미 라벨로 갖고 있다.
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:other a owl:ObjectProperty ; "
          f'rdfs:label "maintenance history equipment"@en, "정비 이력 설비"@ko .\n'
    )
    step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    labels = {
        (str(o), o.language) for o in g.objects(None, RDFS.label)
        if isinstance(o, Literal)
    }
    counts: dict[tuple[str, str], int] = {}
    for pair in labels:
        counts[pair] = counts.get(pair, 0) + 1
    injected = {
        (str(o), o.language)
        for o in g.objects(D("maintenanceHistoryEquipment"), RDFS.label)
    }
    assert injected, "라벨을 붙이지 않았다"
    for text, lang in injected:
        others = [
            s for s in g.subjects(RDFS.label, Literal(text, lang=lang))
            if s != D("maintenanceHistoryEquipment")
        ]
        assert others == [], f"라벨 '{text}'@{lang} 이 {others} 와 겹친다"


def test_real_pipeline_leaves_no_undeclared_ops(s3_output_ttl):
    """실측 고정: S3 산출물에 A-Box 가 쓰는 미선언 OP 가 없어야 한다.

    이 수치가 0 이 아니면 그 관계는 domain/range 제약을 받지 않고 딕셔너리·CQ 도
    모른다 — 질의가 0행을 정답처럼 반환한다.
    """
    import glob
    import os

    if s3_output_ttl is None or not os.path.exists("data/generated/abox/a_box.ttl"):
        pytest.skip("픽스처 없음")
    tbox = Graph()
    tbox.parse(data=s3_output_ttl, format="turtle")
    declared = {
        str(o).split("#")[-1] for o in tbox.subjects(RDF.type, OWL.ObjectProperty)
        if str(o).startswith(DOMAIN_NS)
    }

    abox = Graph()
    paths = ["data/generated/abox/a_box.ttl", "data/generated/abox/master_data.ttl"]
    paths += glob.glob("data/source/tacit/*.ttl")
    for p in paths:
        if os.path.exists(p):
            abox.parse(p, format="turtle")
    undeclared = {
        str(p).split("#")[-1]
        for _, p, o in abox
        if p != RDF.type and isinstance(o, URIRef) and str(p).startswith(DOMAIN_NS)
        and str(p).split("#")[-1] not in declared
    }
    assert undeclared == set(), (
        f"A-Box 가 쓰는데 T-Box 에 없는 OP {len(undeclared)}종: {sorted(undeclared)[:5]}"
    )


# ── equivalentProperty 를 **잇지 말아야** 하는 경우 ─────────────────
#
# 이 스텝은 "미선언 0" 을 달성하려고 owl:equivalentProperty 를 주입한다. 그런데
# peer 선택이 (domain, range) 서명만 봤기 때문에 **의미가 반대인 관계를 동일시**
# 했다. 2026-08-15 실측 손상 2건 (owlrl 로 재현 확인):
#
#   followedBy ≡ directlyFollows AND ≡ directlyPrecedes  (그 둘은 서로 inverseOf)
#     ⟹ 세 OP 전부 대칭. tacit 공정 흐름 3 트리플 → 6 트리플.
#        "고로→제강" 이 "제강→고로" 를 함의한다 (S5 tacit 의 목적 파괴).
#
#   hasDestinationWarehouse ≡ transportationHasOriginWarehouse
#     ⟹ 운송 300건에서 "출발 창고" 질의가 목적지를 반환한다.
#        (TRP00001 실제 출발 WH001 은 KG 에 없다)
#
# 어떤 게이트도 이것을 잡지 못했다: domain/range 가 같아 conformance 통과,
# AsymmetricProperty·propertyDisjointWith 선언 0건이라 HermiT 무반응,
# check_quality_rules 107 이슈에 critical/high 0.


def test_does_not_equate_a_property_with_its_own_inverse(monkeypatch):
    """역관계와 equivalent 로 묶지 않는다 — 묶으면 관계가 대칭이 된다."""
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:aToB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta ; "
        f"owl:inverseOf {NS_PREFIX}:observed .\n"
    )
    _observe(monkeypatch, {
        "observed": {"count": 3, "domains": {"Alpha"}, "ranges": {"Beta"}},
    })
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))

    assert (D("observed"), RDF.type, OWL.ObjectProperty) in g, "선언은 돼야 한다"
    assert (D("observed"), OWL.equivalentProperty, D("aToB")) not in g, (
        "역관계와 equivalent 로 묶였다 — 추론 후 관계가 대칭이 되어 방향이 사라진다"
    )
    assert res.stats["undeclared_op_equiv_skipped_inverse"] >= 1


def test_does_not_equate_opposing_direction_labels(monkeypatch):
    """origin ↔ destination 처럼 반의어 라벨은 동일시하지 않는다.

    판정은 step_22d/22e 가 쓰는 **같은** 공용 가드
    (``ontology_quality._has_opposing_directions``) 를 호출해야 한다.
    """
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:hasOriginWarehouse a owl:ObjectProperty ; "
        f'rdfs:label "운송 출발 창고"@ko ; '
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    _observe(monkeypatch, {
        "hasDestinationWarehouse": {
            "count": 300, "domains": {"Alpha"}, "ranges": {"Beta"},
        },
    })
    # 관측된 OP 의 라벨에 destination 토큰이 들어가도록 이름 기반 라벨 생성에 의존.
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))

    assert (D("hasDestinationWarehouse"), OWL.equivalentProperty,
            D("hasOriginWarehouse")) not in g, (
        "출발/목적 창고를 동일시했다 — '출발지' 질의가 목적지를 반환한다"
    )
    assert res.stats["undeclared_op_equiv_skipped_opposing"] >= 1


def test_does_not_equate_when_multiple_candidates(monkeypatch):
    """동의어 후보가 여러 개면 잇지 않는다 (어느 쪽이 진짜인지 근거 없음).

    domain/range 처리가 이미 쓰는 원칙 — 틀린 공리는 없는 공리보다 나쁘다.
    여러 후보를 다 이으면 그것들끼리도 equivalent 가 되어 서로 다른 관계가 뭉개진다.
    """
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:synonymOne a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
        + f"{NS_PREFIX}:synonymTwo a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    _observe(monkeypatch, {
        "observed": {"count": 5, "domains": {"Alpha"}, "ranges": {"Beta"}},
    })
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))

    linked = list(g.objects(D("observed"), OWL.equivalentProperty))
    assert not linked, f"후보가 2개인데 이었다: {[str(x) for x in linked]}"
    assert res.stats["undeclared_op_equiv_skipped_ambiguous"] >= 1


def test_still_links_a_single_unambiguous_synonym(monkeypatch):
    """정당한 단일 동의어는 여전히 잇는다 (NEGATIVE 방향).

    가드가 과잉 차단하면 이 스텝의 본래 목적(기존 이름으로 질의해도 추론이
    연결)이 사라진다.
    """
    g = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:phantomSynonym a owl:ObjectProperty ; "
        f'rdfs:label "알파의 베타"@ko ; '
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )
    _observe(monkeypatch, {
        "alphaHasBeta": {"count": 42, "domains": {"Alpha"}, "ranges": {"Beta"}},
    })
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))

    assert (D("alphaHasBeta"), OWL.equivalentProperty,
            D("phantomSynonym")) in g, (
        "정당한 단일 동의어를 잇지 않았다 — 가드가 과잉 차단한다"
    )
    assert res.stats["undeclared_op_equiv_linked"] >= 1


def test_process_flow_direction_survives_owl_rl(monkeypatch):
    """**산출물 기반 회귀**: 백필 후 OWL RL 추론에서 공정 순서가 대칭이 되지 않는다.

    카운터가 아니라 추론 결과로 주장한다 — 손상은 카운터상 "6건 연결 성공" 으로
    보고됐고 그게 문제를 가렸다.
    """
    owlrl = pytest.importorskip("owlrl")

    g = _tbox(
        f"{NS_PREFIX}:Step a owl:Class .\n"
        + f"{NS_PREFIX}:directlyFollows a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Step ; rdfs:range {NS_PREFIX}:Step ; "
        f"owl:inverseOf {NS_PREFIX}:directlyPrecedes .\n"
        + f"{NS_PREFIX}:directlyPrecedes a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Step ; rdfs:range {NS_PREFIX}:Step ; "
        f"owl:inverseOf {NS_PREFIX}:directlyFollows .\n"
    )
    _observe(monkeypatch, {
        "followedBy": {"count": 3, "domains": {"Step"}, "ranges": {"Step"}},
    })
    step.apply(g, StepContext(domain_ns=DOMAIN_NS))

    # A-Box 사실: 두 공정의 한 방향 순서.
    g.add((I("S1"), RDF.type, D("Step")))
    g.add((I("S2"), RDF.type, D("Step")))
    g.add((I("S1"), D("followedBy"), I("S2")))
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)

    assert (I("S2"), D("followedBy"), I("S1")) not in g, (
        "추론이 역방향을 파생했다 — equivalentProperty 가 관계를 대칭으로 만들었다. "
        "공정 순서(고로→제강)가 양방향이 되면 S5 tacit 의 목적이 사라진다"
    )
