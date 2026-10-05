"""Step 12k — 한 CSV 컬럼을 여러 DP 가 주장하는 충돌 해소.

## 왜 이 파일이 있나 (2026-09-05 실측)

A-Box 생성기는 이 충돌을 만나면 authoritative source 매핑을 **양쪽 다 버리고** 이름
추측 폴백에 맡긴다 (``abox_generation`` 의 ``dp_by_source.pop``). 2026-08-29 에는 그
폴백이 실패해 FK 리터럴 3개가 0건이 됐다.

배포 T-Box 에 4쌍이 있었고 8개 DP **전부 S2 출력에 이미 존재**했다 — 생성 지점 가드로는
잡히지 않는 부류다.

## 이 파일이 주장하는 것

tie-break 가 **결정론적**이고 (사람이 예측 가능), 정당한 단일 주장을 건드리지 않고,
**domain 이 다른 같은 이름 컬럼을 충돌로 오판하지 않는다**. 마지막이 중요하다 — 여러
테이블이 ``Status`` 컬럼을 갖는 것은 정상이다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_12k_duplicate_source_claim_resolve as step
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)
DCTERMS = Namespace("http://purl.org/dc/terms/")


def D(name: str) -> URIRef:
    return URIRef(NS + name)


@pytest.fixture
def ctx() -> StepContext:
    return StepContext(domain_ns=NS)


def _dp(g: Graph, name: str, domain: str, source: str) -> URIRef:
    prop = D(name)
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDFS.domain, D(domain)))
    g.add((prop, DCTERMS.source, Literal(source)))
    return prop


# ── tie-break ──────────────────────────────────────────────────────────


def test_canonical_path_b_name_wins(ctx):
    """``{classCamelLower}{ColumnCamel}`` 이 있으면 그것이 정본이다.

    문서화된 작명 규약이라 사람이 예측할 수 있다. 실측 4쌍 중 3쌍이 여기서 갈린다.
    """
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    _dp(g, "productionPlanStatusText", "ProductionPlan", "Status")
    result = step.apply(g, ctx)
    assert result.error is None
    assert result.stats["dup_source_columns"] == 1
    assert result.stats["dup_source_resolved"][0]["winner"] == "productionPlanStatus"
    assert result.stats["dup_source_resolved"][0]["reason"] == "canonical"
    assert (D("productionPlanStatus"), DCTERMS.source, Literal("Status")) in g
    assert (D("productionPlanStatusText"), DCTERMS.source, Literal("Status")) not in g


def test_multiword_column_becomes_camel(ctx):
    """``Waste_Type`` → ``WasteType`` — 실측에서 이 쌍이 canonical 로 갈렸다."""
    g = Graph()
    _dp(g, "wasteManagementWasteType", "WasteManagement", "Waste_Type")
    _dp(g, "wasteManagementWasteTypeText", "WasteManagement", "Waste_Type")
    stats = step.apply(g, ctx).stats
    assert stats["dup_source_resolved"][0]["winner"] == "wasteManagementWasteType"


def test_shortest_wins_when_no_canonical(ctx):
    """canonical 형이 후보에 없으면 짧은 이름 — 군더더기 접미를 배제한다.

    실측 ``EquipmentStatus.Status``: canonical ``equipmentStatusStatus`` 는 없고
    ``equipmentStatusValue`` / ``equipmentStatusStatusText`` 가 다툰다.
    """
    g = Graph()
    _dp(g, "equipmentStatusValue", "EquipmentStatus", "Status")
    _dp(g, "equipmentStatusStatusText", "EquipmentStatus", "Status")
    stats = step.apply(g, ctx).stats
    assert stats["dup_source_resolved"][0]["winner"] == "equipmentStatusValue"
    assert stats["dup_source_resolved"][0]["reason"] == "shortest"


def test_tie_break_is_deterministic(ctx):
    """길이가 같으면 사전순 — 두 번 돌려 같은 답이 나와야 한다."""
    winners = set()
    for order in (("aaClassFoo", "aaClassBar"), ("aaClassBar", "aaClassFoo")):
        g = Graph()
        for name in order:
            _dp(g, name, "AaClass", "Zzz")
        winners.add(step.apply(g, ctx).stats["dup_source_resolved"][0]["winner"])
    assert winners == {"aaClassBar"}, winners


# ── PRESERVATION ───────────────────────────────────────────────────────


def test_single_claimant_is_untouched(ctx):
    """PRESERVATION: 충돌이 없으면 no-op (멱등)."""
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 0
    assert result.triples_delta == 0
    assert (D("productionPlanStatus"), DCTERMS.source, Literal("Status")) in g


def test_same_column_in_different_classes_is_not_a_conflict(ctx):
    """PRESERVATION: 여러 테이블이 ``Status`` 컬럼을 갖는 것은 정상이다.

    domain 을 키에 넣지 않으면 정당한 주장을 대량으로 떼어낸다 — 실측 배포 T-Box 에서
    ``Status`` 컬럼을 쓰는 클래스가 3개다.
    """
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    _dp(g, "purchaseOrderStatus", "PurchaseOrder", "Status")
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 0
    assert result.triples_delta == 0


def test_loser_dp_declaration_survives(ctx):
    """PRESERVATION: 주장만 뗀다 — DP 선언/domain 은 남긴다.

    유령 DP 정리는 step_12f / step_21b 의 축이다. 여기서 겹치면 두 판정이 섞인다.
    """
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    _dp(g, "productionPlanStatusText", "ProductionPlan", "Status")
    step.apply(g, ctx)
    loser = D("productionPlanStatusText")
    assert (loser, RDF.type, OWL.DatatypeProperty) in g
    assert (loser, RDFS.domain, D("ProductionPlan")) in g


def test_other_source_claims_of_loser_survive(ctx):
    """PRESERVATION: 패자가 **다른** 컬럼도 주장하면 그 주장은 남는다."""
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    loser = _dp(g, "productionPlanStatusText", "ProductionPlan", "Status")
    g.add((loser, DCTERMS.source, Literal("Remark")))
    step.apply(g, ctx)
    assert (loser, DCTERMS.source, Literal("Status")) not in g
    assert (loser, DCTERMS.source, Literal("Remark")) in g


def test_foreign_namespace_dp_is_ignored(ctx):
    """PRESERVATION: 외래 온톨로지 DP 의 주장은 우리가 판정할 대상이 아니다."""
    g = Graph()
    foreign = URIRef("https://spec.industrialontologies.org/ontology/construct/dpA")
    g.add((foreign, RDF.type, OWL.DatatypeProperty))
    g.add((foreign, RDFS.domain, D("ProductionPlan")))
    g.add((foreign, DCTERMS.source, Literal("Status")))
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 0
    assert (foreign, DCTERMS.source, Literal("Status")) in g


def test_domainless_dp_claim_is_not_grouped(ctx):
    """PRESERVATION: domain 없는 DP 는 클래스별 충돌로 셀 수 없다.

    A-Box 색인은 그런 DP 를 ``("", COL)`` 키로 따로 담는다 — 클래스 주장과 섞으면
    무관한 DP 의 주장을 떼어낸다.
    """
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    orphan = D("looseStatus")
    g.add((orphan, RDF.type, OWL.DatatypeProperty))
    g.add((orphan, DCTERMS.source, Literal("Status")))
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 0
    assert (orphan, DCTERMS.source, Literal("Status")) in g


def test_case_differences_in_column_are_one_claim(ctx):
    """``Status`` 와 ``STATUS`` 는 같은 컬럼이다 (A-Box 색인이 upper 로 정규화한다)."""
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    _dp(g, "productionPlanStatusText", "ProductionPlan", "STATUS")
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 1
    assert (D("productionPlanStatusText"), DCTERMS.source, Literal("STATUS")) not in g


# ── 모드 / 배선 ────────────────────────────────────────────────────────


def test_warn_mode_measures_without_modifying(ctx, monkeypatch):
    monkeypatch.setenv("TBOX_DUP_SOURCE_CLAIM", "warn")
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    _dp(g, "productionPlanStatusText", "ProductionPlan", "Status")
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 1
    assert result.stats["dup_source_claims_removed"] == 0
    assert result.triples_delta == 0


def test_off_mode_is_noop(ctx, monkeypatch):
    monkeypatch.setenv("TBOX_DUP_SOURCE_CLAIM", "off")
    g = Graph()
    _dp(g, "productionPlanStatus", "ProductionPlan", "Status")
    _dp(g, "productionPlanStatusText", "ProductionPlan", "Status")
    result = step.apply(g, ctx)
    assert result.stats["dup_source_columns"] == 0
    assert result.triples_delta == 0


def test_step_runs_after_12g_and_before_13():
    """12g(반대 방향) 뒤 · 13(카디널리티 생성) 앞."""
    from tools.quality_steps import _MAIN_POST_STEP9

    names = [getattr(fn, "__module__", "") for fn in _MAIN_POST_STEP9]
    idx = {}
    for i, n in enumerate(names):
        for tag in ("step_12g_dp_source_split",
                    "step_12k_duplicate_source_claim_resolve",
                    "step_13_pk_functional_someValuesFrom"):
            if tag in n:
                idx[tag] = i
    assert "step_12k_duplicate_source_claim_resolve" in idx, names
    assert idx["step_12g_dp_source_split"] < \
        idx["step_12k_duplicate_source_claim_resolve"]
    assert idx["step_12k_duplicate_source_claim_resolve"] < \
        idx["step_13_pk_functional_someValuesFrom"]


# ── 산출물 대조 ────────────────────────────────────────────────────────


def test_deployed_tbox_has_one_claimant_per_column():
    """산출물 확인 — 배포 T-Box 에 중복 주장이 남지 않았는가.

    남아 있으면 A-Box 생성기가 그 컬럼의 authoritative 매핑을 버린다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    stats = step.apply(g, StepContext(domain_ns=NS)).stats
    assert stats["dup_source_columns"] == 0, (
        f"중복 주장이 {stats['dup_source_columns']}건 남아 있다: "
        f"{stats.get('dup_source_resolved')}"
    )
