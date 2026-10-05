"""익명 클래스 표현식의 ``owl:deprecated`` 잔재가 게이트를 죽이지 않는다.

2026-08-19 실측: skolemize 된 ``Union_ManufacturingProcessStep_18a9fee9`` 하나에
과거 실행이 붙인 ``owl:deprecated`` 가 ``deprecated_reference`` high 1건을 냈고,
그것이 ``check_quality_rules`` 를 **baseline FAIL** 로 고정시켰다.
``mutation_runner._caught_by`` 는 PASS→WARN/FAIL 강등만 검출로 세므로 baseline 이
FAIL 이면 그 체크로는 **어떤 mutant 도 잡히지 않는다** (S4.5 검출률 14.3%).

수정 후 실측 (같은 T-Box, 21 mutant 중 적용 7건):

    검출 1/7 (14.3%)  →  3/7 (42.9%)
    새로 잡힌 것: M5_annotation/delete_label, M5_annotation/uri_as_label
    (둘 다 정확히 이 게이트의 담당 영역이었다)

**두 층이 모두 필요하다** — 이 리포의 "가드는 신규 쓰기만 막는다" 패턴:

1. ``_rule_deprecated_reference`` 의 익명 표현식 필터 → **보고** 를 멈춘다.
2. ``step_21c`` 정리 스텝 → **산출물** 의 무의미한 트리플을 지운다.
   step_21 은 이제 익명 노드를 아예 보지 않으므로(커밋 420b250) 자기가 과거에
   붙인 플래그를 제거할 경로가 없다. S3 는 additive 라 자기수리하지 않는다.

이 파일의 주장은 **보존 방향이 주**다: 파괴적 스텝은 "카운터 ≥ 1" 이 아니라
"정당한 입력을 보존하는가" 를 주장해야 한다. 명명 클래스의 정당한 deprecated 를
지우면 step_21 의 over-engineered 마킹 기능 자체가 무력화된다.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.graph_utils import is_anonymous_class_expression
from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_21c_anon_expr_deprecated_cleanup as step21c
from tools.quality_steps._base import StepContext

NS = DOMAIN_NS
ANON = URIRef(NS + "Union_ManufacturingProcessStep_18a9fee9")
NAMED = URIRef(NS + "HighStrengthProduct")
RESTRICTION = URIRef(NS + "ProductMaster_isProductOfProcess_someValuesFrom")
PARENT = URIRef(NS + "ManufacturingProcessStep")
OP = URIRef(NS + "someDeprecatedProperty")


def _ctx() -> StepContext:
    """step 이 쓰는 필드만 채운 최소 컨텍스트."""
    try:
        return StepContext(domain_ns=NS)
    except TypeError:
        ctx = StepContext.__new__(StepContext)
        object.__setattr__(ctx, "domain_ns", NS)
        return ctx


def _graph() -> Graph:
    """실측 상황 재현 — 익명 Union 본체 + 명명 클래스 + Restriction + OP."""
    g = Graph()
    # 익명 Union 표현식 (step_28 이 만들고 skolemize 됨) — 부모가 참조 중.
    g.add((ANON, RDF.type, OWL.Class))
    g.add((ANON, OWL.unionOf, URIRef(NS + "someList")))
    g.add((ANON, OWL.deprecated, Literal(True)))
    g.add((ANON, RDFS.seeAlso, Literal("over_engineered: no children, no usage")))
    g.add((PARENT, RDF.type, OWL.Class))
    g.add((PARENT, OWL.equivalentClass, ANON))
    # 명명 클래스의 **정당한** deprecated — 보존돼야 한다.
    g.add((NAMED, RDF.type, OWL.Class))
    g.add((NAMED, OWL.deprecated, Literal(True)))
    g.add((NAMED, RDFS.seeAlso, Literal("over_engineered: no children, no usage")))
    # skolemize 된 Restriction 본체 — 익명이므로 제거 대상.
    g.add((RESTRICTION, RDF.type, OWL.Restriction))
    g.add((RESTRICTION, OWL.onProperty, URIRef(NS + "isProductOfProcess")))
    g.add((RESTRICTION, OWL.deprecated, Literal(True)))
    # deprecated 프로퍼티 — 클래스가 아니므로 보존돼야 한다.
    g.add((OP, RDF.type, OWL.ObjectProperty))
    g.add((OP, OWL.deprecated, Literal(True)))
    return g


# ──────────────────────────────────────────────────────────────────
# 보존 방향 (주 주장)
# ──────────────────────────────────────────────────────────────────

def test_named_class_deprecated_is_preserved():
    """명명 클래스의 정당한 deprecated 를 지우면 step_21 기능이 무력화된다."""
    g = _graph()
    step21c.apply(g, _ctx())
    assert (NAMED, OWL.deprecated, Literal(True)) in g, (
        "명명 클래스의 deprecated 가 지워졌다 — over-engineered 마킹이 사라진다"
    )
    assert any(g.objects(NAMED, RDFS.seeAlso)), "보존 대상의 seeAlso 표지도 남아야 한다"


def test_deprecated_property_is_preserved():
    """OP/DP 의 deprecated 는 클래스 표현식이 아니므로 건드리지 않는다."""
    g = _graph()
    step21c.apply(g, _ctx())
    assert (OP, OWL.deprecated, Literal(True)) in g


def test_non_marker_seealso_is_preserved():
    """step_21 이 붙이지 않은 seeAlso 는 지우지 않는다."""
    g = _graph()
    manual = Literal("see the SME handbook p.12")
    g.add((ANON, RDFS.seeAlso, manual))
    step21c.apply(g, _ctx())
    assert (ANON, RDFS.seeAlso, manual) in g


def test_idempotent():
    """두 번 돌려도 추가 변경이 없다 (S3 는 여러 번 실행된다)."""
    g = _graph()
    step21c.apply(g, _ctx())
    snapshot = len(g)
    result = step21c.apply(g, _ctx())
    assert len(g) == snapshot
    assert result.stats["anon_expr_deprecated_cleaned"] == 0


# ──────────────────────────────────────────────────────────────────
# 제거 방향
# ──────────────────────────────────────────────────────────────────

def test_anon_union_deprecated_removed():
    """익명 Union 본체의 잔재는 표지까지 함께 제거된다."""
    g = _graph()
    result = step21c.apply(g, _ctx())
    assert (ANON, OWL.deprecated, Literal(True)) not in g
    assert not [
        o for o in g.objects(ANON, RDFS.seeAlso)
        if str(o).startswith("over_engineered:")
    ]
    assert result.stats["anon_expr_deprecated_cleaned"] == 2   # Union + Restriction
    assert result.stats["named_deprecated_preserved"] == 2     # NAMED + OP


def test_skolemized_restriction_deprecated_removed():
    g = _graph()
    step21c.apply(g, _ctx())
    assert (RESTRICTION, OWL.deprecated, Literal(True)) not in g


def test_union_body_itself_is_not_touched_otherwise():
    """제거는 deprecated/표지 한정 — 표현식 구조를 훼손하지 않는다."""
    g = _graph()
    step21c.apply(g, _ctx())
    assert (ANON, OWL.unionOf, URIRef(NS + "someList")) in g
    assert (PARENT, OWL.equivalentClass, ANON) in g


# ──────────────────────────────────────────────────────────────────
# 게이트가 실제로 조용해지는가 (산출물로 확인)
# ──────────────────────────────────────────────────────────────────

def test_quality_gate_no_longer_reports_anon_expr(tmp_path):
    """``_rule_deprecated_reference`` 가 익명 표현식을 high 로 올리지 않는다.

    이것이 baseline FAIL 의 직접 원인이었고, FAIL 이면 ``_caught_by`` 가
    이 체크로 어떤 mutant 도 세지 못한다.
    """
    from tools.mutation_runner import _classify_check_quality_rules
    from tools.validation_core import check_quality_rules

    g = _graph()
    # 정리 스텝을 **돌리지 않고** 검증기만 본다 — 두 층이 독립임을 고정.
    path = tmp_path / "t.ttl"
    g.serialize(destination=str(path), format="turtle")
    with patch(
        "tools.validation_core.GENERATED_TBOX_DIR",
        str(tmp_path),
    ):
        payload = json.loads(check_quality_rules(ttl_path=path.name))
    messages = [
        i.get("message", "") for i in payload.get("issues", [])
        if i.get("rule") == "deprecated_reference"
    ]
    assert not any("Union_" in m for m in messages), (
        f"익명 표현식이 여전히 deprecated_reference 로 보고된다: {messages}"
    )
    assert not any("_someValuesFrom" in m for m in messages), (
        f"skolemize 된 Restriction 이 여전히 보고된다: {messages}"
    )
    # 이 픽스처는 최소 그래프라 label/range 누락으로 다른 high 가 정당하게 남는다.
    # 여기서 고정하려는 것은 **deprecated_reference 가 high 에 기여하지 않는다** 는
    # 것뿐이다 (전체 등급이 아니다 — 그건 아래 배포본 회귀가 본다).
    assert not [
        i for i in payload.get("issues", [])
        if i.get("rule") == "deprecated_reference" and i.get("severity") == "high"
    ], "deprecated_reference 가 여전히 high 를 낸다 — baseline FAIL 고정의 원인"
    assert _classify_check_quality_rules(payload) in ("PASS", "WARN", "FAIL")


def test_deployed_tbox_quality_gate_is_not_fail():
    """배포 T-Box 에서 게이트가 FAIL 이 아니다 — mutation 검출의 필요조건.

    baseline 이 FAIL 이면 ``_caught_by`` 가 이 체크로 mutant 를 세지 못한다
    (강등만 검출로 계산). 수정 전 실측: high=1 → FAIL / 검출 1/7.
    수정 후: high=0 → WARN / 검출 3/7 (M5 annotation 2건이 새로 잡혔다).

    산출물이 없으면 skip — 이 테스트는 배포 상태에 대한 회귀다.
    """
    import os

    from tools.mutation_runner import _classify_check_quality_rules
    from tools.validation_core import check_quality_rules

    path = "data/generated/tbox/t_box.ttl"
    if not os.path.exists(path):
        pytest.skip("배포 T-Box 없음 — 파이프라인 미실행 환경")
    payload = json.loads(check_quality_rules(ttl_path="t_box.ttl"))
    verdict = _classify_check_quality_rules(payload)
    dep_high = [
        i for i in payload.get("issues", [])
        if i.get("rule") == "deprecated_reference" and i.get("severity") == "high"
    ]
    assert not dep_high, f"deprecated_reference high 재발: {dep_high}"
    assert verdict != "FAIL", (
        f"게이트가 baseline FAIL 로 돌아갔다 (high={payload.get('high')}) — "
        "이 상태에서는 mutation 검출률이 다시 죽는다"
    )


def test_helper_distinguishes_expression_from_property():
    """``is_anonymous_class_expression`` 은 프로퍼티를 표현식으로 오판하지 않는다.

    ``is_named_domain_class`` 를 필터로 쓰면 OP/DP 에 False 가 나와 **정당한
    deprecated 프로퍼티 지적까지 침묵** 한다 — 그래서 별도 헬퍼가 필요하다.
    """
    g = _graph()
    assert is_anonymous_class_expression(g, ANON, NS) is True
    assert is_anonymous_class_expression(g, RESTRICTION, NS) is True
    assert is_anonymous_class_expression(g, NAMED, NS) is False
    assert is_anonymous_class_expression(g, OP, NS) is False
    assert is_anonymous_class_expression(g, URIRef("http://other.example/X"), NS) is False


def test_step_registered_in_post_steps():
    """배선 확인 — 등록되지 않으면 산출물이 바뀌지 않는다 (배선 실패 이력)."""
    from tools.quality_steps import _POST_STEPS

    assert step21c.apply in _POST_STEPS
    from tools.quality_steps import (
        step_21b_dead_stub_prune,
        step_22_dup_op,
    )
    idx = _POST_STEPS.index(step21c.apply)
    assert _POST_STEPS.index(step_21b_dead_stub_prune.apply) < idx, "21b 뒤여야 한다"
    assert idx < _POST_STEPS.index(step_22_dup_op.apply), "게이트(22 계열) 앞이어야 한다"
