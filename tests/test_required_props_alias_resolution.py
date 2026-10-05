"""``required_props_audit`` 의 alias 해석기가 실제 DP 이름에 도달하는가.

2026-08-30 실측. ``abox_loss_manifest.json::required_props_audit.violations`` 14건이
**전부 오탐**이었다. 해석기가 클래스명 앞 **1·2 토큰** prefix 만 시도해서 토큰이
3개 이상인 클래스는 실제 DP 이름에 영구히 도달할 수 없었다::

    AirEmissionMonitoring/hasTimestamp
      시도  airTimestamp, airEmissionTimestamp
      실제  airEmissionMonitoringTimestamp    (1,560건 존재)
    ProcessBlastFurnace/hasTimestamp
      시도  processTimestamp, processBlastTimestamp
      실제  blastFurnaceTimestamp             (4,320건 존재)
    NDTResults/hasResult
      시도  nResult, nDResult   ← 약어가 N/D/T 로 쪼개진다
      실제  ndtResultsResult                  (100건 존재)
    Transportation/hasOrigin
      축이 OP 다 (transportationHasOriginWarehouse, 300건)

## 왜 위험한가

이것이 Path B class-specific DP 커버리지를 재는 **유일한 게이트**다. 위반 14건이
상수 노이즈로 깔려 있으면 진짜 위반이 생겨도 구분되지 않는다 — 즉 이 축은
측정되지 않는 것과 같았다.

## 이 테스트의 방향

"위반 0건" 만 주장하면 게이트를 껐을 때도 통과한다. 네 축을 고정한다:

* **해석** — 3토큰 이상 / 약어 / 접미 prefix 형태의 실제 DP 를 찾는다
* **OP 축** — FK 가 OP 로 모델링된 경우를 위반으로 보지 않는다
* **발화** — 값이 실제로 없으면 잡는다 (mutation)
* **비과잉** — 다른 클래스의 동일 접미 DP 를 인정하지 않는다
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import RDF, Graph, Literal, URIRef

from tools.abox_generation import (
    DOMAIN_NS_OBJ,
    _check_required_props_coverage,
    _parse_tbox,
    _re_sub_camel_to_upper_snake,
)

TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")


@pytest.fixture(scope="module")
def tbox_info() -> dict:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    return _parse_tbox(TBOX.read_text(encoding="utf-8"))


def _graph_with(class_dps: dict[str, list[str]], count: int = 1) -> tuple[Graph, dict]:
    """각 클래스의 인스턴스 ``count`` 개에 주어진 DP 를 채운 그래프."""
    graph = Graph()
    per_class: dict[str, int] = {}
    for class_name, dps in class_dps.items():
        for index in range(count):
            inst = URIRef(f"http://ex.org/{class_name}_{index}")
            graph.add((inst, RDF.type, DOMAIN_NS_OBJ[class_name]))
            for dp in dps:
                graph.add((inst, DOMAIN_NS_OBJ[dp], Literal("x")))
        per_class[class_name] = count
    return graph, per_class


# ── 해석: 실제 DP 이름에 도달한다 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("class_name", "real_dps"),
    [
        # 3토큰 이상 — 예전 구현이 도달 불가였던 형태
        ("AirEmissionMonitoring",
         ["airEmissionMonitoringTimestamp", "airEmissionMonitoringUnit"]),
        # 접미 prefix — 클래스명 앞부분을 버린 형태
        ("ProcessBlastFurnace", ["blastFurnaceTimestamp"]),
        ("ProcessContinuousCasting", ["continuousCastingTimestamp"]),
        ("ProcessSteelmakingFurnace", ["steelmakingTimestamp"]),
        # 약어 클래스명 — [A-Z][^A-Z]* 가 글자마다 쪼개던 형태
        ("NDTResults", ["ndtResultsResult", "ndtResultsSeverity"]),
        ("GHGEmission", ["ghgEmissionTimestamp", "ghgEmissionSourceType"]),
        # 의미 접미가 canonical 과 다른 형태 (hasStatus → *Value)
        ("EquipmentStatus", ["equipmentStatusTimestamp", "equipmentStatusValue"]),
        ("RealTimeData", ["realTimeDataTimestamp", "realTimeDataValue"]),
    ],
)
def test_real_dp_names_satisfy_requirement(tbox_info, class_name, real_dps):
    """THE REGRESSION: 실제 DP 가 채워져 있으면 위반이 아니다."""
    graph, per_class = _graph_with({class_name: real_dps})

    result = _check_required_props_coverage(graph, per_class, tbox_info)

    assert result["violations"] == [], (
        f"{class_name}: 실제 DP {real_dps} 가 있는데 위반으로 보고됐다 — "
        f"{[(v['dp'], v['checked_dps']) for v in result['violations']]}"
    )


def test_resolver_finds_more_than_the_canonical_name(tbox_info):
    """후보가 canonical 1개뿐이면 해석 실패다 — 그것이 오탐의 지문이었다."""
    graph, per_class = _graph_with({"AirEmissionMonitoring": []})

    result = _check_required_props_coverage(graph, per_class, tbox_info)

    assert result["violations"], "픽스처가 DP 를 비웠으므로 위반이 나와야 한다"
    for violation in result["violations"]:
        assert len(violation["checked_dps"]) > 1, (
            f"{violation['dp']}: 후보가 {violation['checked_dps']} 뿐 — "
            "이름 해석이 실패했다"
        )


# ── OP 축: FK 를 관계로 모델링한 것은 위반이 아니다 ─────────────────────


def test_op_modelled_requirement_is_satisfied(tbox_info):
    """``Transportation`` 의 출발/도착은 OP 다 — DP 축만 보면 오탐이 된다.

    2026-09-05: OP 이름을 ``dcterms:source`` 로 해상한다. 예전에는
    ``transportationHasOriginWarehouse`` 를 박아 뒀는데, 그 OP 는 낡은
    ``tbox_manual_additions.ttl`` 블록이 만든 0행 유령이었고 실제 데이터는
    ``hasOriginWarehouse`` 가 담고 있었다. 블록을 제거하니 픽스처가 존재하지 않는
    술어로 인스턴스를 만들어 "OP 모델링을 위반으로 보고" 하는 상태가 됐다 —
    **테스트 픽스처가 낡은 것**이지 판정기 결함이 아니다.
    """
    from tests.helpers_tbox_names import op_for_column

    origin_op = op_for_column(
        "Origin_Warehouse", "Transportation", "WarehouseMaster")
    dest_op = op_for_column(
        "Destination_Warehouse", "Transportation", "WarehouseMaster")

    graph = Graph()
    inst = URIRef("http://ex.org/t0")
    graph.add((inst, RDF.type, DOMAIN_NS_OBJ["Transportation"]))
    graph.add((inst, DOMAIN_NS_OBJ[origin_op], URIRef("http://ex.org/w1")))
    graph.add((inst, DOMAIN_NS_OBJ[dest_op], URIRef("http://ex.org/w2")))
    graph.add((inst, DOMAIN_NS_OBJ["transportationDepartureTime"],
               Literal("2025-01-01")))

    result = _check_required_props_coverage(graph, {"Transportation": 1}, tbox_info)

    assert result["violations"] == [], (
        f"OP 로 모델링된 요구사항이 위반으로 보고됐다: "
        f"{[(v['dp'], v['checked_ops']) for v in result['violations']]}"
    )


# ── 발화: 실제로 없으면 잡는다 (mutation, NEGATIVE 방향) ────────────────


def test_missing_values_are_still_detected(tbox_info):
    """해석기를 넓혔어도 진짜 누락은 잡는가 — 게이트가 살아있음의 증거."""
    graph = Graph()
    for index in range(10):
        inst = URIRef(f"http://ex.org/rtd_{index}")
        graph.add((inst, RDF.type, DOMAIN_NS_OBJ["RealTimeData"]))
        graph.add((inst, DOMAIN_NS_OBJ["realTimeDataValue"], Literal("1")))
        if index >= 3:  # 3/10 에서 timestamp 누락
            graph.add((inst, DOMAIN_NS_OBJ["realTimeDataTimestamp"], Literal("t")))

    result = _check_required_props_coverage(graph, {"RealTimeData": 10}, tbox_info)

    hits = [v for v in result["violations"] if v["dp"] == "hasTimestamp"]
    assert hits, "3/10 누락인데 검출되지 않았다 — 게이트가 죽었다"
    assert hits[0]["coverage_pct"] == 70.0, hits[0]


def test_empty_class_reports_zero_coverage(tbox_info):
    """DP 를 하나도 안 채우면 0% 로 보고한다."""
    graph, per_class = _graph_with({"RealTimeData": []})

    result = _check_required_props_coverage(graph, per_class, tbox_info)

    pcts = {v["dp"]: v["coverage_pct"] for v in result["violations"]}
    assert pcts and all(p == 0.0 for p in pcts.values()), pcts


# ── 비과잉: 남의 DP 를 인정하지 않는다 ──────────────────────────────────


def test_foreign_class_dp_does_not_satisfy(tbox_info):
    """다른 클래스의 동일 접미 DP 는 요구를 만족시키지 않는다.

    과잉 매칭하면 위반이 0건이 되지만 그것은 게이트를 끈 것이다.
    ``energySourceMasterSourceType`` 는 ``GHGEmission.hasSourceType`` 을
    만족시켜서는 안 된다.
    """
    graph = Graph()
    inst = URIRef("http://ex.org/ghg0")
    graph.add((inst, RDF.type, DOMAIN_NS_OBJ["GHGEmission"]))
    graph.add((inst, DOMAIN_NS_OBJ["ghgEmissionTimestamp"], Literal("t")))
    graph.add((inst, DOMAIN_NS_OBJ["energySourceMasterSourceType"], Literal("X")))

    result = _check_required_props_coverage(graph, {"GHGEmission": 1}, tbox_info)

    assert any(v["dp"] == "hasSourceType" for v in result["violations"]), (
        "남의 클래스 DP 가 요구를 만족시켰다 — 과잉 매칭"
    )


# ── 헬퍼 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sourceType", "SOURCE_TYPE"),
        ("source_type", "SOURCE_TYPE"),
        ("timestamp", "TIMESTAMP"),
        ("unitOfMeasure", "UNIT_OF_MEASURE"),
        ("", ""),
    ],
)
def test_camel_to_upper_snake(raw, expected):
    """``dcterms:source`` 색인 키 표기를 맞추지 못하면 역인덱스가 전부 빗나간다."""
    assert _re_sub_camel_to_upper_snake(raw) == expected
