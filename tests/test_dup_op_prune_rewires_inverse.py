"""중복 OP 제거가 짝의 ``owl:inverseOf`` 를 끊어 역방향 OP 를 고아로 만들었다.

2026-08-26 실측. `TBOX_DUP_OP_PRUNE=on` 으로 15 그룹을 정리했더니 S9 프로퍼티
커버리지가 **96.4% → 93.8%** 로 떨어졌다. 원인:

``_prune`` 이 제거 대상을 가리키는 참조를 훑으면서 ``owl:onProperty`` 만 정본으로
치환하고, ``owl:inverseOf`` 는 **치환 없이 삭제** 했다. 그래서 짝 쪽 선언은 남고
링크만 끊겨 역방향 OP 8개가 고아가 됐다::

    isEquipmentOfBlastFurnace   선언 O / inverseOf 없음   ← 고아
    isProductOfBlastFurnace     선언 O / inverseOf 없음
    isEquipmentOfContinuousCasting …  (총 8개)

``inverseOf`` 가 없으면 ``load_graph()`` 의 ``ensure_inverse_triples()`` 가 역방향
트리플을 만들 근거가 없다 — 선언은 있는데 값이 0건인 "빈 관계" 가 되고, 그것으로
질의하면 에러 없이 0건이 정답처럼 반환된다. 지우려는 것은 **중복 이름** 이지
관계가 아니므로 이것은 의도 밖 손실이다.

## 교정 후 (산출물 실측)

    inverse_rewired_to_keep     12건
    고아 역방향 OP              8 → 0
    S9 프로퍼티 커버리지        93.8% → 96.8%  (prune 전 96.4% 보다도 높다)
    양방향 연결 쌍              90 → 102
    추론 justification          inverse_of 규칙 19,698건 신규 등장

## 방향 호환성을 반드시 본다

이름이 아니라 **선언된 domain/range** 로 판정한다 (``_inverse_direction_matches``).
라벨 토큰은 domain==range 인 관계에서 무력하고, 방향이 안 맞는 것을 ``inverseOf`` 로
이으면 OWL RL ``prp-inv1`` 이 **틀린 방향 트리플을 파생** 한다 — 이 리포에는 반대
방향 공리 4개가 conformance·HermiT·quality_rules 를 모두 통과한 이력이 있다.
한쪽이라도 domain/range 를 선언하지 않았으면 잇지 않는다 (판정 불가 = 보류).
"""
from __future__ import annotations

import os

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps._base import StepContext
from tools.quality_steps.step_22e_duplicate_op_prune import (
    _inverse_direction_matches,
    apply,
)

NS = Namespace(str(DOMAIN_NS))


def _op(g: Graph, name: str, dom: str, rng: str) -> URIRef:
    u = NS[name]
    g.add((u, RDF.type, OWL.ObjectProperty))
    g.add((u, RDFS.domain, NS[dom]))
    g.add((u, RDFS.range, NS[rng]))
    g.add((NS[dom], RDF.type, OWL.Class))
    g.add((NS[rng], RDF.type, OWL.Class))
    return u


def _ctx() -> StepContext:
    return StepContext(domain_ns=str(DOMAIN_NS))


# ──────────────────────────────────────────────────────────────────
# 1. 방향 판정 (이름이 아니라 domain/range)
# ──────────────────────────────────────────────────────────────────

def test_direction_matches_when_domain_range_swapped():
    g = Graph()
    a = _op(g, "hasBlastFurnaceEquipment", "ProcessBlastFurnace", "EquipmentMaster")
    b = _op(g, "isEquipmentOfBlastFurnace", "EquipmentMaster", "ProcessBlastFurnace")
    assert _inverse_direction_matches(g, b, a) is True


def test_direction_rejected_when_not_swapped():
    """방향이 같으면 역관계가 아니다 — 이으면 틀린 파생이 생긴다."""
    g = Graph()
    a = _op(g, "opA", "X", "Y")
    b = _op(g, "opB", "X", "Y")     # 같은 방향
    assert _inverse_direction_matches(g, b, a) is False


def test_direction_unknown_is_rejected():
    """domain/range 가 없으면 판정 불가 — 잇지 않는다 (보수적)."""
    g = Graph()
    a = _op(g, "opA", "X", "Y")
    b = NS["opB"]
    g.add((b, RDF.type, OWL.ObjectProperty))   # domain/range 없음
    assert _inverse_direction_matches(g, b, a) is False


# ──────────────────────────────────────────────────────────────────
# 2. 재연결 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

def _dup_graph() -> Graph:
    """정본/제거 후보 + 제거 후보를 가리키는 역방향 짝."""
    g = Graph()
    _op(g, "hasBlastFurnaceEquipment", "ProcessBlastFurnace", "EquipmentMaster")
    victim = _op(g, "blastFurnaceUsesEquipment",
                 "ProcessBlastFurnace", "EquipmentMaster")
    partner = _op(g, "isEquipmentOfBlastFurnace",
                  "EquipmentMaster", "ProcessBlastFurnace")
    g.add((partner, OWL.inverseOf, victim))
    return g


def test_partner_inverse_is_rewired_to_canonical(monkeypatch):
    """제거 대상을 가리켰던 짝이 정본을 가리키게 된다."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _dup_graph()
    result = apply(g, _ctx())
    keep = NS["hasBlastFurnaceEquipment"]
    partner = NS["isEquipmentOfBlastFurnace"]
    victim = NS["blastFurnaceUsesEquipment"]

    assert (victim, RDF.type, OWL.ObjectProperty) not in g, "제거되지 않았다"
    assert (partner, OWL.inverseOf, keep) in g, (
        "짝이 정본을 가리키지 않는다 — 역방향 OP 가 고아가 되고 "
        "ensure_inverse_triples() 가 값을 채울 근거를 잃는다"
    )
    assert result.stats.get("inverse_rewired_to_keep", 0) >= 1


def test_no_dangling_inverse_after_prune(monkeypatch):
    """제거 후 어떤 ``inverseOf`` 도 미선언 이름을 가리키지 않는다."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _dup_graph()
    apply(g, _ctx())
    declared = set(g.subjects(RDF.type, OWL.ObjectProperty))
    dangling = [
        (s, o) for s, o in g.subject_objects(OWL.inverseOf)
        if s not in declared or o not in declared
    ]
    assert not dangling, f"dangling inverseOf: {dangling}"


def test_incompatible_partner_is_not_rewired(monkeypatch):
    """방향이 안 맞는 짝은 잇지 않는다 (과잉 교정 방지).

    이 방향이 없으면 "무조건 정본으로 잇는다" 가 되어 prp-inv1 이 틀린 방향
    트리플을 파생한다.
    """
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = Graph()
    _op(g, "keepOp", "A", "B")
    victim = _op(g, "victimOp", "A", "B")
    # 짝인데 방향이 keep 과 호환되지 않는다 (A→B 가 아니라 A→C)
    partner = _op(g, "partnerOp", "A", "C")
    g.add((partner, OWL.inverseOf, victim))
    apply(g, _ctx())
    assert (partner, OWL.inverseOf, NS["keepOp"]) not in g, (
        "방향 불호환인데 이었다 — 틀린 방향 파생 위험"
    )


def test_onproperty_still_substituted(monkeypatch):
    """기존 동작 보존 — Restriction 의 onProperty 는 여전히 정본으로 치환된다."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _dup_graph()
    restr = NS["SomeRestriction"]
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, NS["blastFurnaceUsesEquipment"]))
    apply(g, _ctx())
    assert (restr, OWL.onProperty, NS["hasBlastFurnaceEquipment"]) in g
    assert (restr, OWL.onProperty, NS["blastFurnaceUsesEquipment"]) not in g


def test_off_mode_changes_nothing(monkeypatch):
    """기본 off 에서는 그래프를 건드리지 않는다 (계획만)."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "off")
    g = _dup_graph()
    before = set(g)
    result = apply(g, _ctx())
    assert set(g) == before, "off 인데 그래프가 변했다"
    assert result.stats.get("prune_mode") == "off"


# ──────────────────────────────────────────────────────────────────
# 3. 배포 산출물 회귀
# ──────────────────────────────────────────────────────────────────

_TBOX = "data/generated/tbox/t_box.ttl"

#: prune 으로 정본이 된 OP → 그 역방향 짝. 짝이 고아가 되면 커버리지가 떨어진다.
_EXPECTED_PAIRS = [
    ("hasBlastFurnaceEquipment", "isEquipmentOfBlastFurnace"),
    ("hasBlastFurnaceProduct", "isProductOfBlastFurnace"),
    ("hasContinuousCastingEquipment", "isEquipmentOfContinuousCasting"),
    ("hasContinuousCastingProduct", "isProductOfContinuousCasting"),
    ("tagEquipment", "equipmentHasTag"),
    ("surfaceQualityOfProduct", "isSurfaceQualityProductOf"),
    ("hasElectricalConsumptionEquipment", "isElectricalEquipmentOf"),
    ("hasMechanicalProduct", "isMechanicalTestProductOf"),
]


def _deployed() -> Graph:
    if not os.path.exists(_TBOX):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(_TBOX, format="turtle")
    return g


@pytest.mark.parametrize(("keep", "partner"), _EXPECTED_PAIRS)
def test_deployed_reverse_ops_are_not_orphaned(keep, partner):
    """배포 T-Box 에서 역방향 OP 가 짝을 갖는다.

    실측: prune 직후 이 8개가 전부 고아였고 커버리지가 96.4% → 93.8% 로 떨어졌다.
    """
    g = _deployed()
    pu, ku = NS[partner], NS[keep]
    if (pu, RDF.type, OWL.ObjectProperty) not in g:
        pytest.skip(f"{partner} 미선언 (T-Box 재생성으로 사라졌을 수 있음)")
    linked = (pu, OWL.inverseOf, ku) in g or (ku, OWL.inverseOf, pu) in g
    assert linked, (
        f"{partner} 가 {keep} 와 inverseOf 로 연결되지 않았다 — 고아 역방향 OP. "
        "ensure_inverse_triples() 가 값을 채울 수 없어 빈 관계가 된다"
    )


def test_deployed_tbox_has_no_dangling_inverse():
    """배포 T-Box 의 ``inverseOf`` 가 전부 선언된 OP 를 가리킨다."""
    g = _deployed()
    ns = str(DOMAIN_NS)
    declared = {
        str(p) for p in g.subjects(RDF.type, OWL.ObjectProperty)
        if str(p).startswith(ns)
    }
    dangling = {
        str(x).rsplit("#", 1)[-1]
        for s, o in g.subject_objects(OWL.inverseOf)
        for x in (s, o)
        if str(x).startswith(ns) and str(x) not in declared
    }
    assert not dangling, f"미선언 이름을 가리키는 inverseOf: {sorted(dangling)}"


def test_deployed_inverse_pairs_have_swapped_direction():
    """배포 T-Box 의 모든 ``inverseOf`` 쌍이 방향이 뒤바뀐 관계다.

    한 방향으로 나란한 쌍을 이어 두면 prp-inv1 이 틀린 트리플을 파생한다.
    """
    g = _deployed()
    bad = []
    for s, o in g.subject_objects(OWL.inverseOf):
        sd, sr = set(g.objects(s, RDFS.domain)), set(g.objects(s, RDFS.range))
        od, orr = set(g.objects(o, RDFS.domain)), set(g.objects(o, RDFS.range))
        if sd and sr and od and orr and not (sd == orr and sr == od):
            bad.append(f"{str(s).rsplit('#', 1)[-1]}↔{str(o).rsplit('#', 1)[-1]}")
    assert not bad, f"방향이 뒤바뀌지 않은 inverseOf 쌍: {bad}"
