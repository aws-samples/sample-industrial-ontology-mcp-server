"""Step 13d — OP functional 선언 대 tacit 팬아웃 감사.

## 왜 이 파일이 있나 (2026-09-05 실측)

S2 가 ObjectProperty 47개를 ``owl:FunctionalProperty`` 로 선언했는데 **S3 의 어떤
스텝도 그것을 데이터와 대조하지 않았다** (13 은 DP 전용, 13b·13c 는 cardinality
restriction 축). ``hasStackEquipment`` 는 tacit 이 팬아웃 8 로 11,160 트리플을 쓰는데
functional 로 선언돼 주어 1,560개 전부가 위반이었다.

## 이 파일이 주장하는 것

주장의 무게는 **보존** 쪽에 있다. "카운터가 1 이상" 은 이 리포에서 여러 번
무의미했으므로(mutation 대상의 우연으로 통과), 정당한 선언 42개를 **남기는지**와
판정 불가를 **위반으로 읽지 않는지**를 함께 못 박는다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_13d_op_functional_fanout_audit as step
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)


def D(name: str) -> URIRef:
    return URIRef(NS + name)


@pytest.fixture
def ctx() -> StepContext:
    return StepContext(domain_ns=NS)


@pytest.fixture
def tacit_dir(tmp_path, monkeypatch):
    """빈 tacit 폴더를 주입한다 — 배포 tacit 을 읽지 않도록.

    ``_tacit_dir()`` 이 호출 시점에 ``config.SOURCE_TACIT_DIR`` 을 보고, 캐시 서명에
    폴더 경로가 들어가므로 이 patch 로 캐시가 갈린다.
    """
    import config

    directory = tmp_path / "tacit"
    directory.mkdir()
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(directory))
    return directory


def _write_tacit(directory, triples: str) -> None:
    (directory / "t.ttl").write_text(
        f"@prefix steel: <{NS}> .\n"
        f"@prefix inst: <{NS.rstrip('#')}/instances#> .\n" + triples,
        encoding="utf-8",
    )


def _graph_with_functional_op(op_name: str = "hasThing") -> Graph:
    g = Graph()
    op = D(op_name)
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDF.type, OWL.FunctionalProperty))
    g.add((op, RDFS.domain, D("A")))
    g.add((op, RDFS.range, D("B")))
    return g


# ── 검출 ───────────────────────────────────────────────────────────────


def test_removes_declaration_contradicted_by_tacit(ctx, tacit_dir):
    """팬아웃 2 이상이면 Functional 선언이 거짓이다 → 제거."""
    _write_tacit(tacit_dir, """
        inst:a1 steel:hasThing inst:b1, inst:b2 .
    """)
    g = _graph_with_functional_op()
    result = step.apply(g, ctx)
    assert result.error is None
    assert result.stats["op_functional_contradicted"] == 1
    assert result.stats["op_functional_removed"] == 1
    assert (D("hasThing"), RDF.type, OWL.FunctionalProperty) not in g


def test_reports_the_measured_fanout(ctx, tacit_dir):
    """무엇을 근거로 지웠는지 응답에 남는다 (사람이 판단할 수 있어야 한다)."""
    _write_tacit(tacit_dir, """
        inst:a1 steel:hasThing inst:b1, inst:b2, inst:b3 .
        inst:a2 steel:hasThing inst:b1 .
    """)
    g = _graph_with_functional_op()
    detail = step.apply(g, ctx).stats["op_functional_contradicted_detail"]
    assert detail == [{
        "property": "hasThing",
        "tacit_subjects": 2,
        "tacit_triples": 4,
        "max_fanout": 3,
    }]


# ── PRESERVATION (이 파일의 무게중심) ──────────────────────────────────


def test_preserves_declaration_consistent_with_tacit(ctx, tacit_dir):
    """PRESERVATION: 팬아웃 1 이면 선언이 참이다 — 지우면 게이트를 잃는다."""
    _write_tacit(tacit_dir, """
        inst:a1 steel:hasThing inst:b1 .
        inst:a2 steel:hasThing inst:b2 .
    """)
    g = _graph_with_functional_op()
    result = step.apply(g, ctx)
    assert result.stats["op_functional_contradicted"] == 0
    assert result.stats["op_functional_measurable"] == 1
    assert (D("hasThing"), RDF.type, OWL.FunctionalProperty) in g


def test_preserves_when_op_absent_from_tacit(ctx, tacit_dir):
    """PRESERVATION: 판정 불가는 위반이 아니다.

    실측: 선언 43개 중 tacit 에 나타나는 것은 7개뿐이다. 부재를 위반으로 읽으면
    36개를 근거 없이 지운다.
    """
    _write_tacit(tacit_dir, "inst:a1 steel:otherProp inst:b1 .")
    g = _graph_with_functional_op()
    result = step.apply(g, ctx)
    assert result.stats["op_functional_undecidable"] == 1
    assert result.stats["op_functional_removed"] == 0
    assert (D("hasThing"), RDF.type, OWL.FunctionalProperty) in g


def test_only_the_functional_triple_is_removed(ctx, tacit_dir):
    """PRESERVATION: OP 선언·domain·range 는 건드리지 않는다."""
    _write_tacit(tacit_dir, "inst:a1 steel:hasThing inst:b1, inst:b2 .")
    g = _graph_with_functional_op()
    step.apply(g, ctx)
    assert (D("hasThing"), RDF.type, OWL.ObjectProperty) in g
    assert (D("hasThing"), RDFS.domain, D("A")) in g
    assert (D("hasThing"), RDFS.range, D("B")) in g
    assert len(g) == 3


def test_datatype_property_functional_is_untouched(ctx, tacit_dir):
    """PRESERVATION: DP 의 functional 은 step_13 이 A-Box 실측으로 판정한다.

    여기서 함께 지우면 두 판정기가 같은 축을 다투게 되고, tacit 은 DP 를 거의 쓰지
    않으므로 이 스텝의 근거로는 DP 를 판정할 수 없다.
    """
    _write_tacit(tacit_dir, 'inst:a1 steel:someValue "x", "y" .')
    g = Graph()
    dp = D("someValue")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDF.type, OWL.FunctionalProperty))
    result = step.apply(g, ctx)
    assert result.stats["op_functional_checked"] == 0
    assert (dp, RDF.type, OWL.FunctionalProperty) in g


def test_foreign_namespace_property_is_ignored(ctx, tacit_dir):
    """PRESERVATION: 외래 온톨로지 프로퍼티는 우리가 판정할 대상이 아니다."""
    foreign = URIRef("https://spec.industrialontologies.org/ontology/construct/hasP")
    _write_tacit(tacit_dir, "inst:a1 steel:hasThing inst:b1, inst:b2 .")
    g = Graph()
    g.add((foreign, RDF.type, OWL.ObjectProperty))
    g.add((foreign, RDF.type, OWL.FunctionalProperty))
    result = step.apply(g, ctx)
    assert result.stats["op_functional_checked"] == 0
    assert (foreign, RDF.type, OWL.FunctionalProperty) in g


def test_unreadable_tacit_removes_nothing(ctx, monkeypatch, tacit_dir):
    """PRESERVATION: 근거를 못 읽으면 아무것도 지우지 않는다."""
    def boom():
        raise OSError("tacit 폴더 없음")

    monkeypatch.setattr("domain.tbox_utils.load_tacit_graph", boom)
    g = _graph_with_functional_op()
    result = step.apply(g, ctx)
    assert result.stats["op_functional_removed"] == 0
    assert (D("hasThing"), RDF.type, OWL.FunctionalProperty) in g


# ── 모드 스위치 ────────────────────────────────────────────────────────


def test_warn_mode_measures_without_removing(ctx, tacit_dir, monkeypatch):
    monkeypatch.setenv("TBOX_OP_FUNCTIONAL_FANOUT", "warn")
    _write_tacit(tacit_dir, "inst:a1 steel:hasThing inst:b1, inst:b2 .")
    g = _graph_with_functional_op()
    result = step.apply(g, ctx)
    assert result.stats["op_functional_contradicted"] == 1
    assert result.stats["op_functional_removed"] == 0
    assert (D("hasThing"), RDF.type, OWL.FunctionalProperty) in g


def test_off_mode_is_noop(ctx, tacit_dir, monkeypatch):
    monkeypatch.setenv("TBOX_OP_FUNCTIONAL_FANOUT", "off")
    _write_tacit(tacit_dir, "inst:a1 steel:hasThing inst:b1, inst:b2 .")
    g = _graph_with_functional_op()
    result = step.apply(g, ctx)
    assert result.triples_delta == 0
    assert result.stats["op_functional_checked"] == 0
    assert (D("hasThing"), RDF.type, OWL.FunctionalProperty) in g


# ── 배선 ───────────────────────────────────────────────────────────────


def test_step_is_registered_after_13c():
    """등록되지 않은 스텝은 파이프라인에서 0회 실행된다."""
    from tools.quality_steps import _MAIN_POST_STEP9

    names = [getattr(fn, "__module__", "") for fn in _MAIN_POST_STEP9]
    assert any("step_13d_op_functional_fanout_audit" in n for n in names), names
    idx_13c = next(i for i, n in enumerate(names)
                   if "step_13c_op_max_cardinality_direction" in n)
    idx_13d = next(i for i, n in enumerate(names)
                   if "step_13d_op_functional_fanout_audit" in n)
    assert idx_13c < idx_13d


# ── 산출물 대조 ────────────────────────────────────────────────────────


def test_deployed_tbox_measurement_matches_record():
    """배포 T-Box 에 돌려 기록된 실측(모순 1개 = hasStackEquipment)과 대조한다.

    수치를 못 박는 것이 목적이 아니라 **판정 불가를 대량으로 지우지 않는지**를 본다 —
    모순 개수가 measurable 을 넘으면 판정 로직이 무너진 것이다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    declared_before = len(
        set(g.subjects(RDF.type, OWL.FunctionalProperty))
        & set(g.subjects(RDF.type, OWL.ObjectProperty)),
    )
    stats = step.apply(g, StepContext(domain_ns=NS)).stats
    assert stats["op_functional_checked"] == declared_before
    assert stats["op_functional_contradicted"] <= stats["op_functional_measurable"]
    # 정당한 선언을 대량으로 지우는 회귀를 잡는다 (실측 제거 1 / 선언 43).
    assert stats["op_functional_removed"] < stats["op_functional_checked"] / 2, stats
