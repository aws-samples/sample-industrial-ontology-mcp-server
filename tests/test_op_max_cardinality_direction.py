"""Step 13c — OP 의 ``owl:maxCardinality`` 가 CSV FK **방향**과 맞는가.

## 왜

``step_13b`` 는 ``minCardinality`` + ``DatatypeProperty`` 만 본다. 그래서
**maxCardinality + ObjectProperty** 축은 데이터 대조가 전무했다.

2026-08-30 실측: 이번 S2 가 ``owl:maxCardinality 1`` 을 32개 만들었고 (기준선 0개)
그중 1개가 데이터와 정면 모순이었다::

    steel:EquipmentMaster ⊑ ≤1 steel:hasEquipmentStatus
    Equipment_Status.csv = 6,000행 = 설비 50대 × 시점 120

게이트 3개가 놓쳤다: ``validate_owl_consistency`` 는 A-Box 를 로드하지 않고
(``owl_reasoner.py`` 에 ``ABOX_PATH`` 0건), ``validate_owl_cardinality`` 는
``ensure_inverse_triples`` 를 안 거치는데 이 술어는 a_box.ttl 에 0건이며 역방향
6,000건으로만 존재하고, ``check_shacl_owl_cardinality_sync`` 는 restriction 이
BNode 일 것을 요구하지만 스콜렘화 후 전부 URIRef 다.

## 이 파일이 주장하는 것

**판정 신호가 컬럼 존재 여부가 아니라 컬럼의 역할(PK 인가)이라는 것.** 처음 만든
판정기는 "출처 컬럼이 소유 테이블에 있으면 정합" 이라 32/32 를 OK 로 오판했다 —
``Equipment_ID`` 는 ``Equipment_Master`` 에도 있고 거기서는 PK 이기 때문이다.
그 오판을 되살리는 mutation 이 이 테스트를 깨야 한다.
"""
from __future__ import annotations

import csv
import os

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL, XSD

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.quality_steps import step_13c_op_max_cardinality_direction as step
from tools.quality_steps._base import StepContext

_DCT = URIRef("http://purl.org/dc/terms/source")


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


# ── 픽스처: CSV 두 장 + 그 구조를 반영한 T-Box ──────────────────────────


def _write_csv(directory, name: str, header: list[str], rows: list[list[str]]):
    path = os.path.join(str(directory), f"{name}.csv")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture
def master_detail(tmp_path, monkeypatch):
    """마스터 1:N 디테일 — 마스터 PK 가 디테일에서 반복된다.

    ``MasterTbl.KEY_ID`` 는 unique(PK), ``DetailTbl.KEY_ID`` 는 FK 로 3회 반복.
    즉 마스터 1건이 디테일 3건을 가리킨다.
    """
    raw = tmp_path / "rawdata"
    raw.mkdir()
    _write_csv(raw, "MasterTbl", ["KEY_ID", "NAME"],
               [["K1", "a"], ["K2", "b"]])
    _write_csv(raw, "DetailTbl", ["KEY_ID", "TS", "VAL"],
               [["K1", "t1", "1"], ["K1", "t2", "2"], ["K1", "t3", "3"],
                ["K2", "t1", "4"], ["K2", "t2", "5"], ["K2", "t3", "6"]])
    monkeypatch.setattr("config.SOURCE_RAWDATA_DIR", str(raw), raising=False)
    monkeypatch.setattr(step, "_class_to_table",
                        lambda: {"Master": "MasterTbl", "Detail": "DetailTbl"})
    return raw


def _graph(*, forward_limit: int | None = 1, reverse_limit: int | None = 1,
           source_col: str | None = "KEY_ID") -> Graph:
    """Master ↔ Detail 양방향 OP + 요청된 maxCardinality 제약."""
    g = Graph()
    for cls in ("Master", "Detail"):
        g.add((D(cls), RDF.type, OWL.Class))
    specs = [
        ("hasDetail", "Master", "Detail", forward_limit,
         "Master_hasDetail_maxCardinality"),
        ("detailOfMaster", "Detail", "Master", reverse_limit,
         "Detail_detailOfMaster_maxCardinality"),
    ]
    for name, dom, rng, limit, restr_name in specs:
        prop = D(name)
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.domain, D(dom)))
        g.add((prop, RDFS.range, D(rng)))
        if source_col:
            g.add((prop, _DCT, Literal(source_col)))
        if limit is None:
            continue
        restr = D(restr_name)
        g.add((restr, RDF.type, OWL.Restriction))
        g.add((restr, OWL.onProperty, prop))
        g.add((restr, OWL.maxCardinality,
               Literal(limit, datatype=XSD.nonNegativeInteger)))
        g.add((D(dom), RDFS.subClassOf, restr))
    g.add((D("hasDetail"), OWL.inverseOf, D("detailOfMaster")))
    return g


def _run(g, **env):
    for key, value in env.items():
        os.environ[key] = value
    try:
        return step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    finally:
        for key in env:
            os.environ.pop(key, None)


def _constrained_props(g: Graph) -> set[str]:
    out = set()
    for restr in g.subjects(RDF.type, OWL.Restriction):
        if g.value(restr, OWL.maxCardinality) is None:
            continue
        prop = g.value(restr, OWL.onProperty)
        if list(g.subjects(RDFS.subClassOf, restr)):
            out.add(str(prop).split("#")[-1])
    return out


# ── THE REGRESSION: FK 방향과 반대인 제약을 잡는가 ──────────────────────


def test_reverse_direction_constraint_is_detected(master_detail):
    """마스터→디테일 ``≤1`` 은 데이터가 1:3 이므로 위반이다."""
    g = _graph()
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 1, stats
    detail = stats["op_max_card_violating_detail"][0]
    assert detail["property"] == "hasDetail"
    assert detail["observed_max"] == 3
    assert detail["violation_ratio"] == 1.0
    assert detail["reason"] == "all_owners_violate"


def test_only_the_wrong_direction_is_removed(master_detail):
    """PRESERVATION: 참인 역방향 제약은 살아남는다.

    ``Detail ⊑ ≤1 detailOfMaster`` 는 옳다 (디테일 1건 → 마스터 1대). 둘 다 지우면
    참인 제약을 잃는다 — 실측에서 inverse 쌍 양쪽에 1 이 걸린 것이 32개 중 1쌍뿐이었고
    한쪽만 거짓이었다.
    """
    g = _graph()
    _run(g)
    kept = _constrained_props(g)
    assert "hasDetail" not in kept, "방향이 뒤집힌 제약이 남았다"
    assert "detailOfMaster" in kept, (
        "참인 역방향 제약까지 지웠다 — 데이터는 디테일 1건당 마스터 1대다"
    )


def test_repeating_fk_column_in_own_table_is_valid(master_detail):
    """PRESERVATION: FK 컬럼이 소유 테이블에서 반복되면 행당 1개라 정합.

    이것이 31개가 통과한 이유다. 이 판정을 잃으면 정당한 제약 31개가 삭제된다.
    """
    g = _graph(forward_limit=None)      # 역방향만 제약
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 0, (
        f"반복 FK 컬럼(Detail.KEY_ID)에 걸린 정당한 제약을 위반으로 셌다: {stats}"
    )
    assert "detailOfMaster" in _constrained_props(g)


def test_column_presence_alone_must_not_decide(master_detail):
    """THE ORACLE BUG: 컬럼이 소유 테이블에 **있다** 는 것만으로 판정하면 안 된다.

    ``KEY_ID`` 는 ``MasterTbl`` 에도 있다 — 다만 거기서는 PK 다. 컬럼 존재만 보는
    판정기는 이 케이스를 "정합" 으로 읽어 32/32 를 OK 로 오판했다 (실측). 유효한
    신호는 **unique 여부** 하나뿐이다.
    """
    g = _graph()
    stats = _run(g).stats
    detail = stats["op_max_card_violating_detail"][0]
    # 소유 테이블에 컬럼이 있고(= 존재 판정 통과) 그런데도 위반으로 잡혀야 한다.
    assert detail["owner_table"] == "MasterTbl"
    assert detail["column"] == "KEY_ID"
    assert detail["fk_table"] == "DetailTbl"


# ── 정책: 무엇을 지우고 무엇을 보고만 하는가 ────────────────────────────


def test_partial_violation_is_reported_not_removed(tmp_path, monkeypatch):
    """부분 위반은 제거하지 않는다 (``step_12i`` 의 fail_ratio==1.0 정책).

    데이터 정제로 해결될 수 있고, 공리가 의도된 제약일 수 있다.
    """
    raw = tmp_path / "rawdata"
    raw.mkdir()
    # 마스터 3건 중 1건만 다중 참조 → 위반율 1/3
    _write_csv(raw, "MasterTbl", ["KEY_ID"], [["K1"], ["K2"], ["K3"]])
    _write_csv(raw, "DetailTbl", ["KEY_ID", "TS"],
               [["K1", "t1"], ["K1", "t2"]])
    monkeypatch.setattr("config.SOURCE_RAWDATA_DIR", str(raw), raising=False)
    monkeypatch.setattr(step, "_class_to_table",
                        lambda: {"Master": "MasterTbl", "Detail": "DetailTbl"})

    g = _graph(reverse_limit=None)
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 0, "부분 위반을 지웠다"
    assert stats["op_max_card_partial"] == 1, stats
    assert "hasDetail" in _constrained_props(g), "부분 위반 제약이 제거됐다"


def test_axiom_value_is_never_rewritten(master_detail):
    """공리 값을 데이터 최대치로 **고쳐 쓰지 않는다** (지표 매수 방지).

    이 리포는 공리 값을 CSV 에 맞추는 자동 교정을 기각했다. 표본이 바뀌면 다시
    깨지므로 ``≤1`` 을 ``≤3`` 으로 바꾸는 것은 해답이 아니다 — 제거가 정답이다.
    """
    g = _graph()
    _run(g)
    limits = [int(o) for _, _, o in g.triples((None, OWL.maxCardinality, None))]
    assert 3 not in limits, "관측 최대치를 공리에 박았다 (지표 매수)"
    assert limits == [1], f"역방향 제약의 값이 변했다: {limits}"


def test_warn_mode_does_not_modify_the_graph(master_detail):
    """``warn`` 모드는 감지만 한다."""
    g = _graph()
    snapshot = set(g)
    res = _run(g, TBOX_OP_MAX_CARD_CHECK="warn")
    assert res.stats["op_max_card_violating"] == 1
    assert res.stats["op_max_card_removed"] == 0
    assert set(g) == snapshot, "warn 모드가 그래프를 수정했다"
    assert res.triples_delta == 0


def test_off_mode_skips(master_detail):
    """``off`` 는 스텝을 건너뛰지만 검사 대상 수는 보고한다."""
    g = _graph()
    res = _run(g, TBOX_OP_MAX_CARD_CHECK="off")
    assert "skipped" in res.stats
    assert res.triples_delta == 0
    assert "hasDetail" in _constrained_props(g)


# ── NEGATIVE: 근거가 없으면 건드리지 않는가 ──────────────────────────────


def test_no_source_column_is_undecided(master_detail):
    """``dcterms:source`` 가 없으면 판정을 포기한다 (이름 유추 금지).

    OP 이름은 관계 서술어라 컬럼명과 무관하다. 근거 없이 추측하면 정당한 공리를
    지운다.
    """
    g = _graph(source_col=None)
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 0
    assert stats["op_max_card_undecided"] == 2
    assert stats["op_max_card_undecided_by_reason"] == {"no_source_column": 2}
    assert _constrained_props(g) == {"hasDetail", "detailOfMaster"}


def test_datatype_property_is_left_to_step_13b(master_detail):
    """DP 에 걸린 제약은 건드리지 않는다 (13b 의 소관)."""
    g = Graph()
    g.add((D("Master"), RDF.type, OWL.Class))
    dp = D("masterValue")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, _DCT, Literal("KEY_ID")))
    restr = D("Master_masterValue_maxCardinality")
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, dp))
    g.add((restr, OWL.maxCardinality, Literal(1, datatype=XSD.nonNegativeInteger)))
    g.add((D("Master"), RDFS.subClassOf, restr))
    res = _run(g)
    assert res.stats["op_max_card_checked"] == 0
    assert res.triples_delta == 0


def test_missing_csv_reports_unmeasured_not_zero(tmp_path, monkeypatch):
    """측정 불가를 "위반 0" 과 구분해 보고한다.

    이 리포의 원칙 — 0회 발동을 0회로 정직하게 보고해야 하고, 키 부재나 무발화를
    "정상" 으로 오독하면 안 된다.
    """
    monkeypatch.setattr("config.SOURCE_RAWDATA_DIR", str(tmp_path / "none"),
                        raising=False)
    monkeypatch.setattr(step, "_class_to_table", lambda: {})
    g = _graph()
    res = _run(g)
    assert "skipped" in res.stats, "측정 불가가 위반 0 과 구분되지 않는다"
    assert res.stats["op_max_card_checked"] == 2, (
        "검사 대상 수는 측정 불가 여부와 무관하게 보고해야 한다"
    )
    assert res.triples_delta == 0


def test_min_cardinality_untouched(master_detail):
    """``minCardinality`` 는 이 스텝의 소관이 아니다."""
    g = _graph(forward_limit=None, reverse_limit=None)
    restr = D("Master_hasDetail_minCardinality")
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, D("hasDetail")))
    g.add((restr, OWL.minCardinality, Literal(1, datatype=XSD.nonNegativeInteger)))
    g.add((D("Master"), RDFS.subClassOf, restr))
    res = _run(g)
    assert res.triples_delta == 0
    assert (restr, OWL.minCardinality, None) in g


def test_foreign_namespace_property_untouched(master_detail):
    """외래 OP 는 손대지 않는다 (도메인-중립 원칙)."""
    g = Graph()
    foreign = URIRef("https://spec.industrialontologies.org/ontology/core/Core/p")
    g.add((foreign, RDF.type, OWL.ObjectProperty))
    g.add((foreign, _DCT, Literal("KEY_ID")))
    g.add((foreign, RDFS.domain, D("Master")))
    g.add((foreign, RDFS.range, D("Detail")))
    restr = D("Master_foreign_maxCardinality")
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, foreign))
    g.add((restr, OWL.maxCardinality, Literal(1, datatype=XSD.nonNegativeInteger)))
    g.add((D("Master"), RDFS.subClassOf, restr))
    res = _run(g)
    assert res.stats["op_max_card_checked"] == 0, "외래 OP 를 검사 대상에 넣었다"
    assert res.triples_delta == 0


# ── 배선 + 실측 고정 ────────────────────────────────────────────────────


def test_step_registered_before_restriction_dedup():
    """배선: 13c 가 등록돼 있고 step_20(restriction dedup) **앞** 이다.

    20 뒤에 두면 지운 제약이 dedup 과정에서 되살아날 수 있다 (13b 와 같은 이유).
    """
    import tools.quality_steps as qs

    order = [
        fn.__module__.rsplit(".", 1)[-1]
        for lst in (qs._PRE_STEPS, qs._MAIN_PRE_STEP9, qs._STEP9_GROUP,
                    qs._MAIN_POST_STEP9, qs._POST_STEPS)
        for fn in lst
    ]
    assert "step_13c_op_max_cardinality_direction" in order, order
    at = order.index("step_13c_op_max_cardinality_direction")
    assert order.index("step_13_pk_functional_someValuesFrom") < at, (
        "13c 가 제약 생성 스텝(13)보다 앞이면 아직 없는 공리를 검사한다"
    )
    assert at < order.index("step_20_restriction_dedup"), (
        "13c 가 step_20 뒤에 있다 — 지운 제약이 dedup 으로 되살아날 수 있다"
    )


def test_shipped_tbox_has_no_reverse_direction_constraint():
    """실측 고정: 배포 T-Box 에 FK 방향과 반대인 maxCardinality 가 없다.

    이 스텝을 배포 산출물에 돌려 잔여 위반이 0 인지 확인한다 — 단위 픽스처가
    통과해도 실제 T-Box 에서 무발화일 수 있다 (이 리포의 "산출물로 확인하라").
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    stats = step.apply(g, StepContext(domain_ns=DOMAIN_NS)).stats
    if "skipped" in stats:
        pytest.skip(f"측정 불가: {stats['skipped']}")
    assert stats["op_max_card_violating"] == 0, (
        "배포 T-Box 에 FK 방향과 반대인 maxCardinality 가 남아 있다: "
        f"{stats['op_max_card_violating_detail']}"
    )


def test_step_result_contract(master_detail):
    """StepResult 계약 (코디네이터가 stats 를 병합한다)."""
    g = _graph()
    res = _run(g)
    assert res.name == "step_13c_op_max_cardinality_direction"
    assert res.step_number == "13c"
    for key in ("op_max_card_checked", "op_max_card_violating",
                "op_max_card_removed", "op_max_card_partial",
                "op_max_card_undecided"):
        assert key in res.stats, f"stats 에 {key} 가 없다"
    assert NS_PREFIX  # import 사용 표시


# ── tacit 축: CSV 로 판정 불가인 OP 를 암묵지 실측으로 판정하는가 ──────────
#
# CSV FK 컬럼만 보던 판정기는 **암묵지가 채우는 관계** 를 구조적으로 못 봤다.
# ``dcterms:source`` 가 없으니 첫 관문(no_source_column)에서 판정 불가로 빠진다.
#
# 2026-09-03 실측 (S2 재실행 후 배포 T-Box):
#   steel:AirEmissionMonitoring ⊑ ≤1 steel:hasStackEquipment
#     step_13c  : undecided (no_source_column)
#     validate_kg: 위반 1,560건 (개체마다 7~8개)
# 채우는 유일한 출처는 tacit_rules.json 의 Location 버킷 co-location 이고 규칙 자체가
# **다대다임을 명시**한다. 즉 공리(1:1)와 출처(다대다)가 정면 모순이었다.


def _write_tacit(directory, name: str, triples: list[tuple[str, str, str]]):
    """tacit TTL 한 장 — (subject, predicate, object) 는 모두 로컬 이름."""
    lines = [
        f"@prefix steel: <{DOMAIN_NS}> .",
        f"@prefix steel-inst: <{DOMAIN_NS.rstrip('#')}/instances#> .",
        "",
    ]
    lines += [f"steel-inst:{s} steel:{p} steel-inst:{o} ." for s, p, o in triples]
    path = os.path.join(str(directory), f"{name}.ttl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


@pytest.fixture
def tacit_filled(tmp_path, monkeypatch):
    """CSV 에 FK 가 없고 **tacit 이** 관계를 채우는 OP.

    ``Air`` 테이블 4행, tacit 이 각 행을 ``Equip`` 3개에 잇는다 (다대다 버킷 조인).
    """
    raw = tmp_path / "rawdata"
    raw.mkdir()
    _write_csv(raw, "AirTbl", ["MON_ID", "CONC"],
               [["A1", "1"], ["A2", "2"], ["A3", "3"], ["A4", "4"]])
    _write_csv(raw, "EquipTbl", ["EQ_ID", "NAME"],
               [["E1", "x"], ["E2", "y"], ["E3", "z"]])
    tacit = tmp_path / "tacit"
    tacit.mkdir()
    monkeypatch.setattr("config.SOURCE_RAWDATA_DIR", str(raw), raising=False)
    monkeypatch.setattr("config.SOURCE_TACIT_DIR", str(tacit), raising=False)
    monkeypatch.setattr(step, "_class_to_table",
                        lambda: {"Air": "AirTbl", "Equip": "EquipTbl"})
    return tacit


def _tacit_graph_fixture(limit: int = 1) -> Graph:
    """``Air ⊑ ≤limit hasStackEquip`` — ``dcterms:source`` 없음 (CSV FK 부재)."""
    g = Graph()
    for cls in ("Air", "Equip"):
        g.add((D(cls), RDF.type, OWL.Class))
    prop = D("hasStackEquip")
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.domain, D("Air")))
    g.add((prop, RDFS.range, D("Equip")))
    restr = D("Air_hasStackEquip_maxCardinality")
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, prop))
    g.add((restr, OWL.maxCardinality,
           Literal(limit, datatype=XSD.nonNegativeInteger)))
    g.add((D("Air"), RDFS.subClassOf, restr))
    return g


def _all_rows_fanout(n: int = 3) -> list[tuple[str, str, str]]:
    return [
        (f"Air_A{i}", "hasStackEquip", f"Equip_E{j}")
        for i in range(1, 5) for j in range(1, n + 1)
    ]


def test_tacit_supplied_fanout_is_detected(tacit_filled):
    """THE REGRESSION: tacit 이 다대다로 채우는데 공리가 ≤1 이면 위반이다.

    CSV 로는 판정 불가(no_source_column)이므로 tacit 을 2차 근거로 써야 잡힌다.
    """
    _write_tacit(tacit_filled, "colocation", _all_rows_fanout(3))
    g = _tacit_graph_fixture()
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 1, (
        f"tacit 이 채우는 관계를 판정하지 못했다 (판정 불가로 빠졌다): {stats}"
    )
    detail = stats["op_max_card_violating_detail"][0]
    assert detail["evidence"] == "tacit"
    assert detail["observed_max"] == 3
    assert detail["tacit_violators"] == 4
    assert detail["owner_rows"] == 4, "분모가 소유 테이블 행 수여야 한다"
    assert detail["denominator"] == "owner_rows"
    assert detail["violation_ratio"] == 1.0
    assert detail["reason"] == "all_owners_violate_tacit"
    assert "hasStackEquip" not in _constrained_props(g), "위반 공리가 남았다"


def test_tacit_fanout_within_limit_is_preserved(tacit_filled):
    """PRESERVATION: tacit 팬아웃이 공리를 만족하면 지우지 않는다."""
    _write_tacit(tacit_filled, "colocation", _all_rows_fanout(1))
    g = _tacit_graph_fixture()
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 0, (
        f"tacit 실측이 ≤1 을 만족하는데 위반으로 셌다: {stats}"
    )
    assert "hasStackEquip" in _constrained_props(g), (
        "정당한 제약을 지웠다 — tacit 은 개체당 1개만 잇는다"
    )


def test_tacit_partial_violation_is_reported_not_removed(tacit_filled):
    """PRESERVATION: 일부 개체만 위반하면 보고만 한다.

    분모를 "tacit 주체 수" 로 잡으면 1개만 이어도 비율 1.0 이 되어 나머지가
    만족하는 공리를 지운다. 분모는 소유 클래스 전체 개체 수여야 한다.
    """
    _write_tacit(tacit_filled, "colocation", [
        ("Air_A1", "hasStackEquip", "Equip_E1"),
        ("Air_A1", "hasStackEquip", "Equip_E2"),   # A1 만 위반 (4행 중 1)
    ])
    g = _tacit_graph_fixture()
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 0, (
        f"부분 위반을 전수 위반으로 오판해 공리를 지웠다: {stats}"
    )
    assert stats["op_max_card_partial"] == 1, stats
    assert "hasStackEquip" in _constrained_props(g)


def test_no_tacit_evidence_stays_undecided(tacit_filled):
    """tacit 도 그 술어를 모르면 판정 불가 — "측정 불가" 를 위반으로 읽지 않는다."""
    _write_tacit(tacit_filled, "colocation", [
        ("Air_A1", "someOtherLink", "Equip_E1"),
    ])
    g = _tacit_graph_fixture()
    stats = _run(g).stats
    assert stats["op_max_card_violating"] == 0, stats
    assert stats["op_max_card_undecided"] == 1, (
        f"근거가 없는데 판정한 것으로 집계됐다: {stats}"
    )
    assert "hasStackEquip" in _constrained_props(g)


def test_tacit_axis_respects_warn_mode(tacit_filled):
    """warn 모드는 감지만 하고 지우지 않는다 (CSV 축과 동일 정책)."""
    _write_tacit(tacit_filled, "colocation", _all_rows_fanout(3))
    g = _tacit_graph_fixture()
    stats = _run(g, TBOX_OP_MAX_CARD_CHECK="warn").stats
    assert stats["op_max_card_violating"] == 1
    assert stats["op_max_card_removed"] == 0
    assert "hasStackEquip" in _constrained_props(g)
