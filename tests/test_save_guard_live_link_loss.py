"""손실률 게이트가 통과시킨 회귀를 **산 데이터 경로** 축이 잡는다.

2026-08-25 실측. S2 재실행이 ``op_links`` 18개를 잃었는데 ``worst_ratio`` 는
**14.8%** 로 ``_SAVE_DECL_LOSS_RATIO`` (0.2) 문턱을 통과했다. 그 18개 중 7개는
A-Box·tacit 이 실제로 채우는 관계였고, 결과로 S9 ``cw_master_orphan`` 이
``ItemSupplierMap`` 98/98 = 100% 고아로 FAIL 했다 (전체 고아율 4.32% → 17.99%).

## 임계를 낮추는 것은 답이 아니다 (실측으로 기각)

같은 방식으로 2026-08-19 **정상** 실행을 재계산하면 raw 기준 ``op_links`` 손실이
**18.8%** 로 이번(14.8%)보다 **높다**. 0.1 로 낮추면 그 정상 실행도 차단되고,
0.15 로 낮춰도 이번 건은 통과한다 — 비율은 두 경우를 구분하지 못한다.

같은 단계(S3 정규화)로 공정 비교했을 때 갈리는 것은 비율이 아니라 **산 경로 개수**:

    8/19 정상 실행   산 경로 손실 1건
    8/25 회귀        산 경로 손실 7건   (손실률은 14.0% vs 14.8% 로 거의 동일)

## 이 축이 발견한 것

6개를 복원한 뒤에도 1건이 남았다 — ``isStackEquipmentOf`` (정방향
``hasStackEquipment`` 가 A-Box 1,560 / tacit 11,160 트리플). 손으로 찾은 6개에
빠져 있던 7번째이고, 게이트가 사람의 누락을 잡았다.

## 왜 비율이 아니라 절대 개수인가

분모(전체 ``op_links``)가 S2 재생성마다 흔들려 같은 손실이 다른 비율로 보인다.
"산 데이터 경로는 한 개도 잃지 말라" 가 의도이므로 개수로 센다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.multi_agent_tbox import (
    _SAVE_DECL_LOSS_RATIO,
    _SAVE_LIVE_LINK_LOSS_MAX,
    _declaration_capabilities,
    _live_link_losses,
)

NS = Namespace(str(DOMAIN_NS))


def _tbox(links: list[tuple[str, str, str]], inverses: dict | None = None) -> Graph:
    """``(op_name, domain, range)`` 목록으로 최소 T-Box 를 만든다."""
    g = Graph()
    for name, dom, rng in links:
        op = NS[name]
        g.add((op, RDF.type, OWL.ObjectProperty))
        g.add((op, RDFS.domain, NS[dom]))
        g.add((op, RDFS.range, NS[rng]))
        for cls in (dom, rng):
            g.add((NS[cls], RDF.type, OWL.Class))
    for a, b in (inverses or {}).items():
        g.add((NS[a], OWL.inverseOf, NS[b]))
    return g


def _missing(prev: Graph, new: Graph) -> set:
    pc = _declaration_capabilities(prev)
    nc = _declaration_capabilities(new)
    return pc["op_links"] - nc["op_links"]


# ──────────────────────────────────────────────────────────────────
# 1. 산 경로 소실을 잡는다 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

def test_live_link_loss_detected_when_abox_uses_the_property(monkeypatch):
    """사라진 OP **자신** 이 A-Box 를 채우고 있었으면 산 경로 손실이다.

    ``owl:inverseOf`` 짝을 통한 판정은 별도 테스트가 다룬다 — 이 테스트는 짝 없이
    단독 OP 가 데이터를 갖는 경로를 찍는다.
    """
    prev = _tbox([("itemSupplierMapHasItem", "ItemSupplierMap", "ItemMaster"),
                  ("isItemOfSupplierMap", "ItemMaster", "ItemSupplierMap")])
    new = _tbox([("itemSupplierMapHasItem", "ItemSupplierMap", "ItemMaster")])
    # 사라진 OP 자신이 쓰이고 있었다.
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names",
        lambda names: {"isItemOfSupplierMap"} & set(names),
    )
    live, unavailable = _live_link_losses(prev, _missing(prev, new))
    assert unavailable is False
    assert len(live) == 1, live
    assert "ItemMaster→ItemSupplierMap" in live[0]


def test_surviving_forward_op_alone_is_not_a_live_loss(monkeypatch):
    """정방향만 쓰이고 ``inverseOf`` 선언이 없으면 그 관계는 손실로 세지 않는다.

    ``inverseOf`` 가 없으면 ``ensure_inverse_triples()`` 가 역방향을 만들 근거도
    없어, 사라진 역방향 OP 는 애초에 데이터를 가진 적이 없다 (phantom). 이 방향을
    빼면 phantom 소실이 차단 사유가 되어 정상적인 정리가 막힌다.
    """
    prev = _tbox([("itemSupplierMapHasItem", "ItemSupplierMap", "ItemMaster"),
                  ("isItemOfSupplierMap", "ItemMaster", "ItemSupplierMap")])
    new = _tbox([("itemSupplierMapHasItem", "ItemSupplierMap", "ItemMaster")])
    # 정방향만 쓰이고, 둘 사이에 inverseOf 선언이 **없다**.
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names",
        lambda names: {"itemSupplierMapHasItem"} & set(names),
    )
    live, _ = _live_link_losses(prev, _missing(prev, new))
    assert live == [], (
        f"inverseOf 선언 없는 정방향 사용을 산 경로로 셌다: {live}"
    )


def test_inverse_pair_usage_counts_as_live(monkeypatch):
    """역방향 OP 자신이 0건이어도 ``owl:inverseOf`` 짝이 데이터를 가지면 산 경로다.

    역방향 트리플은 A-Box 생성기가 아니라 ``ensure_inverse_triples()`` 가 만들므로
    A-Box 파일에는 정방향만 있다. 짝을 보지 않으면 이 축이 영구히 0 을 세고,
    게이트를 켜 놓고도 아무것도 잡지 못한다.
    """
    prev = _tbox(
        [("inventoryStatusHasWarehouse", "InventoryStatus", "WarehouseMaster"),
         ("warehouseHasInventoryStatus", "WarehouseMaster", "InventoryStatus")],
        inverses={"warehouseHasInventoryStatus": "inventoryStatusHasWarehouse"},
    )
    new = _tbox([("inventoryStatusHasWarehouse", "InventoryStatus", "WarehouseMaster")])
    # 사라진 OP 이름 자체는 A-Box 에 **없다** — 짝만 있다.
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names",
        lambda names: {"inventoryStatusHasWarehouse"} & set(names),
    )
    live, _ = _live_link_losses(prev, _missing(prev, new))
    assert len(live) == 1, (
        f"inverseOf 짝의 사용량을 못 봤다 — 이 축은 영구 0 이 된다: {live}"
    )


# ──────────────────────────────────────────────────────────────────
# 2. 과잉 차단 방지 — phantom 소실은 개선이다
# ──────────────────────────────────────────────────────────────────

def test_phantom_loss_is_not_counted(monkeypatch):
    """A-Box 0건 관계가 사라지는 것은 손실이 아니라 개선이다.

    실측 (2026-08-25): 손실 18건 중 11건이 phantom
    (``blastFurnaceFollowedBySteelmaking`` 등). 이것을 세면 정상적인 phantom 정리가
    차단되고, 차단되는 게이트는 결국 꺼진다.
    """
    prev = _tbox([("blastFurnaceFollowedBySteelmaking",
                   "ProcessBlastFurnace", "ProcessSteelmakingFurnace")])
    new = Graph()
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", lambda names: set())
    live, unavailable = _live_link_losses(prev, _missing(prev, new))
    assert unavailable is False
    assert live == [], f"phantom 소실을 손실로 셌다: {live}"


def test_renaming_is_not_a_loss(monkeypatch):
    """같은 관계를 잇는 OP 가 새 이름으로 남으면 손실 0.

    ``op_links`` 가 관계 단위라 이 방향은 구조적으로 보장되지만, 분모/분자 계산이
    이름 축으로 퇴행하면 정상 재생성이 대량 차단된다 (기존 주석의 실측: 이름으로
    재면 44% 손실).
    """
    prev = _tbox([("isWaterMonitoringPointOf",
                   "MonitoringPointMaster", "WaterQualityMonitoring")])
    new = _tbox([("monitoringPointOfWaterQuality",
                  "MonitoringPointMaster", "WaterQualityMonitoring")])
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names",
        lambda names: {"isWaterMonitoringPointOf"} & set(names),
    )
    live, _ = _live_link_losses(prev, _missing(prev, new))
    assert live == [], f"리네이밍을 손실로 셌다: {live}"


def test_no_missing_links_returns_empty(monkeypatch):
    """손실이 없으면 A-Box 스캔조차 하지 않는다 (불필요한 I/O 방지)."""
    g = _tbox([("a", "A", "B")])
    called = []
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names",
        lambda names: called.append(names) or set(),
    )
    live, unavailable = _live_link_losses(g, set())
    assert (live, unavailable) == ([], False)
    assert not called, "손실 0건인데 A-Box 를 스캔했다"


# ──────────────────────────────────────────────────────────────────
# 3. 판정 불가는 차단하지 않는다 (부트스트랩 보호)
# ──────────────────────────────────────────────────────────────────

def test_signal_unavailable_does_not_block(monkeypatch):
    """A-Box 가 없으면(첫 실행) 이 축으로 차단하지 않는다.

    ``abox_used_local_names`` 는 판정 불가를 ``None`` 으로 알린다. 0건과 구분하지
    않으면 A-Box 가 아직 없는 첫 S2 를 영구 차단해 부트스트랩이 불가능해진다
    (이 리포는 같은 계열 사고를 겪었다 — 판정 불가를 미사용으로 읽어 산 데이터를
    지운 이력).
    """
    prev = _tbox([("isItemOfSupplierMap", "ItemMaster", "ItemSupplierMap")])
    new = Graph()
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", lambda names: None)
    live, unavailable = _live_link_losses(prev, _missing(prev, new))
    assert unavailable is True
    assert live == [], "판정 불가인데 손실을 보고했다 — 차단으로 이어진다"


# ──────────────────────────────────────────────────────────────────
# 4. 두 축은 독립이다
# ──────────────────────────────────────────────────────────────────

def test_ratio_threshold_unchanged():
    """비율 축 임계는 건드리지 않았다.

    8/19 정상 실행이 raw 18.8% 였으므로 0.2 미만으로 낮추면 오차단이 생긴다.
    이 단정이 깨지면 그 위험이 되살아난다.
    """
    assert _SAVE_DECL_LOSS_RATIO == 0.2


def test_live_link_max_is_strict():
    """산 경로는 한 개도 허용하지 않는다.

    값을 올리면 이번 회귀(7건)는 여전히 잡히지만, 1~2건 소실이 조용히 통과한다 —
    ``isStackEquipmentOf`` 단건 소실이 정확히 그 크기였다.
    """
    assert _SAVE_LIVE_LINK_LOSS_MAX == 1


@pytest.mark.parametrize(("live_count", "ratio", "should_block"), [
    (0, 0.05, False),   # 정상
    (0, 0.25, True),    # 비율 축만 걸림
    (7, 0.148, True),   # 산 경로 축만 걸림 — THE REGRESSION
    (1, 0.14, True),    # isStackEquipmentOf 단건
    (3, 0.30, True),    # 둘 다
])
def test_block_decision_is_or_of_two_axes(live_count, ratio, should_block):
    """어느 한 축이 걸리면 차단한다 (AND 가 아니라 OR).

    AND 로 묶으면 이번 회귀(비율 통과 + 산 경로 초과)가 그대로 저장된다.
    """
    blocked = ratio >= _SAVE_DECL_LOSS_RATIO or live_count >= _SAVE_LIVE_LINK_LOSS_MAX
    assert blocked is should_block


# ──────────────────────────────────────────────────────────────────
# 5. 배포 산출물 회귀 — 실측으로 두 상태가 갈린다
# ──────────────────────────────────────────────────────────────────

_PREV = "/tmp/pre_s2/t_box.ttl"
_CURRENT = "data/generated/tbox/t_box.ttl"


def test_deployed_tbox_has_no_live_link_loss():
    """현재 배포 T-Box 는 직전 배포본 대비 산 경로 손실이 없다.

    ``tbox_manual_additions.ttl`` 의 역방향 OP 7개가 이 단정을 지탱한다. 그 파일을
    지우거나 step_30 병합이 깨지면 이 테스트가 red 가 된다 — 회귀 탐지기다.
    """
    import os
    if not (os.path.exists(_PREV) and os.path.exists(_CURRENT)):
        pytest.skip("비교용 T-Box 스냅샷 없음 (세션 외 실행)")
    prev = Graph().parse(_PREV, format="turtle")
    new = Graph().parse(_CURRENT, format="turtle")
    live, unavailable = _live_link_losses(prev, _missing(prev, new))
    if unavailable:
        pytest.skip("A-Box 신호 없음")
    assert live == [], (
        f"A-Box·tacit 이 채우는 관계가 소실됐다: {live}. "
        "rules/domain/tbox_manual_additions.ttl 에 역방향 OP 를 명시하라."
    )


def test_manual_additions_declares_reverse_ops():
    """수동 추가분 파일이 역방향 OP 를 정/역 쌍으로 선언한다.

    소스 검사다 — 산출물은 ``data/generated`` (gitignore) 라 저장소에 남지 않으므로
    이 파일이 유일한 영속 경로다. 함수 경계 아래 배선은 소스로만 잡힌다.
    """
    import os

    from domain.rules_paths import rules_path

    path = rules_path("tbox_manual_additions.ttl")
    if not os.path.exists(path):
        pytest.skip("tbox_manual_additions.ttl 없음 (도메인-중립 no-op)")
    from domain.namespaces import SPARQL_PREFIXES
    with open(path, encoding="utf-8") as fh:
        g = Graph().parse(data=SPARQL_PREFIXES + "\n" + fh.read(), format="turtle")

    required = {
        "isItemOfSupplierMap", "isSupplierOfItemSupplierMap",
        "isItemOfInventoryStatus", "warehouseHasInventoryStatus",
        "isItemOfInventoryTransaction", "warehouseHasInventoryTransaction",
        "isStackEquipmentOf",
    }
    declared = {
        str(p).rsplit("#", 1)[-1]
        for p in g.subjects(RDF.type, OWL.ObjectProperty)
    }
    assert required <= declared, f"역방향 OP 누락: {sorted(required - declared)}"

    # 각 OP 가 domain/range/label/inverseOf 를 모두 갖는다 — 하나라도 없으면
    # SHACL 위반이거나 owl:inverseOf 가 미선언 이름을 가리킨다.
    for name in sorted(required):
        op = URIRef(str(DOMAIN_NS) + name)
        for label, pred in (("domain", RDFS.domain), ("range", RDFS.range),
                            ("label", RDFS.label), ("inverseOf", OWL.inverseOf)):
            assert list(g.objects(op, pred)), f"{name} 에 {label} 없음"
