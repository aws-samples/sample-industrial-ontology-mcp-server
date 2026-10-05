"""OP 근거 기록 회귀 가드 — DP 는 99.6% 인데 OP 는 21% 였다.

## 배경 (실측)

| 축 | ``dcterms:source`` 보유 | 장치 |
|----|------------------------:|------|
| DP | 239/240 (99.6%)         | step_12d3 **역기록 스텝** + step_12e 게이트 |
| OP | 29/137 (21%)            | 없음 |

차이는 **역기록**이다. DP 는 ``step_12d3_dp_source_backfill`` 이 커버리지 판정
결과를 ``dcterms:source`` 로 되써서 99.6% 를 만든다. OP 에는 대응 스텝이 없다.

그런데 OP 를 만드는 세 스텝은 **근거를 이미 손에 들고 있다**:

- ``step_15b_fk_op_autocreate``: CSV 헤더 원문을 읽어 FK 를 판정한다
  (``headers = next(csv.reader(_cf))`` → ``normalized in fk_patterns``).
  그런데 ``fk_entries`` 에 ``(source_class, target)`` 만 담고 **header 를 버린다**.
- ``step_15_cross_domain_ops``: ``csv_fk_class_pairs()`` 결과로 게이트한다.
- ``step_15c_undeclared_op_backfill``: A-Box 관측 결과를 갖는다.

근거가 없는 게 아니라 **기록하지 않는 것**이다.

## 왜 중요한가

``step_22f_op_grounding_gate`` 는 근거 신호로 ``owl:onProperty`` 대상 여부를 보는데,
그 Restriction 은 ``step_13b`` 가 모든 단일 domain/range OP 에 자동으로 붙인 것이라
**OP 선언이 자기 근거가 되는 순환**이다. 실측: Restriction 111개 중 107개가
tautological. 그래서 phantom 76개가 무근거 6개로만 보고됐다.

``dcterms:source`` 는 순환하지 않는 근거다 — CSV 컬럼은 T-Box 밖에 있다.
"""
from __future__ import annotations

import csv
import os

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext

STEEL = "http://example.com/steel-ontology#"
DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")


@pytest.fixture
def fk_csv(tmp_path, monkeypatch):
    """FK 컬럼을 가진 최소 CSV 세트 — step_15b 가 스캔할 대상."""
    raw = tmp_path / "rawdata"
    raw.mkdir()
    with open(raw / "Shipment_Log.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Log_ID", "Equipment_ID", "Amount"])
        w.writerow(["L1", "EQ001", "10"])
    with open(raw / "Equipment_Master.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Equipment_ID", "Equipment_Name"])
        w.writerow(["EQ001", "furnace"])

    import config
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(raw))
    import tools.ontology_quality as oq
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(raw), raising=False)
    return str(raw)


def _graph_with_classes(*names: str) -> Graph:
    g = Graph()
    for n in names:
        g.add((URIRef(STEEL + n), RDF.type, OWL.Class))
    return g


def _ctx() -> StepContext:
    return StepContext(domain_ns=STEEL)


def _op_sources(g: Graph) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(STEEL)):
            continue
        out[str(op)[len(STEEL):]] = [str(s) for s in g.objects(op, DCTERMS_SOURCE)]
    return out


# ── step_15b: CSV 헤더를 근거로 기록 ─────────────────────────────────────

def test_fk_autocreated_op_records_the_csv_column(fk_csv, monkeypatch):
    """THE REGRESSION: FK 로 만든 OP 는 그 CSV 컬럼을 ``dcterms:source`` 로 남긴다.

    step_15b 는 ``Equipment_ID`` 헤더를 읽어 FK 를 판정하면서 그 문자열을
    버렸다 — 근거가 손에 있는데 기록만 안 했다.
    """
    from tools.quality_steps import step_15b_fk_op_autocreate as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda _c: set(), raising=False,
    )
    step.apply(g, _ctx())

    sources = _op_sources(g)
    created = {k: v for k, v in sources.items() if "Equipment" in k}
    assert created, f"FK OP 가 생성되지 않았다 (픽스처 문제): {sorted(sources)}"
    assert any(v for v in created.values()), (
        f"생성된 OP 에 dcterms:source 가 없다: {created}"
    )
    for name, srcs in created.items():
        if srcs:
            assert any("Equipment_ID" in s for s in srcs), (
                f"{name} 의 source 가 CSV 컬럼이 아니다: {srcs}"
            )


def test_recorded_source_matches_a_real_csv_header(fk_csv, monkeypatch):
    """기록된 source 는 **실재하는 CSV 헤더**여야 한다.

    없는 컬럼명을 적으면 근거가 있는 것처럼 보이면서 검산이 불가능해진다
    (실측: 배포 T-Box 의 OP source 28개 중 2개가 hallucinated column).
    """
    from tools.quality_steps import step_15b_fk_op_autocreate as step

    headers: set[str] = set()
    for fname in os.listdir(fk_csv):
        with open(os.path.join(fk_csv, fname), encoding="utf-8-sig") as fh:
            headers |= set(next(csv.reader(fh), []))

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda _c: set(), raising=False,
    )
    step.apply(g, _ctx())

    for name, srcs in _op_sources(g).items():
        for s in srcs:
            assert s in headers, (
                f"{name} 의 dcterms:source '{s}' 가 어떤 CSV 헤더에도 없다"
            )


def test_inverse_op_also_gets_the_source(fk_csv, monkeypatch):
    """역방향 OP 도 같은 근거를 갖는다 — 한쪽만 기록하면 근거율이 반토막난다."""
    from tools.quality_steps import step_15b_fk_op_autocreate as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda _c: set(), raising=False,
    )
    step.apply(g, _ctx())

    sources = _op_sources(g)
    pairs = {k: v for k, v in sources.items()
             if k.startswith("has") or k.startswith("is")}
    with_src = [k for k, v in pairs.items() if v]
    assert len(with_src) == len(pairs), (
        f"근거가 없는 OP 가 남았다: {sorted(set(pairs) - set(with_src))}"
    )


def test_existing_source_is_not_overwritten(fk_csv, monkeypatch):
    """이미 source 가 있으면 덮어쓰지 않는다 (SME/수동 기록 보존)."""
    from rdflib import Literal

    from tools.quality_steps import step_15b_fk_op_autocreate as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    op = URIRef(STEEL + "hasEquipmentMaster")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "ShipmentLog")))
    g.add((op, RDFS.range, URIRef(STEEL + "EquipmentMaster")))
    g.add((op, DCTERMS_SOURCE, Literal("SME_Verified_Column")))
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda _c: set(), raising=False,
    )
    step.apply(g, _ctx())

    srcs = [str(s) for s in g.objects(op, DCTERMS_SOURCE)]
    assert "SME_Verified_Column" in srcs, f"기존 근거가 사라졌다: {srcs}"


# ── RR 목표를 FK 상한으로 보정 ───────────────────────────────────────────

def test_rr_target_is_capped_by_fk_evidence(monkeypatch):
    """THE REGRESSION: RR 목표가 데이터로 도달 가능한 상한을 넘지 않는다.

    실측 (2026-08-18): DP 239 / CSV FK 쌍 46 인 이 도메인에서
      - RR ≥ 0.3 은 OP **102개** 를 요구한다
      - FK 근거로 만들 수 있는 최대는 정/역 합쳐 **92개**
    즉 목표 자체가 근거 없는 OP 10개 이상을 만들어야만 달성 가능하다.
    ``⚠️ OP가 부족합니다. 크로스 도메인 ObjectProperty를 추가하세요.`` 라는
    피드백이 매 라운드 Architect 를 근거 없는 OP 생성으로 밀었다.
    """
    import tools.multi_agent_tbox as mt

    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {(f"a{i}", f"b{i}")
                                                     for i in range(46)})
    # OP 40 / DP 239 → RR 0.143. 순진한 목표(0.3)는 미달이지만 FK 상한
    # (92/(92+239)=0.278) 기준으로는 "더 만들 수 있다" 가 맞다.
    ttl_lines = [
        "@prefix steel: <http://example.com/steel-ontology#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
    ]
    for i in range(40):
        ttl_lines.append(f"steel:op{i} a owl:ObjectProperty .")
    for i in range(239):
        ttl_lines.append(f"steel:dp{i} a owl:DatatypeProperty .")
    feedback = mt._compute_metrics_feedback("\n".join(ttl_lines) + "\n", 1)

    assert "FK" in feedback or "상한" in feedback, (
        f"FK 상한이 피드백에 없다 — Architect 가 도달 불가 목표를 본다:\n{feedback}"
    )


def test_displayed_rr_target_never_exceeds_the_ceiling(monkeypatch):
    """표시되는 목표값 자체가 상한을 넘지 않는다.

    상한을 **별도 줄로 보여주기만** 하면 목표 줄에는 여전히 도달 불가한 0.3 이
    남는다 — Architect 는 목표 줄을 보고 움직인다. 실측으로 이 mutant
    (``rr_target = 0.3`` 고정) 가 생존했다.
    """
    import re

    import tools.multi_agent_tbox as mt

    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {(f"a{i}", f"b{i}")
                                                     for i in range(5)})
    lines = [
        "@prefix steel: <http://example.com/steel-ontology#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
    ]
    for i in range(8):          # 상한 10 에 미달 → 목표 줄이 표시된다
        lines.append(f"steel:op{i} a owl:ObjectProperty .")
    for i in range(100):
        lines.append(f"steel:dp{i} a owl:DatatypeProperty .")
    feedback = mt._compute_metrics_feedback("\n".join(lines) + "\n", 1)

    shown = re.search(r"목표:\s*≥([0-9.]+)", feedback)
    assert shown, f"RR 목표가 표시되지 않았다:\n{feedback}"
    target = float(shown.group(1))
    ceiling = 10 / 110          # FK 쌍 5 × 2 / (10 + DP 100)
    assert target <= round(ceiling, 2) + 1e-9, (
        f"표시 목표 {target} 가 데이터 상한 {ceiling:.2f} 를 넘는다 — "
        f"Architect 가 근거 없는 OP 를 만들어야 달성된다:\n{feedback}"
    )


def test_rr_warning_suppressed_when_at_fk_ceiling(monkeypatch):
    """FK 상한에 도달했으면 'OP 가 부족하다' 고 하지 않는다.

    이 방향을 주장하지 않으면 상한만 표시하고 압박 문구는 그대로 남는다 —
    실측 손상(근거 없는 OP 생성)이 계속된다.
    """
    import tools.multi_agent_tbox as mt

    # FK 쌍 5개 → 상한 10 OP. 현재 OP 10 이면 더 만들 근거가 없다.
    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {(f"a{i}", f"b{i}")
                                                     for i in range(5)})
    lines = [
        "@prefix steel: <http://example.com/steel-ontology#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
    ]
    for i in range(10):
        lines.append(f"steel:op{i} a owl:ObjectProperty .")
    for i in range(100):
        lines.append(f"steel:dp{i} a owl:DatatypeProperty .")
    feedback = mt._compute_metrics_feedback("\n".join(lines) + "\n", 1)

    assert "OP가 부족합니다" not in feedback, (
        f"FK 상한에 도달했는데 OP 추가를 압박한다:\n{feedback}"
    )


def test_rr_warning_kept_when_below_fk_ceiling(monkeypatch):
    """NEGATIVE: 상한 아래면 경고를 유지한다 (게이트를 끄는 변경이 아니다)."""
    import tools.multi_agent_tbox as mt

    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {(f"a{i}", f"b{i}")
                                                     for i in range(50)})
    lines = [
        "@prefix steel: <http://example.com/steel-ontology#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
    ]
    for i in range(5):          # OP 5 — 상한 100 에 한참 못 미친다
        lines.append(f"steel:op{i} a owl:ObjectProperty .")
    for i in range(100):
        lines.append(f"steel:dp{i} a owl:DatatypeProperty .")
    feedback = mt._compute_metrics_feedback("\n".join(lines) + "\n", 1)

    assert "OP" in feedback and ("부족" in feedback or "추가" in feedback), (
        f"상한 아래인데 경고가 사라졌다:\n{feedback}"
    )


# ── 역기록 스텝: 기존 무근거 OP 를 채운다 ─────────────────────────────────
#
# step_15b 수정만으로는 **앞으로 만들 OP** 에만 적용된다 — 실측: 현행 T-Box 에서
# step_15b 의 fk_ops_autocreated=0 (모든 FK OP 가 이미 있다), 근거율 21.2% 불변.
# 기존 108개를 채우려면 DP 쪽 step_12d3 처럼 **역기록 스텝** 이 필요하다.
#
# 원칙은 step_12d3 와 같다: **추측이 아니라 이미 내린 판정의 전사**.
# ``csv_fk_pair_columns()`` 가 "이 (domain,range) 관계는 이 CSV 컬럼에서 왔다" 를
# 이미 안다 — 기록만 안 하고 있었다.

def test_backfill_fills_existing_ungrounded_op(fk_csv, monkeypatch):
    """THE REGRESSION: 기존 OP 의 (domain,range) 로 FK 컬럼을 역기록한다."""
    from tools.quality_steps import step_15d_op_source_backfill as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    op = URIRef(STEEL + "shipmentUsesEquipment")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "ShipmentLog")))
    g.add((op, RDFS.range, URIRef(STEEL + "EquipmentMaster")))

    step.apply(g, _ctx())

    srcs = [str(s) for s in g.objects(op, DCTERMS_SOURCE)]
    assert srcs == ["Equipment_ID"], f"역기록되지 않았다: {srcs}"


def test_backfill_does_not_touch_ops_without_fk_evidence(fk_csv, monkeypatch):
    """NEGATIVE: FK 근거가 없는 (domain,range) 는 비워 둔다.

    근거 없는 OP 에 아무 컬럼이나 적으면 근거율만 올라가고 **근거는 없다** —
    지표를 매수하는 것이라 게이트가 무의미해진다.
    """
    from tools.quality_steps import step_15d_op_source_backfill as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster", "Unrelated")
    op = URIRef(STEEL + "shipmentToUnrelated")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "ShipmentLog")))
    g.add((op, RDFS.range, URIRef(STEEL + "Unrelated")))

    step.apply(g, _ctx())

    assert list(g.objects(op, DCTERMS_SOURCE)) == [], (
        "FK 근거가 없는 OP 에 출처를 적었다 — 지표 매수"
    )


def test_backfill_preserves_existing_source(fk_csv, monkeypatch):
    """NEGATIVE: 기존 출처를 덮어쓰지 않는다."""
    from rdflib import Literal

    from tools.quality_steps import step_15d_op_source_backfill as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    op = URIRef(STEEL + "shipmentUsesEquipment")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "ShipmentLog")))
    g.add((op, RDFS.range, URIRef(STEEL + "EquipmentMaster")))
    g.add((op, DCTERMS_SOURCE, Literal("SME_Column")))

    step.apply(g, _ctx())

    assert [str(s) for s in g.objects(op, DCTERMS_SOURCE)] == ["SME_Column"]


def test_backfill_replaces_the_none_sentinel(fk_csv, monkeypatch):
    """``none:`` ("근거 없음" 명시) 는 실제 FK 근거를 찾으면 교체한다.

    보존하면 근거가 있는데도 영구히 "없음" 으로 남고 근거율 집계에서도 빠진다.
    교체 시 기존 트리플을 지워야 한다 — 남기면 한 OP 가 "있음/없음" 을 동시에
    주장해 A-Box 출처 인덱스가 비결정적이 된다.
    """
    from rdflib import Literal

    from tools.quality_steps import step_15d_op_source_backfill as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    op = URIRef(STEEL + "shipmentUsesEquipment")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "ShipmentLog")))
    g.add((op, RDFS.range, URIRef(STEEL + "EquipmentMaster")))
    g.add((op, DCTERMS_SOURCE, Literal("none:역방향 추론")))

    step.apply(g, _ctx())

    srcs = [str(s) for s in g.objects(op, DCTERMS_SOURCE)]
    assert srcs == ["Equipment_ID"], f"none: 센티넬이 교체되지 않았다: {srcs}"


def test_none_sentinel_does_not_count_as_grounded(monkeypatch):
    """``none:`` 을 근거율에 세면 지표가 부풀린다 (step_12e 규약과 동일)."""
    import tools.multi_agent_tbox as mt

    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {("a", "b")})
    ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
steel:opReal a owl:ObjectProperty ; dcterms:source "Equipment_ID" .
steel:opNone a owl:ObjectProperty ; dcterms:source "none:역방향 추론" .
steel:dp1 a owl:DatatypeProperty .
"""
    feedback = mt._compute_metrics_feedback(ttl, 1)
    assert "1/2" in feedback, (
        f"none: 를 근거로 세어 근거율이 부풀었다:\n{feedback}"
    )


def test_backfill_is_idempotent(fk_csv, monkeypatch):
    """두 번 돌려도 같은 결과 — 두 번째는 아무것도 하지 않는다."""
    from tools.quality_steps import step_15d_op_source_backfill as step

    g = _graph_with_classes("ShipmentLog", "EquipmentMaster")
    op = URIRef(STEEL + "shipmentUsesEquipment")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "ShipmentLog")))
    g.add((op, RDFS.range, URIRef(STEEL + "EquipmentMaster")))

    first = step.apply(g, _ctx()).stats
    snapshot = set(g)
    second = step.apply(g, _ctx()).stats

    assert first["op_source_backfilled"] >= 1
    assert second["op_source_backfilled"] == 0
    assert set(g) == snapshot


def test_backfill_skips_when_csv_unreadable(monkeypatch):
    """CSV 판정 불가면 아무것도 하지 않는다 (None 규약)."""
    from tools.quality_steps import step_15d_op_source_backfill as step

    monkeypatch.setattr(
        "domain.graph_utils.csv_fk_pair_columns", lambda: None, raising=False,
    )
    g = _graph_with_classes("A", "B")
    op = URIRef(STEEL + "aToB")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, URIRef(STEEL + "A")))
    g.add((op, RDFS.range, URIRef(STEEL + "B")))

    result = step.apply(g, _ctx())

    assert result.stats["op_source_backfilled"] == 0
    assert list(g.objects(op, DCTERMS_SOURCE)) == []


def test_backfill_is_registered_in_the_pipeline():
    """스텝이 파이프라인에 등록돼 있고 OP 생성 스텝들 **뒤**에 온다.

    앞에 두면 15/15b/15c 가 방금 만든 OP 를 놓쳐 근거율이 낮게 남는다 — 등록만
    하고 순서를 틀리면 조용한 부분 실패가 된다.
    """
    from tools.quality_steps import _MAIN_POST_STEP9, step_15d_op_source_backfill

    assert step_15d_op_source_backfill.apply in _MAIN_POST_STEP9
    names = [f.__module__ for f in _MAIN_POST_STEP9]
    idx = names.index("tools.quality_steps.step_15d_op_source_backfill")
    for producer in (
        "tools.quality_steps.step_15_cross_domain_ops",
        "tools.quality_steps.step_15b_fk_op_autocreate",
        "tools.quality_steps.step_15c_undeclared_op_backfill",
    ):
        assert idx > names.index(producer), (
            f"{producer} 보다 앞에 있어 그 스텝이 만든 OP 를 못 채운다"
        )


def test_op_grounding_rate_is_reported(monkeypatch):
    """OP 근거율이 라운드 피드백에 노출된다.

    개수 목표만 보여주면 Architect·리뷰어의 관심이 개수에 머문다. 근거율(현행
    21%)을 노출하면 지적 대상이 개수에서 근거로 옮겨간다.
    """
    import tools.multi_agent_tbox as mt

    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {("a", "b")})
    ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
steel:opA a owl:ObjectProperty ; dcterms:source "Equipment_ID" .
steel:opB a owl:ObjectProperty .
steel:dp1 a owl:DatatypeProperty .
"""
    feedback = mt._compute_metrics_feedback(ttl, 1)
    assert "근거" in feedback, f"근거율이 피드백에 없다:\n{feedback}"
    assert "1/2" in feedback or "50" in feedback, (
        f"근거율 수치가 보이지 않는다:\n{feedback}"
    )
