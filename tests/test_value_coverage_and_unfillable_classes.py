"""값 커버리지 축 + 채워질 수 없는 클래스 진단 (rank 9 / rank 19).

## rank 9 — ``column_coverage`` 는 이름 해석만 센다

컬럼 → DP **이름 매칭**이 성공했는지만 보므로 매칭 후 값이 한 건도 안 실려도
1.0 이다. 실측 (2026-08-30): DP 7개가 ``property_completeness_detail`` 에서
``pct: 0.0`` 인데 ``column_coverage: 1.0`` 이었다.

그 7건은 **손실이 아니었다** — FK 컬럼이라 리터럴 DP 대신 관계 OP 로 실렸다
(``hasChemicalAnalysisProduct`` 150건 등 전수 확인). Path B 의 정상 동작이다.

문제는 이 지표가 **"OP 로 갔다" 와 "조용히 버려졌다" 를 구분하지 못한다**는 것이다.
둘 다 1.0 이므로 진짜 손실이 통과한다 — ``itemSupplierMapPriority`` 98행 전량
소실이 그 경로였다.

그래서 ``value_coverage`` 축을 신설했다: 각 (class, column) 을
``literal`` / ``relation`` / ``missing`` 으로 분류하고 ``missing`` 만 깎는다.

## rank 19 — 추론 후에도 빈 클래스 14개, 원인이 두 갈래

======================== ===== ==========================================
그룹                      개수   조치
======================== ===== ==========================================
``value_mismatch``         7   공리 값이 CSV 실값과 다름 → SME 가 어휘 확정
``no_definition``          7   판별 근거 자체가 없음 → 조건 추가 또는 재검토
======================== ===== ==========================================

일부는 표기만 다르다 (``HIGH``/``High``, ``Recycle``/``Recycling``, ``IN``/
``Inbound``). **그래도 자동 교정하지 않는다** — 값 어휘는 SME 소유이고, 공리를
데이터에 맞추는 것은 지표 매수다. 대신 근접 후보를 함께 제시해 판단 재료를 준다.

``class_instance_count`` check 는 원인을 구분하지 않아 "추상 클래스라 정상" 과
"값이 어긋나 영구히 빔" 을 같은 줄에 낸다 — 그래서 14건이 상수 노이즈였다.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from rdflib import OWL, RDF, Graph, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_31b_unfillable_class_audit as step_31b
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")
MANIFEST = pathlib.Path("data/generated/abox/abox_loss_manifest.json")


def _tbox() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    g = Graph()
    g.parse(str(TBOX), format="turtle")
    return g


# ── rank 9: value_coverage 축 ────────────────────────────────────────────


def test_manifest_exposes_value_coverage():
    """THE REGRESSION: 값 기준 축이 fidelity_score 에 함께 노출된다."""
    if not MANIFEST.exists():
        pytest.skip("loss manifest 없음")
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert "value_coverage" in m, "value_coverage 블록이 없다"
    assert "value_coverage" in m["fidelity_score"], (
        "fidelity_score 에 값 기준 축이 없다 — column_coverage 만 보면 "
        "이름 해석 성공이 데이터 적재로 오독된다"
    )


def test_value_coverage_classifies_three_ways():
    """literal / relation / missing 세 갈래가 모두 집계되는가."""
    if not MANIFEST.exists():
        pytest.skip("loss manifest 없음")
    vc = json.loads(MANIFEST.read_text(encoding="utf-8")).get("value_coverage")
    if not vc:
        pytest.skip("value_coverage 없음")

    for key in ("columns_checked", "loaded_as_literal",
                "loaded_as_relation", "not_loaded"):
        assert key in vc, key
    assert vc["columns_checked"] == (
        vc["loaded_as_literal"] + vc["loaded_as_relation"] + vc["not_loaded"]
    ), "분류 합이 검사 대상과 다르다"


def test_fk_columns_routed_to_relations_are_recorded():
    """``pct: 0.0`` DP 의 이유가 산출물에 남는가.

    이것이 없으면 "왜 이 DP 는 0% 인가" 를 사람이 매번 다시 조사한다.
    """
    if not MANIFEST.exists():
        pytest.skip("loss manifest 없음")
    vc = json.loads(MANIFEST.read_text(encoding="utf-8")).get("value_coverage")
    if not vc or not vc.get("loaded_as_relation"):
        pytest.skip("관계 라우팅 사례가 없다")

    items = vc["relation_routed_items"]
    assert items, "라우팅 목록이 비었다"
    for item in items:
        assert item["class"] and item["column"] and item["via_op"]


def test_deployed_abox_has_no_unloaded_column():
    """배포 산출물 — 리터럴도 관계도 없는 컬럼이 0건인가."""
    if not MANIFEST.exists():
        pytest.skip("loss manifest 없음")
    vc = json.loads(MANIFEST.read_text(encoding="utf-8")).get("value_coverage")
    if not vc:
        pytest.skip("value_coverage 없음")

    assert vc["not_loaded"] == 0, (
        f"실리지 않은 컬럼: {vc['not_loaded_items'][:5]}"
    )


def test_value_coverage_detects_missing_literals():
    """MUTATION: 리터럴을 지우면 ``not_loaded`` 로 잡히는가 — 게이트 생존 확인."""
    from tools.abox_generation import _compute_value_coverage, _parse_tbox

    master = pathlib.Path("data/generated/abox/master_data.ttl")
    if not (master.exists() and TBOX.exists()):
        pytest.skip("산출물 없음")
    info = _parse_tbox(TBOX.read_text(encoding="utf-8"))
    g = Graph()
    g.parse(str(master), format="turtle")

    cls = "WarehouseMaster"
    count = len(list(g.subjects(RDF.type, URIRef(NS + cls))))
    if count == 0:
        pytest.skip("대상 클래스 인스턴스 없음")
    per_class = {cls: count}
    coverage = {cls: {"mapped": 6, "unmapped": 0, "total": 6, "rate": 100.0,
                      "unmapped_columns": []}}

    baseline = _compute_value_coverage(g, per_class, info, coverage)
    target = URIRef(NS + "warehouseMasterLocation")
    removed = 0
    for s, p, o in list(g.triples((None, target, None))):
        g.remove((s, p, o))
        removed += 1
    if removed == 0:
        pytest.skip("대상 DP 리터럴이 없다")

    mutated = _compute_value_coverage(g, per_class, info, coverage)

    assert mutated["not_loaded"] > baseline["not_loaded"], (
        "리터럴을 지웠는데 not_loaded 가 늘지 않았다 — 게이트가 죽었다"
    )
    assert any(
        it["column"].lower() == "location" for it in mutated["not_loaded_items"]
    ), mutated["not_loaded_items"]


def test_relation_routing_is_not_counted_as_loss():
    """NEGATIVE 방향 — FK→관계 라우팅은 손실이 아니다.

    손실로 세면 정상 Path B 동작이 매번 위반으로 보고돼 게이트가 노이즈가 된다.
    """
    if not MANIFEST.exists():
        pytest.skip("loss manifest 없음")
    vc = json.loads(MANIFEST.read_text(encoding="utf-8")).get("value_coverage")
    if not vc:
        pytest.skip("value_coverage 없음")

    if vc["loaded_as_relation"] and vc["not_loaded"] == 0:
        assert vc["value_coverage"] == 1.0, (
            "관계 라우팅이 커버리지를 깎았다"
        )


# ── rank 19: 채워질 수 없는 클래스 ───────────────────────────────────────


def test_unfillable_audit_separates_causes():
    """THE REGRESSION: 원인을 구분한다 — 추상 클래스와 값 불일치는 다른 문제다."""
    g = _tbox()

    stats = step_31b.apply(g, StepContext(domain_ns=NS)).stats
    audit = stats["unfillable_class_audit"]
    if audit.get("skipped"):
        pytest.skip(f"판정 불가: {audit['skipped']}")

    assert "by_cause" in audit
    assert audit["unfillable_count"] == len(audit["unfillable"])
    for item in audit["unfillable"]:
        assert item["cause"] in (
            "value_mismatch", "no_definition", "evidence_unavailable",
        ), item


def test_value_mismatch_reports_csv_values_and_near_matches():
    """SME 가 판단할 재료를 준다 — 공리 값 + CSV 실값 + 근접 후보."""
    g = _tbox()

    audit = step_31b.apply(g, StepContext(domain_ns=NS)).stats[
        "unfillable_class_audit"]
    if audit.get("skipped"):
        pytest.skip("판정 불가")
    mismatches = [f for f in audit["unfillable"] if f["cause"] == "value_mismatch"]
    if not mismatches:
        pytest.skip("값 불일치 사례 없음")

    for item in mismatches:
        assert item["claimed_value"], item
        assert item["csv_values"], item
        assert item["on_property"], item
        assert item["claimed_value"] not in item["csv_values"], (
            f"{item['class']}: 공리 값이 CSV 에 있는데 불일치로 분류됐다"
        )


def test_near_match_finds_case_and_suffix_variants():
    """표기 차이를 근접 후보로 잡는가 — 대소문자를 무시해야 한다.

    처음엔 대소문자를 그대로 비교해 ``HIGH``/``High`` 를 놓쳤고, 근접 후보가
    비어 SME 가 "완전히 다른 값" 으로 오해할 상태였다.
    """
    g = _tbox()

    audit = step_31b.apply(g, StepContext(domain_ns=NS)).stats[
        "unfillable_class_audit"]
    if audit.get("skipped"):
        pytest.skip("판정 불가")
    by_class = {
        f["class"]: f for f in audit["unfillable"] if f["cause"] == "value_mismatch"
    }
    checked = 0
    for cls, expected in (
        ("HighSeverityAlarmEvent", "High"),
        ("InboundInventoryTransaction", "Inbound"),
        ("RecycledWasteManagement", "Recycling"),
    ):
        item = by_class.get(cls)
        if not item:
            continue
        checked += 1
        assert expected in item["near_matches"], (
            f"{cls}: 근접 후보에 {expected} 가 없다 — {item['near_matches']}"
        )
    if checked == 0:
        pytest.skip("대상 클래스가 없다 (T-Box 세대 차이)")


def test_no_definition_classes_are_reported_not_deleted():
    """판별 근거가 없는 클래스는 **보고만** 한다 — 지우면 계층이 무너진다."""
    g = _tbox()
    before = len(list(g.subjects(RDF.type, OWL.Class)))

    audit = step_31b.apply(g, StepContext(domain_ns=NS)).stats[
        "unfillable_class_audit"]
    after = len(list(g.subjects(RDF.type, OWL.Class)))

    assert after == before, "read-only 스텝이 클래스를 삭제했다"
    if audit.get("skipped"):
        pytest.skip("판정 불가")
    no_def = [f for f in audit["unfillable"] if f["cause"] == "no_definition"]
    for item in no_def:
        # 여전히 T-Box 에 있어야 한다.
        assert (URIRef(NS + item["class"]), RDF.type, OWL.Class) in g


def test_audit_excludes_skolem_expression_nodes():
    """``Union_*`` / ``*_someValuesFrom`` 은 클래스 열거 대상이 아니다.

    포함하면 익명 표현식이 "빈 클래스" 로 세어져 수치가 부풀고, 이 리포의
    '익명 표현식이 게이트를 죽였다' 와 같은 오염이 된다.
    """
    g = _tbox()

    audit = step_31b.apply(g, StepContext(domain_ns=NS)).stats[
        "unfillable_class_audit"]
    if audit.get("skipped"):
        pytest.skip("판정 불가")

    names = [f["class"] for f in audit["unfillable"]]
    assert not [n for n in names if n.startswith("Union_")], names
    assert not [n for n in names if "_someValuesFrom" in n], names


def test_audit_is_read_only():
    """T-Box 트리플이 하나도 변하지 않는가."""
    g = _tbox()
    before = len(g)

    result = step_31b.apply(g, StepContext(domain_ns=NS))

    assert len(g) == before
    assert result.triples_delta == 0


def test_fail_mode_raises_above_threshold(monkeypatch):
    """``fail`` 모드가 임계 초과에서 발화하는가."""
    monkeypatch.setenv("TBOX_UNFILLABLE_GATE", "fail")
    monkeypatch.setenv("TBOX_UNFILLABLE_MAX", "0")
    g = _tbox()

    audit_stats = step_31b._audit(g, NS)
    if audit_stats.get("skipped") or not audit_stats.get("unfillable_count"):
        pytest.skip("빈 클래스가 없어 이 축을 검사할 수 없다")

    with pytest.raises(RuntimeError, match="채워질 수 없는 클래스"):
        step_31b.apply(g, StepContext(domain_ns=NS))


def test_warn_mode_does_not_raise(monkeypatch):
    """기본 ``warn`` 은 파이프라인을 막지 않는다."""
    monkeypatch.setenv("TBOX_UNFILLABLE_GATE", "warn")
    monkeypatch.setenv("TBOX_UNFILLABLE_MAX", "0")
    g = _tbox()

    step_31b.apply(g, StepContext(domain_ns=NS))  # 예외 없음


def test_missing_inferred_graph_is_undecidable(monkeypatch):
    """추론 그래프가 없으면 **판정 불가** — 0 과 혼동하면 전부 미충족이 된다."""
    monkeypatch.setattr(step_31b, "_instance_counts", lambda _ns: None)
    g = _tbox()

    audit = step_31b._audit(g, NS)

    assert audit["skipped"] == "inferred_graph_unavailable"
    assert audit["unfillable_count"] == 0
