"""한 DP 가 여러 CSV 컬럼을 주장하는 것 방지 (Step 12e 감시 + 12g 정리).

배경 (2026-07-27 실측): ``dcterms:source`` 는 "이 DP 가 어느 컬럼에서 왔는가" 를
단일하게 지목해야 한다 (``04-property-rules.md`` 의 "DatatypeProperty source column"
절). 그런데 ``orderStdNoBOld`` 가 두 컬럼을 함께 주장했다:

    STD_NO_B_OLD    7,272행 채움  값 예: GRADE002     (구 체계)
    STD_NO_B_NEW  7,272행 채움  값 예: GRADE001   (신 체계)
    → 두 컬럼이 모두 채워진 7,272행 중 **값이 같은 행 0건** = 서로 다른 항목

A-Box 는 각 컬럼을 순회하며 같은 속성에 값을 넣으므로:

    값 트리플 14,544 > 인스턴스 약 1.2만   (초과 2,865)

이 속성으로 필터하면 두 체계의 표준번호가 섞여 나오고 COUNT 가 중복 계수된다.
형제 컬럼들(``STD_NO_A_OLD`` / ``_N``)은 이미 분리돼 있어 이 DP 만 예외였다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, XSD, Literal, Namespace, URIRef

from domain.tbox_utils import _new_graph
from tools.quality_steps import step_12e_dp_source_gate as gate
from tools.quality_steps import step_12g_dp_source_split as split

STEEL = "http://example.com/steel-ontology#"
DCTERMS = Namespace("http://purl.org/dc/terms/")


class _Ctx:
    domain_ns = STEEL


def _dp(g, name, domain, columns):
    u = URIRef(STEEL + name)
    g.add((u, RDF.type, OWL.DatatypeProperty))
    g.add((u, RDFS.domain, URIRef(STEEL + domain)))
    g.add((u, RDFS.range, XSD.string))
    for c in columns:
        g.add((u, DCTERMS.source, Literal(c)))
    return u


def _sources(g, name):
    return sorted(str(o) for o in g.objects(URIRef(STEEL + name), DCTERMS.source))


# --------------------------------------------------------------------------
# Step 12e — 감시 (측정만)
# --------------------------------------------------------------------------

def test_gate_detects_multi_column_dp():
    g = _new_graph()
    g.add((URIRef(STEEL + "Order"), RDF.type, OWL.Class))
    _dp(g, "orderStdNoBOld", "Order",
        ["STD_NO_B_NEW", "STD_NO_B_OLD"])
    stats = gate._measure(g, STEEL)
    assert stats["multi_column_dp_count"] == 1
    assert stats["multi_column_dps"]["orderStdNoBOld"] == [
        "STD_NO_B_NEW", "STD_NO_B_OLD"]


def test_gate_clean_when_single_column():
    g = _new_graph()
    g.add((URIRef(STEEL + "Order"), RDF.type, OWL.Class))
    _dp(g, "orderStdNoAOld", "Order", ["STD_NO_A_OLD"])
    _dp(g, "orderHotRollingStdNoANew", "Order", ["STD_NO_A_NEW"])
    assert gate._measure(g, STEEL)["multi_column_dp_count"] == 0


# --------------------------------------------------------------------------
# Step 12g — 정리
# --------------------------------------------------------------------------

def _pair_graph():
    """구/신 체계 컬럼 + 신 체계 전용 DP 가 이미 있는 상태."""
    g = _new_graph()
    g.add((URIRef(STEEL + "Order"), RDF.type, OWL.Class))
    _dp(g, "orderStdNoBOld", "Order",
        ["STD_NO_B_NEW", "STD_NO_B_OLD"])
    _dp(g, "orderPlateRollingStdNoBNew", "Order", ["STD_NO_B_NEW"])
    return g


def test_split_removes_claim_handled_by_dedicated_dp():
    """THE REGRESSION: 전용 DP 가 있는 컬럼 표기를 뺀다."""
    g = _pair_graph()
    stats = split.apply(g, _Ctx()).stats
    assert stats["source_claims_removed"] == 1
    assert _sources(g, "orderStdNoBOld") == ["STD_NO_B_OLD"]
    assert _sources(g, "orderPlateRollingStdNoBNew") == ["STD_NO_B_NEW"]


def test_split_resolves_gate_warning():
    g = _pair_graph()
    split.apply(g, _Ctx())
    assert gate._measure(g, STEEL)["multi_column_dp_count"] == 0


def test_split_is_idempotent():
    g = _pair_graph()
    split.apply(g, _Ctx())
    second = split.apply(g, _Ctx()).stats
    assert second["source_claims_removed"] == 0
    assert _sources(g, "orderStdNoBOld") == ["STD_NO_B_OLD"]


def test_split_keeps_column_without_dedicated_dp():
    """전용 DP 가 없으면 지우지 않는다 — 지우면 그 컬럼이 유실된다."""
    g = _new_graph()
    g.add((URIRef(STEEL + "Order"), RDF.type, OWL.Class))
    _dp(g, "orderSomething", "Order", ["COL_A", "COL_B"])
    stats = split.apply(g, _Ctx()).stats
    assert stats["source_claims_removed"] == 0
    assert stats["ambiguous_remaining"] == 1
    assert _sources(g, "orderSomething") == ["COL_A", "COL_B"]


def test_split_never_strips_all_sources():
    """모든 컬럼이 다른 DP 와 겹쳐도 최소 하나는 남긴다 (출처 소실 방지)."""
    g = _new_graph()
    g.add((URIRef(STEEL + "Order"), RDF.type, OWL.Class))
    _dp(g, "orderBoth", "Order", ["COL_A", "COL_B"])
    _dp(g, "orderA", "Order", ["COL_A"])
    _dp(g, "orderB", "Order", ["COL_B"])
    split.apply(g, _Ctx())
    assert len(_sources(g, "orderBoth")) >= 1


def test_split_leaves_single_column_dps_alone():
    g = _new_graph()
    g.add((URIRef(STEEL + "Order"), RDF.type, OWL.Class))
    _dp(g, "orderX", "Order", ["COL_X"])
    before = len(g)
    result = split.apply(g, _Ctx())
    assert result.triples_delta == 0
    assert len(g) == before


def test_split_registered_after_gate():
    """12e 측정 → 12g 정리 순서여야 로그가 정리 전 규모를 보여준다."""
    from tools.quality_steps import _MAIN_POST_STEP9
    mods = [getattr(f, "__module__", "") for f in _MAIN_POST_STEP9]
    gate_i = next((i for i, m in enumerate(mods) if "step_12e" in m), None)
    split_i = next((i for i, m in enumerate(mods) if "step_12g" in m), None)
    assert gate_i is not None and split_i is not None
    assert gate_i < split_i
