"""Disjointness 처리 두 결함의 회귀 가드.

1. **step_01 이 외부 저작 공리를 지웠다** (``step_01_disjoint_complete.py``)
   모든 ``owl:AllDisjointClasses`` 를 무조건 지우고 ``DISJOINT_GROUPS`` 설정으로만
   다시 세웠다. 그래서 S2 Jury / ``rules/domain/tbox_manual_additions.ttl`` / tacit 이
   **설정 밖 클래스** 에 대해 저작한 공리가 대체 없이 사라졌다. Validator 가 요구해
   Jury 가 넣은 수정이 S3 에서 조용히 되돌려지고, stats 는 파괴된 것을 말하지 않았다.
   같은 함수의 pairwise 루프는 이미 설정 범위로 한정돼 있어 **두 표기의 처리가
   비대칭** 이었다는 점이 결함의 증거다.

2. **검증 게이트가 표기 하나만 읽었다** (``check_disjoint_class_violations``)
   ``owl:AllDisjointClasses`` 만 수집하고 pairwise ``owl:disjointWith`` 는 무시했다.
   그러면 그룹이 0이라 early return 이 그대로 PASS 를 반환하고,
   ``disjoint_groups=0`` 은 "선언이 없다" 로 읽힌다. 이 리포는 pairwise 를 정상
   입력으로 취급한다 (``validation_core._rule_disjoint_style`` 이 WARNING 으로
   잡으며 변환을 권고). 추론이 넣는 type pollution (상호배타 클래스 2개에 동시
   소속) 이 정확히 이 게이트가 잡아야 할 결함이다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, BNode, Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL

STEEL = "http://example.com/steel-ontology#"


def _c(name: str) -> URIRef:
    return URIRef(STEEL + name)


def _add_all_disjoint(g: Graph, members: list[str]) -> BNode:
    node = BNode()
    g.add((node, RDF.type, OWL.AllDisjointClasses))
    lst = BNode()
    Collection(g, lst, [_c(m) for m in members])
    g.add((node, OWL.members, lst))
    for m in members:
        g.add((_c(m), RDF.type, OWL.Class))
    return node


# ── 1. step_01 보존 ──────────────────────────────────────────────────


def _apply_step_01(g: Graph):
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_01_disjoint_complete import apply
    return apply(g, StepContext(domain_ns=STEEL))


def _adc_member_sets(g: Graph) -> list[set[str]]:
    out = []
    for node in g.subjects(RDF.type, OWL.AllDisjointClasses):
        members_node = g.value(node, OWL.members)
        if members_node is None:
            continue
        try:
            out.append({str(m)[len(STEEL):] for m in Collection(g, members_node)})
        except Exception:  # noqa: BLE001
            continue
    return out


def test_external_axiom_about_unconfigured_classes_survives():
    """THE REGRESSION: 설정 밖 클래스의 외부 저작 공리는 보존된다."""
    g = Graph()
    _add_all_disjoint(g, ["JuryAlpha", "JuryBeta"])

    result = _apply_step_01(g)

    assert {"JuryAlpha", "JuryBeta"} in _adc_member_sets(g), (
        "Jury/수동추가/tacit 이 저작한 disjointness 가 재구축으로 사라졌다"
    )
    assert result.stats["external_disjoint_preserved"] >= 1, (
        "보존 사실이 stats 에 남아야 한다 (조용한 파괴 방지)"
    )


def test_configured_group_axiom_is_still_rebuilt():
    """POSITIVE: 설정 그룹에 속한 공리는 여전히 재구축된다 (기능 보존).

    설정 클래스를 포함한 낡은/부분적인 노드는 지우고 config 기준으로 다시 세운다.
    """
    from tools.ontology_quality import DISJOINT_GROUPS

    if not DISJOINT_GROUPS or len(DISJOINT_GROUPS[0]) < 2:
        pytest.skip("설정에 disjoint 그룹이 없다")
    configured = DISJOINT_GROUPS[0][:2]

    g = Graph()
    _add_all_disjoint(g, list(configured))
    result = _apply_step_01(g)

    # 재구축이 일어났고(추가 카운트) 설정 클래스 공리가 존재한다.
    assert result.stats["disjoint_groups_added"] >= 1
    assert any(set(configured) <= s for s in _adc_member_sets(g))


def test_mixed_graph_keeps_external_and_rebuilds_configured():
    """외부 공리와 설정 공리가 섞여 있어도 각각 올바르게 처리된다."""
    from tools.ontology_quality import DISJOINT_GROUPS

    if not DISJOINT_GROUPS or len(DISJOINT_GROUPS[0]) < 2:
        pytest.skip("설정에 disjoint 그룹이 없다")

    g = Graph()
    _add_all_disjoint(g, ["JuryAlpha", "JuryBeta"])
    _add_all_disjoint(g, list(DISJOINT_GROUPS[0][:2]))

    _apply_step_01(g)
    sets = _adc_member_sets(g)
    assert {"JuryAlpha", "JuryBeta"} in sets
    assert any(set(DISJOINT_GROUPS[0][:2]) <= s for s in sets)


# ── 2. 게이트가 두 표기를 모두 읽는지 ────────────────────────────────


def _check(tbox: Graph, abox: Graph) -> dict:
    from tools.validation_support.checks.temporal_cardinality import (
        check_disjoint_class_violations,
    )
    from tools.validation_support.common import SharedCheckContext

    merged = tbox + abox
    return check_disjoint_class_violations(
        merged, tbox, shared=SharedCheckContext(merged, tbox),
    )


def _violating_abox() -> Graph:
    """상호배타 클래스 2개에 동시 소속인 인스턴스 (type pollution)."""
    abox = Graph()
    inst = URIRef(STEEL + "X1")
    abox.add((inst, RDF.type, _c("Equipment")))
    abox.add((inst, RDF.type, _c("Process")))
    return abox


def test_all_disjoint_classes_spelling_is_enforced():
    """기존 표기는 그대로 검출된다."""
    tbox = Graph()
    _add_all_disjoint(tbox, ["Equipment", "Process"])
    result = _check(tbox, _violating_abox())
    assert result["passed"] is False and len(result["violations"]) == 1


def test_pairwise_disjoint_with_spelling_is_enforced():
    """THE REGRESSION: pairwise ``owl:disjointWith`` 도 검출해야 한다."""
    tbox = Graph()
    for cls in ("Equipment", "Process"):
        tbox.add((_c(cls), RDF.type, OWL.Class))
    tbox.add((_c("Equipment"), OWL.disjointWith, _c("Process")))

    result = _check(tbox, _violating_abox())

    assert result["disjoint_groups"] >= 1, (
        "pairwise 선언이 그룹으로 수집되지 않아 게이트가 무조건 PASS 한다"
    )
    assert result["pairwise_disjoint_pairs"] >= 1
    assert result["passed"] is False, "상호배타 클래스 2개 동시 소속이 통과됐다"


def test_no_declaration_still_passes():
    """선언이 아예 없으면 판정 대상이 없다 (오탐 방지)."""
    tbox = Graph()
    for cls in ("Equipment", "Process"):
        tbox.add((_c(cls), RDF.type, OWL.Class))
    result = _check(tbox, _violating_abox())
    assert result["passed"] is True and result["disjoint_groups"] == 0


def test_pairs_outside_the_detected_namespace_are_ignored():
    """감지된 도메인 네임스페이스 밖의 쌍은 수집하지 않는다.

    ``shared.ns`` 는 T-Box 에서 **다수 네임스페이스** 를 감지하므로, 도메인 클래스가
    다수인 그래프에 외래 쌍 하나가 섞이면 그 쌍만 제외돼야 한다.
    """
    tbox = Graph()
    for cls in ("Equipment", "Process", "Order", "Plant"):
        tbox.add((_c(cls), RDF.type, OWL.Class))
    foreign_a = URIRef("http://elsewhere.org/x#A")
    foreign_b = URIRef("http://elsewhere.org/x#B")
    for cls in (foreign_a, foreign_b):
        tbox.add((cls, RDF.type, OWL.Class))
    tbox.add((foreign_a, OWL.disjointWith, foreign_b))

    result = _check(tbox, _violating_abox())

    assert result["pairwise_disjoint_pairs"] == 0, (
        "외래 네임스페이스 쌍이 도메인 disjoint 그룹으로 수집됐다"
    )
