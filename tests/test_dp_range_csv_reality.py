"""선언된 DP ``rdfs:range`` 가 CSV 실값과 맞는가 (step_12i).

2026-08-30 실측. ``itemSupplierMapPriority`` 의 range 가 ``xsd:integer`` 인데
``Item_Supplier_Map.csv`` 의 ``Priority`` 실값은 ``Primary``/``Secondary`` 문자열이라
**98행 전량**이 A-Box 에서 조용히 사라졌다.

세 곳이 동시에 침묵했다:

1. ``_format_value`` 는 변환 실패 시 ``None`` → 트리플만 건너뛴다 (경고 없음)
2. ``column_coverage`` 는 이름 해석만 세므로 ``rate: 100.0``
3. ``property_completeness_detail`` 은 ``pct: 0.0`` 을 기록하지만 FAIL 축이 아니다

## 이 테스트의 방향

"위반이 0건" 만 주장하면 게이트를 껐을 때도 통과한다. 네 축을 고정한다:

* **검출** — 값과 어긋난 range 를 잡는다 (mutation)
* **무발화** — 정상 range 에는 발화하지 않는다 (263쌍 중 오탐 0)
* **교정** — 전량 손실은 ``xsd:string`` 으로 넓혀 데이터를 복구한다
* **보존** — 부분 손실은 건드리지 않는다 (진짜 데이터 오류를 숨기지 않는다)
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_12i_dp_range_csv_reality as step_12i
from tools.quality_steps._base import StepContext

DCTERMS = Namespace("http://purl.org/dc/terms/")
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")
NS = str(DOMAIN_NS)


def _ctx() -> StepContext:
    return StepContext(domain_ns=NS)


def _tbox() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    graph = Graph()
    graph.parse(str(TBOX), format="turtle")
    return graph


def _synthetic(column: str, class_name: str, xsd_range) -> Graph:
    """CSV 에 실재하는 (class, column) 을 가리키는 DP 1개만 있는 최소 그래프."""
    graph = Graph()
    prop = URIRef(NS + "probeDp")
    graph.add((prop, RDF.type, OWL.DatatypeProperty))
    graph.add((prop, RDFS.domain, URIRef(NS + class_name)))
    graph.add((prop, RDFS.range, xsd_range))
    graph.add((prop, DCTERMS.source, Literal(column)))
    return graph


# ── 검출: 값과 어긋난 range 를 잡는다 ──────────────────────────────────


def test_detects_string_values_declared_as_integer():
    """THE REGRESSION: 문자열 컬럼을 integer 로 선언하면 잡힌다."""
    graph = _synthetic("Priority", "ItemSupplierMap", XSD.integer)

    measured = step_12i._measure(graph, NS)

    assert measured["violation_count"] == 1, measured
    violation = measured["violations"][0]
    assert violation["fail_ratio"] == 1.0
    assert violation["declared_range"] == "integer"
    assert "Primary" in violation["sample_failed"]


def test_mutation_numeric_column_declared_boolean_is_caught():
    """정상 DP 의 range 를 망가뜨리면 검출되는가 — 게이트가 살아있음의 증거."""
    graph = _tbox()
    target = URIRef(NS + "blastFurnaceIronTempC")
    if (target, RDF.type, OWL.DatatypeProperty) not in graph:
        pytest.skip("대상 DP 없음 (T-Box 세대 차이)")
    graph.remove((target, RDFS.range, None))
    graph.add((target, RDFS.range, XSD.boolean))

    measured = step_12i._measure(graph, NS)

    hits = [v for v in measured["violations"] if v["dp"] == "blastFurnaceIronTempC"]
    assert hits, "숫자 컬럼을 boolean 으로 선언했는데 검출되지 않았다"
    assert hits[0]["fail_ratio"] == 1.0


# ── 무발화: 정상 선언에는 조용하다 (NEGATIVE 방향) ──────────────────────


def test_correct_range_produces_no_violation():
    """문자열 컬럼을 string 으로 선언하면 위반이 아니다."""
    graph = _synthetic("Priority", "ItemSupplierMap", XSD.string)

    assert step_12i._measure(graph, NS)["violation_count"] == 0


def test_numeric_column_declared_decimal_is_silent():
    graph = _synthetic("Unit_Price", "ItemSupplierMap", XSD.decimal)

    assert step_12i._measure(graph, NS)["violation_count"] == 0


def test_deployed_tbox_has_no_range_reality_violation():
    """배포 T-Box 전수 — 교정 후 위반이 남지 않는가.

    오탐이 있으면 이 검사가 실패한다 (263쌍 중 1건만 진짜였다).
    """
    graph = _tbox()

    result = step_12i.apply(graph, _ctx())
    measured = result.stats["dp_range_reality"]

    assert measured["blocking_count"] == 0, (
        f"교정 후에도 차단 위반이 남았다: {measured['blocking']}"
    )
    # 재측정으로 교정이 실제로 그래프에 반영됐는지 확인한다 (카운터 신뢰 금지).
    assert step_12i._measure(graph, NS)["violation_count"] == 0


# ── 교정: 전량 손실만 넓힌다 ────────────────────────────────────────────


def test_total_loss_range_is_widened_to_string():
    graph = _synthetic("Priority", "ItemSupplierMap", XSD.integer)

    step_12i.apply(graph, _ctx())

    prop = URIRef(NS + "probeDp")
    assert list(graph.objects(prop, RDFS.range)) == [XSD.string]


def test_widening_records_why_in_skos_note():
    """교정 근거를 산출물에 남긴다 — 지우면 다음 사람이 같은 추측을 반복한다."""
    from rdflib.namespace import SKOS

    graph = _synthetic("Priority", "ItemSupplierMap", XSD.integer)
    step_12i.apply(graph, _ctx())

    notes = [str(o) for o in graph.objects(URIRef(NS + "probeDp"), SKOS.note)]
    assert notes, "교정 근거 note 가 없다"
    assert "integer" in notes[0] and "Primary" in notes[0]


def test_autofix_can_be_disabled(monkeypatch):
    """``TBOX_DP_RANGE_REALITY_AUTOFIX=false`` 면 read-only 로 동작한다."""
    monkeypatch.setenv("TBOX_DP_RANGE_REALITY_AUTOFIX", "false")
    graph = _synthetic("Priority", "ItemSupplierMap", XSD.integer)

    result = step_12i.apply(graph, _ctx())

    assert list(graph.objects(URIRef(NS + "probeDp"), RDFS.range)) == [XSD.integer]
    assert result.stats["dp_range_reality"]["blocking_count"] == 1


# ── 보존: 부분 손실은 건드리지 않는다 (NEGATIVE 방향) ───────────────────


def test_partial_loss_is_reported_but_not_widened():
    """소수 이상치는 CSV 오류일 수 있다 — 조용히 넓히면 그 오류가 영구히 숨는다.

    ``Quality_Score`` 는 decimal 값인데 integer 로 선언하면 소수점 값만 실패한다.
    전량이 아니므로 교정 대상이 아니어야 한다.
    """
    graph = _synthetic("Quality_Score", "ItemSupplierMap", XSD.integer)

    result = step_12i.apply(graph, _ctx())
    measured = result.stats["dp_range_reality"]

    if not measured["violations"]:
        pytest.skip("이 컬럼에 부분 실패가 없다 (CSV 세대 차이)")
    ratio = measured["violations"][0]["fail_ratio"]
    if ratio >= 1.0:
        pytest.skip("전량 실패라 이 축의 픽스처가 아니다")
    assert not measured.get("autofixed"), "부분 손실을 교정했다 — 데이터 오류가 숨는다"
    assert list(graph.objects(URIRef(NS + "probeDp"), RDFS.range)) == [XSD.integer]


# ── 게이트 모드 ────────────────────────────────────────────────────────


def test_fail_mode_raises_on_uncorrected_violation(monkeypatch):
    """``fail`` 모드는 교정 불가 위반에서 RuntimeError 를 낸다."""
    monkeypatch.setenv("TBOX_DP_RANGE_REALITY_GATE", "fail")
    monkeypatch.setenv("TBOX_DP_RANGE_REALITY_AUTOFIX", "false")
    graph = _synthetic("Priority", "ItemSupplierMap", XSD.integer)

    with pytest.raises(RuntimeError, match="CSV 실값과 불일치"):
        step_12i.apply(graph, _ctx())


def test_missing_csv_is_noop(monkeypatch):
    """CSV/매핑이 없는 도메인-중립 배포에서는 조용히 skip 한다."""
    monkeypatch.setattr(step_12i, "_load_columns_with_values", lambda: {})
    graph = _synthetic("Priority", "ItemSupplierMap", XSD.integer)

    measured = step_12i._measure(graph, NS)

    assert measured["violations"] == []
    assert measured.get("skipped")
