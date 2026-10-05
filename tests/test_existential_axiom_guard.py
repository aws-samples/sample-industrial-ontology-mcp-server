"""필수참여(∃) 공리를 CSV 근거 없이 만들지 않는가 — 생성 지점 가드.

2026-08-28 실측. ``step_13`` 은 FK NULL 비율 가드를 갖고 있었고 docstring 이 "OWA
원칙 — 정보 부재 ≠ 위반" 을 명시했다. 그런데 **한 번도 발화하지 못했다**::

    fk_col = op_local.lower().replace("has","").replace("_","") + "id"
    realTimeDataMonitorsSteelmaking → "realtimedatamonitorssteelmakingid"

컬럼명을 문자열 조작으로 추측하므로 그런 컬럼이 없고, 헤더 검사에서 빠져나가
``return False`` (= skip 안 함) 가 됐다. 실측: 위반 OP 5개 전부 ``skip?=False`` 였고
``hasFailureCause`` 는 ``failurecauseid`` 로 그럴듯하게 추측했지만 실제 컬럼은
``Equipment_ID`` 다.

결과로 공리 109개 중 44쌍이 A-Box 에서 위반이고 개체 52,578건이 영향받았다. 추론은
그 개체들을 **자기가 위반하는 someValuesFrom 클래스로 타이핑**한다.

## 수정 — 이름 추측을 값 교집합으로 대체

두 축을 실측한다:

1. **근거** — domain CSV 의 어떤 컬럼이 range 클래스의 PK 값 집합과 겹치는가.
   컬럼명이 아니라 값으로 판정하므로 이름 규칙에 의존하지 않는다.
2. **커버리지** — 그 컬럼이 전수 채워져 있는가. ``someValuesFrom`` 은 "모든 개체가
   이 관계를 갖는다" 는 주장이라 한 행이라도 비면 거짓이다.

효과 (배포 T-Box 재생성): **공리 109 → 45** (58개 차단), **위반 44쌍 → 10쌍**.

## 발견된 두 함정

* **시각 컬럼** — composite PK 테이블은 단일 distinct 컬럼이 ``Timestamp`` 뿐인
  경우가 있어, 두 테이블이 같은 시각을 기록한 사실이 관계 근거로 오인됐다
  (``Real_Time_Data.Timestamp`` ∩ ``Process_Steelmaking_Furnace.Timestamp``).
  동시성은 참조가 아니다 → 식별자 후보에서 제외.
* **매핑 누락** — 40 CSV 중 ``Soil_Monitoring`` 하나가 ``table_class_mapping.json``
  에 없어 판정이 "CSV 없음" 으로 빠져나갔다 → 파일명 정규화 폴백.

## 이 테스트의 방향

"공리가 줄었다" 만 주장하면 전부 차단해도 통과한다. 세 축을 고정한다:

* 차단 — CSV 근거 없는 관계에는 공리를 만들지 않는다
* 보존 — **FK 가 전수 채워진 정당한 관계는 만든다** (과잉 차단 방지)
* 판정불가 — PK 후보를 못 찾으면 통과시킨다 (근거 부재를 단정하지 않는다)
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
    _add_some_values_from_restrictions,
    _check_fk_null_ratio,
    _fk_evidence_for_op,
    _is_time_column,
)

NS = Namespace(str(DOMAIN_NS))
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")


def _skip(op: str, domain: str, rng: str, tbox: Graph) -> bool:
    return _check_fk_null_ratio(
        op, domain, g=tbox, op=NS[op],
        domain_cls=NS[domain], range_cls=NS[rng], steel_str=str(DOMAIN_NS),
    )


@pytest.fixture(scope="module")
def deployed_tbox() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    g = Graph()
    g.parse(str(TBOX), format="turtle")
    return g


# ── 차단: 근거 없는 관계 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "domain", "rng"),
    [
        ("equipmentStatusRelatedFailureCause", "EquipmentStatus", "FailureCause"),
        ("isMaterialIssueOfProductionPlan", "InventoryTransaction", "ProductionPlan"),
        ("waterQualityRelatedSoilMonitoring", "WaterQualityMonitoring", "SoilMonitoring"),
        ("soilMonitoringRelatedWaterQuality", "SoilMonitoring", "WaterQualityMonitoring"),
    ],
)
def test_relations_without_csv_basis_are_skipped(op, domain, rng, deployed_tbox):
    """THE REGRESSION: CSV 에 참조 컬럼이 없으면 공리를 만들지 않는다.

    네 쌍 모두 배포 산출물에서 전량 위반(미보유=전체)이었다.
    """
    assert _skip(op, domain, rng, deployed_tbox) is True


def test_skip_reason_names_the_missing_link(deployed_tbox):
    """왜 막았는지 알려준다 — 조치 판단(배선 복구 vs 공리 완화)에 필요하다."""
    _, _, reason = _fk_evidence_for_op(
        deployed_tbox, NS["isMaterialIssueOfProductionPlan"],
        NS["InventoryTransaction"], NS["ProductionPlan"], str(DOMAIN_NS),
    )
    assert "no_column_in" in reason
    assert "InventoryTransaction" in reason and "ProductionPlan" in reason


# ── 보존: 정당한 관계는 만든다 (NEGATIVE 방향) ──────────────────────────


@pytest.mark.parametrize(
    ("op", "domain", "rng"),
    [
        ("hasRealTimeTag", "RealTimeData", "TagMaster"),
        ("hasAlarmTag", "AlarmEvents", "TagMaster"),
    ],
)
def test_fully_covered_fk_is_allowed(op, domain, rng, deployed_tbox):
    """FK 가 전수 채워진 관계는 공리를 유지한다 — 과잉 차단이면 정당한 공리를 잃는다."""
    assert _skip(op, domain, rng, deployed_tbox) is False


def test_allowed_reason_names_the_column(deployed_tbox):
    """허용 근거로 실제 컬럼명을 준다 — 이름 추측이 아니라 값 대조의 산물이다."""
    has_basis, ratio, reason = _fk_evidence_for_op(
        deployed_tbox, NS["hasRealTimeTag"], NS["RealTimeData"], NS["TagMaster"],
        str(DOMAIN_NS),
    )
    assert has_basis is True
    assert ratio >= 0.999
    assert reason == "column=Tag_ID"


def test_partially_filled_fk_is_skipped(deployed_tbox):
    """근거는 있지만 부분 채움이면 차단한다 — 커버리지 축 독립 검증.

    ``someValuesFrom`` 은 "모든 개체가 이 관계를 갖는다" 는 주장이라 한 행이라도
    비면 거짓이다. 실측 픽스처: ``Fuel_Consumption.Energy_Source_ID`` 가 144/180
    (80%) 로 채워져 있다 — 40 CSV 중 유일한 부분 채움 FK 다.

    이 축이 없으면 "근거 유무" 하나만 검사하는 것과 구별되지 않는다 (실제로 두
    분기가 중복이라 mutation 3종이 생존했다).
    """
    has_basis, ratio, reason = _fk_evidence_for_op(
        deployed_tbox, NS["hasFuelEnergySource"],
        NS["FuelConsumption"], NS["EnergySourceMaster"], str(DOMAIN_NS),
    )

    assert has_basis is True, "근거 자체는 있어야 한다 (컬럼이 실재한다)"
    assert reason == "column=Energy_Source_ID"
    assert 0.7 < ratio < 0.9, f"채움률이 {ratio:.3f} — 80% 근처여야 한다"
    assert _skip("hasFuelEnergySource", "FuelConsumption",
                 "EnergySourceMaster", deployed_tbox) is True


def test_coverage_threshold_demands_full_fill():
    """임계가 전수(≈1.0)를 요구하는가 — 느슨하면 부분 채움이 통과한다."""
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _REQUIRED_FILL_RATIO,
    )

    assert _REQUIRED_FILL_RATIO > 0.99, (
        f"임계가 {_REQUIRED_FILL_RATIO} — someValuesFrom 은 전수 요구다"
    )


def test_guard_is_inert_without_graph_context():
    """그래프 컨텍스트가 없으면 판정하지 않는다 (기존 호출자 호환).

    여기서 True 를 주면 컨텍스트를 안 넘기는 호출자가 모든 공리를 잃는다.
    """
    assert _check_fk_null_ratio("anyOp", "AnyClass") is False


# ── 시각 컬럼: 동시성은 참조가 아니다 ───────────────────────────────────


@pytest.mark.parametrize(
    "col", ["Timestamp", "timestamp", "Order_Date", "Departure_Time",
            "Measurement_DateTime", "TEST_DATETIME"],
)
def test_time_columns_are_recognised(col):
    assert _is_time_column(col) is True


@pytest.mark.parametrize("col", ["Tag_ID", "Product_ID", "Value", "Status", "Unit"])
def test_non_time_columns_are_not_recognised(col):
    """식별자·측정값 컬럼을 시각으로 오판하면 정당한 근거를 잃는다."""
    assert _is_time_column(col) is False


def test_timestamp_overlap_is_not_treated_as_evidence(deployed_tbox):
    """같은 시각을 기록한 두 테이블을 참조 관계로 보지 않는다.

    실측: Real_Time_Data.Timestamp ∩ Process_Steelmaking_Furnace.Timestamp 로
    realTimeDataMonitorsSteelmaking 이 "근거 있음/커버리지 100%" 로 판정됐다.
    """
    _, _, reason = _fk_evidence_for_op(
        deployed_tbox, NS["realTimeDataMonitorsSteelmaking"],
        NS["RealTimeData"], NS["ProcessSteelmakingFurnace"], str(DOMAIN_NS),
    )
    assert "Timestamp" not in reason, f"시각 컬럼이 근거로 채택됐다: {reason}"


# ── 판정 불가: 근거 부재를 단정하지 않는다 ──────────────────────────────


def test_composite_pk_range_is_not_judged(deployed_tbox):
    """range 가 composite PK 테이블이면 판정하지 않고 통과시킨다.

    단일 distinct 컬럼이 없어 참조 대상을 특정할 수 없다 (실측: Process_* 는
    Product_ID 30/4320). 근거 부재를 단정하면 오탐이 되고, 남는 것은 25번째
    check(check_existential_participation)가 사후에 보고한다 — 생성 가드와 사후
    게이트 두 벌이 필요한 이유다.
    """
    _, _, reason = _fk_evidence_for_op(
        deployed_tbox, NS["realTimeDataMonitorsSteelmaking"],
        NS["RealTimeData"], NS["ProcessSteelmakingFurnace"], str(DOMAIN_NS),
    )
    assert reason == "no_pk_candidate_in_range"


def test_unmapped_table_falls_back_to_filename(deployed_tbox):
    """table_class_mapping 에 없는 클래스도 파일명으로 CSV 를 찾는다.

    실측: 40 CSV 중 Soil_Monitoring 하나가 매핑에 없어 판정이 "CSV 없음" 으로
    빠져나갔다 — 그러면 근거 검사가 무조건 통과해 게이트가 조용해진다.
    """
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _csv_rows_for_class,
    )

    result = _csv_rows_for_class("SoilMonitoring")
    assert result is not None, "폴백이 동작하지 않는다"
    header, rows = result
    assert "Sample_ID" in header
    assert len(rows) > 100


# ── 전체 효과 (배포 T-Box 재생성) ───────────────────────────────────────


def test_regeneration_blocks_most_baseless_axioms(deployed_tbox):
    """가드가 실물에서 실제로 차단하는가 — 합성 픽스처로는 알 수 없다.

    이 리포는 게이트가 실물에서 0건이 되는 사고를 반복 겪었다 (값 범위 7규칙 전부
    0건 등). 재생성 결과를 직접 센다.
    """
    graph = Graph()
    graph += deployed_tbox
    # 기존 someValuesFrom restriction 제거 후 재생성 → 새 가드 효과만 측정
    for cls, node in list(graph.subject_objects(RDFS.subClassOf)):
        if (node, RDF.type, OWL.Restriction) in graph and graph.value(
            node, OWL.someValuesFrom,
        ):
            graph.remove((cls, RDFS.subClassOf, node))
            for p, o in list(graph.predicate_objects(node)):
                graph.remove((node, p, o))

    stats = _add_some_values_from_restrictions(graph, str(DOMAIN_NS))

    assert stats["restrictions_skipped_no_basis"] >= 20, (
        f"차단이 {stats['restrictions_skipped_no_basis']}건뿐이다 — 가드가 조용하다"
    )
    assert stats["restrictions_added"] >= 20, (
        f"공리가 {stats['restrictions_added']}개만 생성됐다 — 과잉 차단이다"
    )
    # 정확한 수를 고정한다. 범위만 주면 판정 완화(예: 값 교집합 임계 제거)가
    # 1~2건씩 통과시켜도 잡히지 않는다 — 실측으로 mutation 이 45→46 을 만들었다.
    # 배포 산출물이 바뀌어 이 수가 달라지면 **왜 달라졌는지 확인한 뒤** 갱신하라.
    #
    # 2026-08-31: 45 → 36 으로 갱신. 역축(``_domain_pk_coverage_in_range``) 도입으로
    # composite PK range 를 판정할 수 있게 되어 8건이 추가 차단됐다. 그 8건 전수 확인
    # (커버리지 = domain PK 가 range 테이블에 나타나는 비율):
    #
    #   14%  EquipmentMaster.equipmentUsedInSteelmaking      → S9 위반 5쌍에 포함
    #   14%  EquipmentMaster.equipmentUsedInRolling
    #   14%  EquipmentMaster.equipmentUsedInContinuousCasting
    #   16%  EquipmentMaster.equipmentUsedInBlastFurnace
    #   28%  EquipmentMaster.isElectricalEquipmentOf
    #   30%  ProductMaster.isProductOfBlastFurnace           ← S9 미보고, 같은 결함
    #   60%  ProductMaster.isProductOfSteelmaking
    #   80%  ProductMaster.isProductOfContinuousCasting
    #
    # 전부 someValuesFrom("모든 개체가 이 관계를 갖는다")과 모순이므로 정당한 차단이다.
    # 나머지 1건 차이는 T-Box 재생성(S2)으로 OP 집합이 바뀐 몫이다.
    # 2026-09-05: 36 → 35 로 갱신. 원인을 실측으로 분리했다 (tacit 축 ON/OFF 로
    # 재생성 집합을 비교):
    #
    #   tacit 축이 추가로 차단한 것 = [('ManufacturingProcessStep', 'followedBy')]
    #   tacit 축 때문에 새로 생긴 것 = [] (과잉 생성 없음)
    #
    # ``step_13`` 이 CSV 로 판정 불가일 때 tacit 실측 커버리지를 2차 근거로 쓰게 되면서
    # (6f60345) 공정 체인의 존재 공리가 차단됐다 — tacit 이 4개 공정 노드 중 3개에만
    # ``followedBy`` 를 걸기 때문이다 (3/4 = 0.75). 압연에는 후행이 없으므로
    # ``ManufacturingProcessStep ⊑ ∃followedBy`` 는 정의상 거짓이고, 그것을 만들었을 때
    # 추론에서 restriction 위반 34,562건이 났다. 즉 이 1건 감소는 정당한 차단이다.
    #
    # 같은 세션에서 이 테스트가 **진짜 회귀도 잡았다**: ``_already_restricted`` 가
    # ``onProperty`` 일치만 보게 고쳐졌던 탓에 같은 프로퍼티의 **maxCardinality**
    # restriction 까지 억제 근거로 세어 생성이 13건으로 떨어졌다 (정당한 존재 공리
    # 22건 손실). 가드가 ``someValuesFrom`` 까지 요구하도록 정정했다.
    # 2026-09-05 (2): 35 → 37 로 갱신. 원인은 **가드가 아니라 T-Box 세대**다. 판별
    # 절차 — 같은 재생성을 세 산출물에 돌려 비교했다:
    #
    #   t_box_pre13de (13d/13e 도입 전)        added 37 / skipped 45 / total 82
    #   t_box         (13d/13e 도입 후)        added 37 / skipped 45 / total 82
    #   t_box_presession_20260905070824 (직전) added 35 / skipped 46 / total 81
    #
    # 즉 이번 S2 세대가 단일 domain/range OP 를 1쌍 더 만들어 후보가 81 → 82 가 됐고
    # 생성이 2건 늘었다. 13e(사후 감사)는 **생성 후** 도는 별 스텝이라 이 수에 개입하지
    # 않는다 (그래서 pre/post 가 같다). 이 수가 또 달라지면 같은 절차로 원인을 가르고,
    # 가드 탓이 아님을 확인한 뒤 갱신하라.
    total = stats["restrictions_added"] + stats["restrictions_skipped_no_basis"]
    assert stats["restrictions_added"] == 37, (
        f"생성 {stats['restrictions_added']} (전체 {total}) — 37 에서 바뀌었다. "
        "가드 판정이 느슨해졌는지, T-Box/CSV 가 바뀐 것인지 확인하라 "
        "(절차: 이전 세대 T-Box 백업에 같은 재생성을 돌려 비교)."
    )

    # 세대-독립 축 — 가드가 실제로 하중을 받는가. 위 고정값은 T-Box 세대가 바뀌면
    # 갱신해야 하지만, 이 주장은 후보 집합이 어떻게 바뀌어도 유효하다: 가드를 끄면
    # 생성이 확실히 늘어야 한다. 늘지 않으면 가드가 아무것도 막지 않는 것이다.
    loose_graph = Graph()
    loose_graph += graph          # 재생성 직후 상태에서 someValuesFrom 을 다시 비운다
    for cls, node in list(loose_graph.subject_objects(RDFS.subClassOf)):
        if (node, RDF.type, OWL.Restriction) in loose_graph and loose_graph.value(
            node, OWL.someValuesFrom,
        ):
            loose_graph.remove((cls, RDFS.subClassOf, node))
            for p, o in list(loose_graph.predicate_objects(node)):
                loose_graph.remove((node, p, o))
    import tools.quality_steps.step_13_pk_functional_someValuesFrom as _s13

    original = _s13._check_fk_null_ratio
    try:
        _s13._check_fk_null_ratio = lambda *a, **k: False   # 가드 무력화
        loose = _add_some_values_from_restrictions(loose_graph, str(DOMAIN_NS))
    finally:
        _s13._check_fk_null_ratio = original
    assert loose["restrictions_added"] - stats["restrictions_added"] >= 20, (
        f"가드를 껐을 때 생성이 {stats['restrictions_added']} → "
        f"{loose['restrictions_added']} 로 {loose['restrictions_added'] - stats['restrictions_added']}건만 "
        "늘었다 — 가드가 하중을 받지 않는다 (판정이 느슨해졌거나 표적이 사라졌다)"
    )


def test_skip_counter_is_reported():
    """차단 건수를 stats 로 노출하는가 — 조용한 차단은 감사할 수 없다."""
    graph = Graph()
    bind_namespaces(graph)
    graph.add((NS["A"], RDF.type, OWL.Class))
    graph.add((NS["B"], RDF.type, OWL.Class))
    graph.add((NS["p"], RDF.type, OWL.ObjectProperty))
    graph.add((NS["p"], RDFS.domain, NS["A"]))
    graph.add((NS["p"], RDFS.range, NS["B"]))

    stats = _add_some_values_from_restrictions(graph, str(DOMAIN_NS))

    assert "restrictions_skipped_no_basis" in stats
    assert "restrictions_added" in stats


def test_step_docstring_documents_the_basis_check():
    """모듈 docstring 이 "FK NULL 비율" 이 아니라 실제 판정을 설명하는가.

    낡은 설명이 남으면 다음 사람이 이름 추측 방식이 여전히 쓰인다고 오해한다.
    """
    src = pathlib.Path(
        "tools/quality_steps/step_13_pk_functional_someValuesFrom.py",
    ).read_text(encoding="utf-8")
    assert "값 교집합" in src or "value" in src.lower()
    assert "_fk_evidence_for_op" in src


# ── 역축: range 가 composite PK 여도 판정한다 (2026-08-31) ────────────────
#
# 위 축(range PK 값 교집합)은 composite PK 테이블에서 실패한다 — 단일 distinct 컬럼이
# 없으므로 `target_values` 가 비고, 예전에는 그때 **판정을 포기하고 통과**시켰다
# (`no_pk_candidate_in_range`). 그 결과 거짓 공리 5개가 배포됐다.
#
# 실측 (S2 재실행 후 S9): 필수참여 위반 5쌍 / 개체 207건. 전부 EquipmentMaster 이고
# range 가 Process_* / Electrical_Consumption — 정확히 composite PK 테이블들이다.
#
# **반대 방향은 판정 가능하다.** domain PK 가 range 테이블에서 몇 % 나타나는가:
#
#   Equipment_Master.Equipment_ID (unique PK 50/50)
#     Process_Steelmaking_Furnace   7/50 =  14%   → 공리 거짓 (86% 가 위반)
#     Process_Rolling               7/50 =  14%
#     Process_Continuous_Casting    7/50 =  14%
#     Process_Blast_Furnace         8/50 =  16%
#     Electrical_Consumption       14/50 =  28%
#     Equipment_Status             50/50 = 100%   → 공리 **참** (보존해야 한다)
#
# someValuesFrom 은 "모든 개체가 이 관계를 갖는다" 는 주장이므로 14% 는 거짓이다.


@pytest.mark.parametrize("op,domain,rng", [
    ("equipmentUsedInSteelmaking", "EquipmentMaster", "ProcessSteelmakingFurnace"),
    ("equipmentUsedInRolling", "EquipmentMaster", "ProcessRolling"),
    ("equipmentUsedInContinuousCasting", "EquipmentMaster", "ProcessContinuousCasting"),
    ("equipmentUsedInBlastFurnace", "EquipmentMaster", "ProcessBlastFurnace"),
    ("isElectricalEquipmentOf", "EquipmentMaster", "ElectricalConsumption"),
])
def test_partial_domain_coverage_is_skipped(op, domain, rng, deployed_tbox):
    """THE REGRESSION: domain PK 가 range 에서 일부만 나타나면 공리를 만들지 않는다.

    예전에는 range PK 특정 실패 시 통과시켜 이 5개가 배포됐다 (S9 위반 5쌍 / 207건).
    """
    assert _skip(op, domain, rng, deployed_tbox), (
        f"{domain}.{op} → {rng}: domain PK 가 range 에서 일부만 나타나는데 "
        f"공리를 허용했다 — 추론이 위반 개체를 그 클래스로 타이핑한다"
    )


def test_full_domain_coverage_is_still_allowed(deployed_tbox):
    """PRESERVATION: 100% 커버리지면 필수참여가 **참** 이므로 보존한다.

    역축이 무조건 차단하면 정당한 공리를 잃는다 — 게이트를 끄는 것과 같다.
    Equipment_Status 는 설비 50대 전부가 상태 기록을 가지므로 이 공리는 옳다.
    """
    assert not _skip(
        "hasEquipmentStatus", "EquipmentMaster", "EquipmentStatus", deployed_tbox,
    ), "커버리지 100% 인 참인 공리를 차단했다"


def test_reverse_axis_reason_names_both_columns(deployed_tbox):
    """차단 사유가 어느 컬럼을 어떻게 쟀는지 밝히는가 — 사람이 검증할 수 있어야 한다."""
    _, ratio, reason = _fk_evidence_for_op(
        deployed_tbox, NS["equipmentUsedInSteelmaking"],
        NS["EquipmentMaster"], NS["ProcessSteelmakingFurnace"], str(DOMAIN_NS),
    )
    assert "reverse_axis" in reason, reason
    assert "Equipment_ID" in reason, reason
    assert ratio < 0.5, f"커버리지가 {ratio:.1%} 로 측정됐다 — 실측은 14% 다"


def test_reverse_axis_is_inert_without_a_domain_pk():
    """domain PK 를 못 찾으면 판정하지 않는다 (기존 동작 유지).

    근거 없이 막으면 정당한 공리를 대량으로 잃는다 — 이 게이트는 "확실히 근거 없는
    것만" 막는다.
    """
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _domain_pk_coverage_in_range,
    )

    # domain 에 unique 컬럼이 없다 (PK 후보 부재)
    has, ratio, reason = _domain_pk_coverage_in_range(
        ["CODE"], [["A"], ["A"], ["B"]],        # 중복 → PK 아님
        ["CODE"], [["A"], ["B"]],
        "Dom", "Rng",
    )
    assert has is True and ratio == 1.0
    assert reason == "no_pk_candidate_in_range"


def test_reverse_axis_ignores_time_columns():
    """시각 컬럼은 PK 후보가 아니다 — 동시성은 참조가 아니다 (위 축과 같은 이유)."""
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _domain_pk_coverage_in_range,
    )

    # Timestamp 가 유일한 unique 컬럼이면 판정 불가로 남아야 한다
    has, _, reason = _domain_pk_coverage_in_range(
        ["Timestamp"], [["t1"], ["t2"], ["t3"]],
        ["Timestamp"], [["t1"], ["t2"], ["t3"]],
        "Dom", "Rng",
    )
    assert has is True
    assert reason == "no_pk_candidate_in_range", (
        "시각 일치를 참조 근거로 읽었다 — 두 테이블이 같은 시각을 기록한 것뿐이다"
    )


def test_deployed_sweep_finds_no_remaining_partial_coverage(deployed_tbox):
    """실측 고정: 배포 T-Box 에 부분 커버리지 ∃공리가 **남아 있지 않다**.

    "공리가 줄었다" 만 주장하면 전부 차단해도 통과한다. 그래서 두 축을 함께 본다:

      · 남은 공리 중 역축으로 차단될 것이 **0개** (정리가 끝났다)
      · 그런데도 공리 자체는 **여러 개 남아 있다** (과잉차단이 아니다)

    2026-08-31: 역축 도입 시점에는 차단 집합이 S9 위반 5쌍과 정확히 일치했고
    (equipmentUsedIn* 4 + isElectricalEquipmentOf), 그 5개를 T-Box 에서 제거해
    S9 필수참여 체크가 5쌍/207건 → 0쌍/0건 으로 통과했다. 그 뒤로는 "잔여 0" 이
    올바른 고정값이다 — 목록을 박아두면 정리된 상태에서 영구 실패한다.

    2026-09-05: 판정을 ``effective_existential_coverage`` (CSV ∪ tacit) 로 바꿨다.
    ``_fk_evidence_for_op`` 단독은 domain 에 CSV 가 있으면 tacit 을 보지 않아
    ``AirEmissionMonitoring ⊑ ∃hasStackEquipment`` 를 0.0 으로 낸다 — 그런데 tacit 이
    1,560/1,560 을 채우므로 S9 의 ``existential_participation`` 은 위반 0 이다.
    CSV 축만 기준으로 삼으면 이 테스트가 **산 공리의 삭제를 요구**한다.
    """
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _REQUIRED_FILL_RATIO,
    )
    from tools.quality_steps.step_13e_existential_axiom_audit import (
        effective_existential_coverage,
    )

    blocked = set()
    examined = 0
    for restr in deployed_tbox.subjects(OWL.someValuesFrom, None):
        prop = deployed_tbox.value(restr, OWL.onProperty)
        rng = deployed_tbox.value(restr, OWL.someValuesFrom)
        if not (isinstance(prop, URIRef) and str(prop).startswith(str(DOMAIN_NS))):
            continue
        for dom in deployed_tbox.subjects(RDFS.subClassOf, restr):
            if not (isinstance(dom, URIRef)
                    and str(dom).startswith(str(DOMAIN_NS))):
                continue
            examined += 1
            effective, _evidence = effective_existential_coverage(
                deployed_tbox, prop, dom, rng, str(DOMAIN_NS))
            if effective < _REQUIRED_FILL_RATIO:
                blocked.add(str(prop).split("#")[-1])

    if examined == 0:
        pytest.skip("배포 T-Box 에 someValuesFrom 공리가 없다")
    # 축 1 — 잔여 부분 커버리지 공리가 없다 (정리 완료).
    assert not blocked, (
        f"부분 커버리지 ∃공리가 {len(blocked)}개 남아 있다: {sorted(blocked)} — "
        f"추론이 위반 개체를 그 클래스로 타이핑해 질의가 조용히 틀린다"
    )
    # 축 2 — 그런데도 공리는 남아 있다 (역축이 전부 지운 것이 아니다).
    assert examined >= 20, (
        f"검사 대상이 {examined}개뿐이다 — 과잉차단으로 ∃공리가 사라졌는지 확인하라"
    )
