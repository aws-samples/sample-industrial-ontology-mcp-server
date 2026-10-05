"""ODP 추상 그룹 주입이 subClassOf 순환을 만들지 않는다.

2026-08-23 실측 (S3, 신규 S2 산출물): `check_quality_rules` 가 **critical 12건**
(`subclass_cycle`) 으로 S4 를 FAIL 시켰다. 고유 순환은 **1개** 였고 (조상마다
중복 보고돼 12건으로 보였다):

    MonitoringManagement → EnvironmentalMonitoring → MonitoringManagement

## 원인 — 부분 문자열 매칭이 상위 개념을 자식으로 끌어왔다

`rules/domain/abstract_group_hints.json` 의 그룹:

    {"abstract_class": "EnvironmentalMonitoring",
     "child_name_patterns": ["Monitoring", "_Emission"], ...}

패턴 ``"Monitoring"`` 이 **``MonitoringManagement`` 자신을** 매칭한다. 그런데
그 클래스는 이미 ``EnvironmentalMonitoring`` 의 부모다
(`design_patterns.json` 의 ``모니터링`` 그룹). 결과가 상호 subClassOf 다.

## 왜 step_22c 로 부족한가

`step_22c_subclass_cycle` (Tarjan SCC) 은 순환을 **정확히 끊는다** — 배포 T-Box
에 직접 돌려 확인했다(`sub_class_cycles_broken: 1`). 문제는 실행 순서다:
``_POST_STEPS`` 에서 **22c 가 먼저, 24b 가 나중** 이라 24b 가 방금 끊은 순환을
다시 만든다. 스텝 단위로 추적해 그 전환을 관측했다:

    [_POST_STEPS] step_22c_subclass_cycle: 순환 True → False
    [_POST_STEPS] step_24b_odp_auto_apply: 순환 False → True   ← 재생성

이 리포의 "수정이 다음 결함을 만든다" 패턴이다. 사후 정리(22c)만으로는 부족하고
**생성 지점**(24b)에서 막아야 한다.

판정은 공용 헬퍼 `domain.graph_utils.is_ancestor_of` 에 위임한다 — 같은 판정이
계층을 만드는 다른 스텝(step_12 / step_14 / jury 의 add_class_hierarchy)에도
필요하고, 사본으로 두면 한 곳만 고쳐진다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.graph_utils import is_ancestor_of
from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import _apply_odp_abstract_groups

NS = DOMAIN_NS


def _cls(g: Graph, name: str) -> URIRef:
    u = URIRef(NS + name)
    g.add((u, RDF.type, OWL.Class))
    return u


# ──────────────────────────────────────────────────────────────────
# 1. is_ancestor_of 헬퍼
# ──────────────────────────────────────────────────────────────────

def test_direct_parent_is_ancestor():
    g = Graph()
    child, parent = _cls(g, "Child"), _cls(g, "Parent")
    g.add((child, RDFS.subClassOf, parent))
    assert is_ancestor_of(g, parent, child) is True
    assert is_ancestor_of(g, child, parent) is False


def test_transitive_ancestor():
    g = Graph()
    a, b, c = _cls(g, "A"), _cls(g, "B"), _cls(g, "C")
    g.add((a, RDFS.subClassOf, b))
    g.add((b, RDFS.subClassOf, c))
    assert is_ancestor_of(g, c, a) is True


def test_self_is_ancestor():
    g = Graph()
    a = _cls(g, "A")
    assert is_ancestor_of(g, a, a) is True


def test_unrelated_is_not_ancestor():
    g = Graph()
    a, b = _cls(g, "A"), _cls(g, "B")
    assert is_ancestor_of(g, b, a) is False


def test_terminates_on_existing_cycle():
    """이미 순환이 있어도 무한 루프에 빠지지 않는다."""
    g = Graph()
    a, b = _cls(g, "A"), _cls(g, "B")
    g.add((a, RDFS.subClassOf, b))
    g.add((b, RDFS.subClassOf, a))
    assert is_ancestor_of(g, b, a) is True
    assert is_ancestor_of(g, URIRef(NS + "Other"), a) is False


def test_named_restriction_is_not_traversed():
    """공리 노드(named Restriction)를 경유하면 무관한 조상이 보인다."""
    g = Graph()
    child = _cls(g, "Child")
    restriction = URIRef(NS + "Child_someProp_someValuesFrom")
    unrelated = _cls(g, "Unrelated")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((child, RDFS.subClassOf, restriction))
    g.add((restriction, RDFS.subClassOf, unrelated))
    assert is_ancestor_of(g, unrelated, child) is False, (
        "Restriction 을 타고 가면 공리가 분류 계층으로 오인된다"
    )


@pytest.mark.parametrize("bad", [None, "string", 123])
def test_non_uriref_returns_false(bad):
    g = Graph()
    assert is_ancestor_of(g, bad, _cls(g, "A")) is False
    assert is_ancestor_of(g, _cls(g, "A"), bad) is False


# ──────────────────────────────────────────────────────────────────
# 2. ODP 주입이 순환을 만들지 않는다 (실측 사례)
# ──────────────────────────────────────────────────────────────────

def test_odp_skips_link_that_would_create_cycle():
    """실측 재현 — 후보가 이미 abstract_class 의 조상이면 건너뛴다."""
    g = Graph()
    mgmt = _cls(g, "MonitoringManagement")
    env = _cls(g, "EnvironmentalMonitoring")
    # 실제 T-Box 상태: EnvironmentalMonitoring ⊑ MonitoringManagement
    g.add((env, RDFS.subClassOf, mgmt))
    # 패턴이 매칭할 정상 자식도 함께 둔다 (그룹이 skip 되지 않게)
    for name in ("AirEmissionMonitoring", "WaterQualityMonitoring"):
        _cls(g, name)

    stats = _apply_odp_abstract_groups(g, NS)
    assert (mgmt, RDFS.subClassOf, env) not in g, (
        "순환을 만드는 링크가 주입됐다"
    )
    assert stats["odp_cycle_links_skipped"] >= 1, stats


def test_odp_still_links_legitimate_children():
    """가드가 정당한 주입을 막지 않는다 (과잉 차단 방지 — 주 방향)."""
    g = Graph()
    _cls(g, "EnvironmentalMonitoring")
    child = _cls(g, "AirEmissionMonitoring")
    stats = _apply_odp_abstract_groups(g, NS)
    parents = set(g.objects(child, RDFS.subClassOf))
    assert parents, f"정당한 자식이 부모를 못 받았다 (stats={stats})"


def test_odp_is_idempotent():
    g = Graph()
    _cls(g, "EnvironmentalMonitoring")
    _cls(g, "AirEmissionMonitoring")
    _apply_odp_abstract_groups(g, NS)
    size = len(g)
    stats2 = _apply_odp_abstract_groups(g, NS)
    assert len(g) == size
    assert stats2["odp_subclass_links_added"] == 0


# ──────────────────────────────────────────────────────────────────
# 3. 산출물 회귀 — 배포 T-Box 에 순환이 없다
# ──────────────────────────────────────────────────────────────────

def test_deployed_tbox_has_no_subclass_cycle():
    """배포 T-Box 에 subClassOf 순환이 없다.

    있으면 ``check_quality_rules`` 가 critical 로 S4 를 FAIL 시키고, baseline
    FAIL 이 되면 S4.5 mutation 검출률도 함께 죽는다.
    """
    import collections
    import os

    path = "data/generated/tbox/t_box.ttl"
    if not os.path.exists(path):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(path, format="turtle")
    named_r = {
        s for s in g.subjects(RDF.type, OWL.Restriction) if isinstance(s, URIRef)
    }
    parents: dict[URIRef, set[URIRef]] = collections.defaultdict(set)
    for s, o in g.subject_objects(RDFS.subClassOf):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and o not in named_r:
            parents[s].add(o)

    def _has_cycle(start: URIRef) -> bool:
        stack = [(start, {start})]
        while stack:
            node, seen = stack.pop()
            for parent in parents.get(node, ()):
                if parent == start:
                    return True
                if parent not in seen:
                    stack.append((parent, seen | {parent}))
        return False

    offenders = sorted(str(c).split("#")[-1] for c in parents if _has_cycle(c))
    assert not offenders, f"subClassOf 순환에 속한 클래스: {offenders}"


def test_deployed_tbox_quality_gate_has_no_critical():
    import json
    import os

    from tools.validation_core import check_quality_rules

    path = "data/generated/tbox/t_box.ttl"
    if not os.path.exists(path):
        pytest.skip("배포 T-Box 없음")
    payload = json.loads(check_quality_rules(ttl_path="t_box.ttl"))
    cycles = [
        i for i in payload.get("issues", [])
        if i.get("rule") == "subclass_cycle"
    ]
    assert not cycles, f"subclass_cycle 재발: {len(cycles)}건"
    assert payload.get("critical") == 0, payload.get("critical")
