"""step_21 이 자식을 얻은 클래스의 deprecated 마킹을 **되살린다**.

2026-08-22 실측 (S3 를 같은 T-Box 에 두 번 돌린 산출물):

    check_quality_rules → critical=0 high=5
      Deprecated entity EnergyConsumption is still referenced by:
        ElectricalConsumption, FuelConsumption, GasEnergy, SteamEnergy
      (+ EnergyInfrastructure / AtmosphericMonitoring /
         WasteNoiseMonitoring / LandWaterMonitoring)

step_21 이 1회차에서 "자식·용처 없음" 으로 마킹한 5개 클래스에 step_12/14 가
2회차에서 자식을 붙였다. 그런데 step_21 은 **마킹만 하고 해제하지 않아서**
``deprecated`` 가 남았고, ``_rule_deprecated_reference`` 가 그것을 high 로 보고해
``check_quality_rules`` 가 **baseline FAIL** 로 고정됐다 — 그러면
``mutation_runner._caught_by`` 가 PASS→FAIL 강등만 세므로 이 체크로는 어떤
mutant 도 검출되지 않는다 (S4.5 검출률이 죽는다. 커밋 71a08fc 의 익명 표현식
사례와 **같은 실패 모드**, 다른 원인).

이것은 내 변경이 만든 것이 아니라 **선재 비멱등성** 이다: 원본 0b 로 같은 입력에
S3 를 돌려도 동일한 high 5건이 나왔다 (실측 대조 확인).

## 이 파일의 주장

- **되살림**: 자식/용처를 얻은 클래스는 마킹이 해제된다 (표지 seeAlso 도 함께).
- **보존 (주 방향)**: 여전히 고립된 클래스의 마킹은 유지된다 — 해제가 과잉이면
  step_21 의 over-engineered 탐지 기능 자체가 무의미해진다.
- **보존**: 사람이 손으로 넣은 ``owl:deprecated`` (``over_engineered:`` 표지가
  없는 것) 는 건드리지 않는다.
- **멱등**: 두 번 돌려도 추가 변경이 없다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_21_over_engineered_dep as step21
from tools.quality_steps._base import StepContext

NS = DOMAIN_NS
MARKER = Literal("over_engineered: no children, no usage")


def _ctx() -> StepContext:
    try:
        return StepContext(domain_ns=NS)
    except TypeError:
        ctx = StepContext.__new__(StepContext)
        object.__setattr__(ctx, "domain_ns", NS)
        return ctx


def _cls(g: Graph, name: str) -> URIRef:
    u = URIRef(NS + name)
    g.add((u, RDF.type, OWL.Class))
    return u


def test_class_that_gained_children_is_undeprecated():
    """실측 사례 재현 — 자식을 얻었으면 마킹을 해제한다."""
    g = Graph()
    parent = _cls(g, "EnergyConsumption")
    child = _cls(g, "SteamEnergy")
    g.add((parent, OWL.deprecated, Literal(True)))
    g.add((parent, RDFS.seeAlso, MARKER))
    g.add((child, RDFS.subClassOf, parent))          # 2회차에 붙은 자식

    result = step21.apply(g, _ctx())
    assert (parent, OWL.deprecated, Literal(True)) not in g
    assert MARKER not in set(g.objects(parent, RDFS.seeAlso))
    assert result.stats["over_engineered_undeprecated"] == 1


def test_class_used_as_domain_is_undeprecated():
    """자식이 아니라 domain/range 로 쓰이게 된 경우도 해제한다."""
    g = Graph()
    used = _cls(g, "SomeClass")
    g.add((used, OWL.deprecated, Literal(True)))
    g.add((used, RDFS.seeAlso, MARKER))
    op = URIRef(NS + "someOp")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, used))

    step21.apply(g, _ctx())
    assert (used, OWL.deprecated, Literal(True)) not in g


# ──────────────────────────────────────────────────────────────────
# 보존 방향 (주 주장 — 해제가 과잉이면 기능이 무의미해진다)
# ──────────────────────────────────────────────────────────────────

def test_still_isolated_class_stays_deprecated():
    """자식도 용처도 없는 클래스는 계속 deprecated 여야 한다."""
    g = Graph()
    lonely = _cls(g, "StillLonely")
    g.add((lonely, OWL.deprecated, Literal(True)))
    g.add((lonely, RDFS.seeAlso, MARKER))

    result = step21.apply(g, _ctx())
    assert (lonely, OWL.deprecated, Literal(True)) in g, (
        "고립 클래스의 마킹을 지우면 over-engineered 탐지가 무의미해진다"
    )
    assert result.stats["over_engineered_undeprecated"] == 0


def test_manual_deprecation_is_preserved():
    """사람이 손으로 넣은 deprecated 는 표지가 없으므로 건드리지 않는다."""
    g = Graph()
    manual = _cls(g, "ManuallyDeprecated")
    child = _cls(g, "SomeChild")
    g.add((manual, OWL.deprecated, Literal(True)))
    g.add((manual, RDFS.seeAlso, Literal("SME 결정: 2026-Q3 폐기 예정")))
    g.add((child, RDFS.subClassOf, manual))

    result = step21.apply(g, _ctx())
    assert (manual, OWL.deprecated, Literal(True)) in g, (
        "step_21 이 자기가 붙이지 않은 마킹을 지웠다"
    )
    assert result.stats["over_engineered_undeprecated"] == 0


def test_marking_still_happens_for_new_orphans():
    """되살림 로직이 신규 마킹을 막지 않는다."""
    g = Graph()
    _cls(g, "BrandNewOrphan")
    result = step21.apply(g, _ctx())
    assert result.stats["over_engineered_deprecated"] == 1
    assert (URIRef(NS + "BrandNewOrphan"), OWL.deprecated, Literal(True)) in g


def test_idempotent():
    """두 번 돌려도 추가 변경이 없다 (S3 는 여러 번 실행된다)."""
    g = Graph()
    parent = _cls(g, "EnergyConsumption")
    child = _cls(g, "SteamEnergy")
    g.add((parent, OWL.deprecated, Literal(True)))
    g.add((parent, RDFS.seeAlso, MARKER))
    g.add((child, RDFS.subClassOf, parent))

    step21.apply(g, _ctx())
    size = len(g)
    r2 = step21.apply(g, _ctx())
    assert len(g) == size
    assert r2.stats["over_engineered_undeprecated"] == 0


# ──────────────────────────────────────────────────────────────────
# 산출물 회귀 — 배포 T-Box 에서 게이트가 FAIL 이 아니다
# ──────────────────────────────────────────────────────────────────

def test_deployed_tbox_has_no_deprecated_reference_high():
    """배포 T-Box 에 ``deprecated_reference`` high 가 없다.

    있으면 ``check_quality_rules`` 가 baseline FAIL 이 되어 S4.5 mutation
    검출률이 죽는다 (실측: 14.3% → 57.1% 로 회복한 것을 되돌린다).
    """
    import json
    import os

    from tools.validation_core import check_quality_rules

    path = "data/generated/tbox/t_box.ttl"
    if not os.path.exists(path):
        pytest.skip("배포 T-Box 없음")
    payload = json.loads(check_quality_rules(ttl_path="t_box.ttl"))
    offenders = [
        i for i in payload.get("issues", [])
        if i.get("rule") == "deprecated_reference" and i.get("severity") == "high"
    ]
    assert not offenders, f"deprecated_reference high 재발: {offenders}"
