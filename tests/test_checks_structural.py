"""Tests for tools/validation_support/checks/structural.py — Session 10.

새 패키지 경로로 직접 import해서 호출. 기존 tools.kg_validation._check_*
경로 테스트는 test_kg_validation.py / test_adaptive_gates.py에서 계속 커버.
"""
from __future__ import annotations

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.validation_support.checks.structural import (
    check_bidirectional_op,
    check_class_instance_count,
    check_orphan_nodes,
    check_process_flow,
)
from tools.validation_support.common import SharedCheckContext


def _cls(name):
    return URIRef(f"{DOMAIN_NS}{name}")


def _inst(name):
    return URIRef(f"{DOMAIN_INST_NS}{name}")


def test_bidirectional_empty_pairs():
    g = _new_graph()
    tbox = _new_graph()
    r = check_bidirectional_op(g, tbox)
    assert r["passed"] is True
    assert r["total_pairs"] == 0


def test_bidirectional_with_shared_ctx():
    """shared.inverse_pairs 캐시가 쓰이는지."""
    g = _new_graph()
    tbox = _new_graph()
    tbox.add((_cls("hasA"), RDF.type, OWL.ObjectProperty))
    tbox.add((_cls("isAOf"), RDF.type, OWL.ObjectProperty))
    tbox.add((_cls("hasA"), OWL.inverseOf, _cls("isAOf")))
    shared = SharedCheckContext(g, tbox)
    r = check_bidirectional_op(g, tbox, shared=shared)
    assert r["total_pairs"] == 1


def test_process_flow_no_domain_auto_pass():
    g = _new_graph()
    r = check_process_flow(g)
    assert r["passed"] is True
    assert r["steps"] == 0


def test_orphan_nodes_no_typed_entities():
    g = _new_graph()
    r = check_orphan_nodes(g)
    assert r["passed"] is True
    assert r["orphan_count"] == 0


def test_orphan_detects_isolated():
    g = _new_graph()
    eq_cls = _cls("Eq")
    g.add((eq_cls, RDF.type, OWL.Class))
    orphan = _inst("Orphan1")
    connected = _inst("Connected1")
    target = _inst("Target")
    g.add((orphan, RDF.type, eq_cls))
    g.add((connected, RDF.type, eq_cls))
    g.add((target, RDF.type, eq_cls))
    # connected는 target과 연결
    g.add((connected, _cls("hasLink"), target))
    r = check_orphan_nodes(g)
    assert r["orphan_count"] >= 1
    orphan_names = {s["entity"] for s in r["samples"]}
    assert "Orphan1" in orphan_names


def test_class_instance_count_no_instances_ratio():
    g = _new_graph()
    tbox = _new_graph()
    for name in ["A", "B", "C"]:
        tbox.add((_cls(name), RDF.type, OWL.Class))
    r = check_class_instance_count(g, tbox=tbox)
    # 3개 클래스 중 0개에 인스턴스 → ratio 100%
    assert r["no_instance_ratio"] == 100.0
    assert r["passed"] is False


def test_class_instance_count_with_inferred_tier():
    g = _new_graph()
    tbox = _new_graph()
    for name in ["A", "Inferred1"]:
        tbox.add((_cls(name), RDF.type, OWL.Class))
    g.add((_inst("A_1"), RDF.type, _cls("A")))
    r = check_class_instance_count(
        g, tbox=tbox,
        class_tiers={"A": "master", "Inferred1": "inferred"},
    )
    # Inferred1은 no_instance에서 제외 → 남은 no_instance 0개
    assert "Inferred1" not in r["no_instance_classes"]
    assert r["passed"] is True


# ── 이슈 3 개선: no_instance 클래스를 추론 가능성으로 분류 ──────


def test_class_instance_count_splits_derivable_vs_orphan():
    """no_instance 클래스 중 owl:Restriction hasValue 가 있으면 derivable_by_restriction,
    없으면 truly_orphan 으로 분리 보고 — 이슈 3 리포트 개선.

    예: Scope1Emission 이 Scope1Emission_scopeType_hasValue 제약을 상속하면
    인스턴스 0 이어도 reasoner 가 Scope_Type 값 기반으로 분류 추론 가능 →
    SME 관점에서 "over-engineered" 가 아니라 "추론 의존 클래스".
    """
    g = _new_graph()
    tbox = _new_graph()
    # 3개 클래스: Base (인스턴스 1개), DerivableA (restriction 제약), Orphan1 (순수 고아)
    tbox.add((_cls("Base"), RDF.type, OWL.Class))
    tbox.add((_cls("DerivableA"), RDF.type, OWL.Class))
    tbox.add((_cls("Orphan1"), RDF.type, OWL.Class))

    # DerivableA 는 hasValue 제약을 가진 subClassOf restriction
    # (Scope1Emission 이 Scope_Type=Scope1 으로 분류되는 패턴을 모방)
    from rdflib import Literal
    restr = URIRef(f"{DOMAIN_NS}bnode-restriction-1")
    tbox.add((restr, RDF.type, OWL.Restriction))
    tbox.add((restr, OWL.onProperty, _cls("scopeType")))
    tbox.add((restr, OWL.hasValue, Literal("Scope1")))
    tbox.add((_cls("DerivableA"), RDFS.subClassOf, restr))

    # Base 만 인스턴스 보유. **그 인스턴스가 공리의 값을 가져야** derivable 이다 —
    # 2026-08-27 부터 도달성 검사가 추가됐다: hasValue 값이 A-Box 에 없으면 추론기가
    # 몇 번 돌려도 그 클래스는 영구 0건이므로 derivable 이 아니다 (배포 T-Box 실측:
    # 정의 클래스 22개 중 9개가 값 불일치로 영구 0건이었고, 그것을 derivable 로 세면
    # 결함이 정상으로 위장된다). 상세: tests/test_defined_class_gate_and_functional.py
    g.add((_inst("Base_1"), RDF.type, _cls("Base")))
    g.add((_inst("Base_1"), _cls("scopeType"), Literal("Scope1")))

    r = check_class_instance_count(g, tbox=tbox)

    # 기존 필드 유지 (backward compat)
    assert set(r["no_instance_classes"]) == {"DerivableA", "Orphan1"}

    # 신규 필드: 추론 가능 여부로 분리
    assert "no_instance_derivable" in r
    assert "no_instance_truly_orphan" in r
    assert "DerivableA" in r["no_instance_derivable"]
    assert "Orphan1" in r["no_instance_truly_orphan"]
    assert "DerivableA" not in r["no_instance_truly_orphan"]


def test_class_instance_count_derivable_empty_when_no_restrictions():
    """restriction 없는 환경 → no_instance_derivable 빈 리스트."""
    g = _new_graph()
    tbox = _new_graph()
    for name in ["A", "B"]:
        tbox.add((_cls(name), RDF.type, OWL.Class))

    r = check_class_instance_count(g, tbox=tbox)
    assert r.get("no_instance_derivable") == []
    assert set(r.get("no_instance_truly_orphan", [])) == {"A", "B"}


def test_reexport_from_kg_validation():
    """tools.kg_validation._check_* 경로가 여전히 같은 함수를 가리키는지."""
    from tools.kg_validation import (
        _check_bidirectional_op,
        _check_orphan_nodes,
        _check_process_flow,
    )
    assert _check_bidirectional_op is check_bidirectional_op
    assert _check_orphan_nodes is check_orphan_nodes
    assert _check_process_flow is check_process_flow


def test_reexport_package():
    """tools.validation_support.checks.__init__에서도 import 가능."""
    from tools.validation_support.checks import (
        check_bidirectional_op as a,
    )
    from tools.validation_support.checks import (
        check_class_instance_count as b,
    )
    from tools.validation_support.checks import (
        check_orphan_nodes as c,
    )
    from tools.validation_support.checks import (
        check_process_flow as d,
    )
    assert callable(a) and callable(b) and callable(c) and callable(d)
