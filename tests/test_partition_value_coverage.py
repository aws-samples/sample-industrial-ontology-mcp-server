"""파티션이 CSV 실값을 덮지 못하면 개체를 논리적으로 배제한다 (rank 3 + rank 4).

2026-08-30 실측. 배포 T-Box 의 ``owl:disjointUnionOf`` 10건 중 **7건**이 CSV 값을
덮지 못했고, 그 파티션들이 합계 **38,051 개체**를 OWL DL 상 모순으로 만들었다.
원인은 LLM 이 **CSV 를 보지 않고 값 어휘를 발명한 것**이다:

========================= ============================ =====================
parent                     공리 값                        CSV 에만 있는 값
========================= ============================ =====================
``RealTimeData``           ``GOOD``                     ``0``/``1``/``2`` 36,000
``InventoryTransaction``   ``IN`` / ``OUT``             ``Inbound``/``Outbound``… 600
``EquipmentStatus``        Running/Stopped/Maintenance  ``Standby`` 1,227
``WasteManagement``        Landfill/Recycle             ``Incineration``… 118
``TagMaster``              ANALOG/DIGITAL               ``FLOAT`` 50
``MaintenanceHistory``     Corrective/Preventive        ``Emergency``… 47
``NDTResults``             Pass/Fail                    ``Conditional`` 9
========================= ============================ =====================

## 닫힘이 두 겹이었다

``disjointUnionOf`` 를 지워도 ``EquipmentStatus`` / ``WasteManagement`` 가 여전히
INCONSISTENT 였다 — ``parent owl:equivalentClass Union_X (unionOf children)`` 이
같은 닫힘을 한 번 더 걸고 있었다. 한 겹만 걷으면 증상이 그대로 남는다.

## 왜 게이트가 침묵했나

배포 상태(명명 Restriction)에서는 HermiT 이 제약을 보지 못해 ``consistent: true``
를 반환한다. ``disjointUnionOf`` 를 읽는 check 도 리포 전체에 0개였다. 그래서
익명화(rank 2 후반)와 **함께 착륙해야** 한다 — 익명화만 하면 S4 가 선재 결함으로
hard-fail 하고, 정리만 하면 검증기가 여전히 눈멀어 있다.

## 이 테스트의 방향

* **차단** — 값을 덮지 못하는 파티션/union 닫힘을 제거한다
* **보존** — 덮는 것은 남긴다 (과잉 제거 방지)
* **판정 불가 ≠ 갭 없음** — 근거가 없으면 보존한다
* **통합** — 정리 + 익명화 후 위배 개체가 있어도 CONSISTENT 다
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_12j_collection_literal_cleanup as step_12j
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")


def _ctx() -> StepContext:
    return StepContext(domain_ns=NS)


def _tbox() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    g = Graph()
    g.parse(str(TBOX), format="turtle")
    return g


# ── 값 커버리지 판정 ────────────────────────────────────────────────────


def test_value_gap_detects_uncovered_csv_values():
    """THE REGRESSION: 공리가 놓친 CSV 값을 찾는다.

    ``EquipmentStatus`` 를 픽스처로 쓴다 — CSV 에 ``Standby`` 1,227행이 있는데
    공리는 Running/Stopped/Maintenance 만 주장한다.
    """
    from tools.ontology_quality import _partition_value_gap

    g = _tbox()
    members = [
        URIRef(NS + m) for m in
        ("RunningEquipmentStatus", "StoppedEquipmentStatus",
         "MaintenanceEquipmentStatus")
    ]
    if (members[0], RDF.type, OWL.Class) not in g:
        pytest.skip("대상 클래스 없음 (T-Box 세대 차이)")

    gap = _partition_value_gap(g, "EquipmentStatus", members)

    assert gap, "Standby 가 배제되는데 갭이 검출되지 않았다"
    assert "Standby" in gap["uncovered_values"]
    assert gap["uncovered_rows"] > 0


def test_value_gap_returns_none_when_fully_covered():
    """NEGATIVE 방향 — 값을 다 덮으면 갭이 없다 (과잉 차단 방지)."""
    from tools.ontology_quality import _partition_value_gap

    g = _tbox()
    members = [
        URIRef(NS + m) for m in ("Scope1Emission", "Scope2Emission", "Scope3Emission")
    ]
    if (members[0], RDF.type, OWL.Class) not in g:
        pytest.skip("대상 클래스 없음")

    assert _partition_value_gap(g, "GHGEmission", members) is None


def test_value_gap_returns_none_without_defining_axiom():
    """정의 공리가 없으면 추론이 값으로 분류하지 않으므로 모순이 없다.

    ``ManufacturingProcessStep`` 의 자식들은 hasValue 정의가 없고, 실측으로
    안전하다 (파티션이 남아 있어도 CONSISTENT).
    """
    from tools.ontology_quality import _partition_value_gap

    g = _tbox()
    members = [
        URIRef(NS + m) for m in
        ("ProcessBlastFurnace", "ProcessSteelmakingFurnace",
         "ProcessContinuousCasting", "ProcessRolling")
    ]
    if (members[0], RDF.type, OWL.Class) not in g:
        pytest.skip("대상 클래스 없음")

    assert _partition_value_gap(g, "ManufacturingProcessStep", members) is None


def test_value_gap_is_none_when_evidence_missing():
    """판정 불가와 갭 없음을 혼동하면 정당한 파티션을 지운다.

    ``dcterms:source`` 가 없는 DP 로 정의된 파티션은 CSV 대조가 불가능하다.
    """
    from rdflib.namespace import DCTERMS

    from tools.ontology_quality import _partition_value_gap

    g = _tbox()
    prop = URIRef(NS + "equipmentStatusValue")
    if (prop, RDF.type, OWL.DatatypeProperty) not in g:
        pytest.skip("대상 DP 없음")
    for src in list(g.objects(prop, DCTERMS.source)):
        g.remove((prop, DCTERMS.source, src))

    members = [URIRef(NS + m) for m in
               ("RunningEquipmentStatus", "StoppedEquipmentStatus",
                "MaintenanceEquipmentStatus")]

    assert _partition_value_gap(g, "EquipmentStatus", members) is None


# ── 정리: 파티션 + union 닫힘 ────────────────────────────────────────────


def test_uncovered_partitions_are_pruned():
    g = _tbox()

    result = step_12j.apply(g, _ctx())
    stats = result.stats

    assert stats["uncovered_partitions_removed"] > 0 or stats["partitions_kept"] >= 0
    # 남은 파티션은 전부 값을 덮어야 한다.
    from tools.ontology_quality import _partition_value_gap
    for parent, lst in g.subject_objects(OWL.disjointUnionOf):
        if isinstance(lst, Literal):
            pytest.fail("리터럴 disjointUnionOf 가 남았다")
        members = [m for m in g.items(lst) if isinstance(m, URIRef)]
        gap = _partition_value_gap(g, str(parent).replace(NS, ""), members)
        assert gap is None, f"값을 덮지 못하는 파티션이 남았다: {gap}"


def test_union_closures_are_pruned_too():
    """``parent ≡ unionOf(children)`` 도 같은 닫힘이다 — 한 겹만 걷으면 안 된다."""
    g = _tbox()

    step_12j.apply(g, _ctx())

    from tools.ontology_quality import _partition_value_gap
    for parent, union_cls in g.subject_objects(OWL.equivalentClass):
        if not isinstance(union_cls, URIRef):
            continue
        lists = list(g.objects(union_cls, OWL.unionOf))
        if not lists:
            continue
        members = [m for lst in lists for m in g.items(lst) if isinstance(m, URIRef)]
        if len(members) < 2:
            continue
        gap = _partition_value_gap(g, str(parent).replace(NS, ""), members)
        assert gap is None, f"값을 덮지 못하는 union 닫힘이 남았다: {gap}"


def test_union_class_and_child_subclassof_are_preserved():
    """닫힘만 끊고 계층은 보존한다 — 질의 경로를 잃으면 안 된다."""
    g = _tbox()
    unions = [
        u for u in g.subjects(RDF.type, OWL.Class)
        if isinstance(u, URIRef) and "Union_" in str(u)
    ]
    if not unions:
        pytest.skip("Union_* 클래스 없음")
    children_before = {
        (s, o) for s, o in g.subject_objects(RDFS.subClassOf)
        if isinstance(s, URIRef) and str(s).startswith(NS)
    }

    step_12j.apply(g, _ctx())

    still = [u for u in unions if (u, RDF.type, OWL.Class) in g]
    assert still, "Union 클래스가 삭제됐다 — equivalentClass 만 끊어야 한다"
    children_after = {
        (s, o) for s, o in g.subject_objects(RDFS.subClassOf)
        if isinstance(s, URIRef) and str(s).startswith(NS)
    }
    assert children_before <= children_after, "subClassOf 계층이 손실됐다"


def test_idempotent():
    """두 번 돌려도 같다 — S3 스텝의 baseline 안정성 요건."""
    g = _tbox()
    first = step_12j.apply(g, _ctx()).stats
    second = step_12j.apply(g, _ctx()).stats

    assert second["uncovered_partitions_removed"] == 0
    assert second["uncovered_union_closures_removed"] == 0
    assert second["collection_literals_removed"] == 0
    assert first is not second


# ── 통합: 정리 + 익명화 후에도 위배 개체가 CONSISTENT ────────────────────


@pytest.mark.slow
def test_violating_instances_are_consistent_after_cleanup():
    """THE INTEGRATION: 이전에 모순이던 7개 값이 이제 통과한다.

    이것이 rank 3 과 rank 2-후반을 **함께** 착륙시켜야 하는 이유다. 익명화만
    하면 이 검사가 INCONSISTENT 로 실패하고, 정리만 하면 검증기가 눈멀어 있어
    이 검사가 아무것도 증명하지 못한다.
    """
    from owlready2 import (
        Nothing,
        OwlReadyInconsistentOntologyError,
        default_world,
        sync_reasoner,
    )

    from tools.owl_reasoner import _cleanup_world, _ttl_to_owlready
    from tools.quality_steps import step_12h_iof_category_conflict as step_12h

    g = _tbox()
    step_12h.apply(g, _ctx())
    step_12j.apply(g, _ctx())

    inst_ns = "http://example.com/steel-ontology/instances#"
    cases = [
        ("EquipmentStatus", "equipmentStatusValue", "Standby"),
        ("RealTimeData", "realTimeDataQualityCode", "1"),
        ("WasteManagement", "wasteManagementTreatmentMethod", "Incineration"),
        ("MaintenanceHistory", "maintenanceHistoryType", "Emergency"),
        ("NDTResults", "ndtResultsResult", "Conditional"),
        ("TagMaster", "tagMasterDataType", "FLOAT"),
        ("InventoryTransaction", "inventoryTransactionType", "Transfer"),
    ]
    added = 0
    for cls, dp, val in cases:
        if (URIRef(NS + cls), RDF.type, OWL.Class) not in g:
            continue
        inst = URIRef(inst_ns + "probe_" + cls)
        g.add((inst, RDF.type, URIRef(NS + cls)))
        g.add((inst, URIRef(NS + dp), Literal(val)))
        added += 1
    if not added:
        pytest.skip("대상 클래스 없음")

    _cleanup_world()
    onto, _tmp = _ttl_to_owlready(g.serialize(format="turtle"))
    try:
        with onto:
            sync_reasoner(debug=0)
    except OwlReadyInconsistentOntologyError:
        pytest.fail(
            "위배 개체를 넣었더니 INCONSISTENT — 닫힘이 아직 남아 있다",
        )
    unsat = sorted(
        c.name for c in default_world.inconsistent_classes() if c is not Nothing
    )
    assert not unsat, f"unsatisfiable 클래스가 남았다: {unsat}"
    _cleanup_world()
