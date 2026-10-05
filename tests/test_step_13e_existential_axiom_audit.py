"""Step 13e — 기존 ``someValuesFrom`` 공리의 사후 근거 감사.

## 왜 이 파일이 있나 (2026-09-05 실측)

``step_13`` 의 근거 게이트는 **자기 생성분만** 지킨다. 배포 T-Box 의 필수참여 위반
3쌍 / 개체 607건 중 근거 없는 2건이 **S2 출력 스냅샷에 이미 있었다** — 즉 생성 게이트를
아무리 고쳐도 잡히지 않는 경로였다.

## 이 파일이 주장하는 것

1. **오탐 방지가 최우선**: CSV 판정기만 쓰면 ``hasStackEquipment`` 를 지운다 (tacit 이
   1,560/1,560 을 채워 실제로는 위반이 아니다). tacit 축이 살아 있는지 못 박는다 —
   이 리포는 가드가 산 데이터를 억제한 사고를 겪었다.
2. **판정기 공유**: 생성과 감사가 사본을 쓰면 두 방향이 갈린다.
3. **판정 불가 보존**: 근거를 확인할 수 없는 것은 위반이 아니다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_13e_existential_axiom_audit as step
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)
_JUDGE = "tools.quality_steps.step_13_pk_functional_someValuesFrom._fk_evidence_for_op"
_CSV = "tools.quality_steps.step_13_pk_functional_someValuesFrom._csv_rows_for_class"


def D(name: str) -> URIRef:
    return URIRef(NS + name)


@pytest.fixture
def ctx() -> StepContext:
    return StepContext(domain_ns=NS)


@pytest.fixture
def tacit_dir(tmp_path, monkeypatch):
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


def _graph_with_axiom(node=None, *, op="hasThing", owner="Owner", filler="Filler"):
    """``owner ⊑ ∃op.filler`` 한 건을 가진 그래프."""
    g = Graph()
    restriction = node if node is not None else BNode()
    g.add((D(op), RDF.type, OWL.ObjectProperty))
    g.add((D(owner), RDF.type, OWL.Class))
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, D(op)))
    g.add((restriction, OWL.someValuesFrom, D(filler)))
    g.add((D(owner), RDFS.subClassOf, restriction))
    return g, restriction


def _stub_judge(monkeypatch, *, has: bool, ratio: float, reason: str = "stub"):
    monkeypatch.setattr(_JUDGE, lambda *a, **k: (has, ratio, reason))


# ── 검출 ───────────────────────────────────────────────────────────────


def test_prunes_axiom_with_no_evidence(ctx, tacit_dir, monkeypatch):
    _stub_judge(monkeypatch, has=False, ratio=0.0, reason="no_column")
    g, restriction = _graph_with_axiom()
    result = step.apply(g, ctx)
    assert result.error is None
    assert result.stats["existential_axioms_pruned"] == 1
    assert result.stats["existential_unsupported_no_evidence"] == 1
    assert (D("Owner"), RDFS.subClassOf, restriction) not in g
    assert (restriction, OWL.someValuesFrom, D("Filler")) not in g


def test_partial_fill_is_labelled_separately(ctx, tacit_dir, monkeypatch):
    """근거는 있는데 전수가 아닌 경우 — 조치가 다르므로 사유를 갈라 놓는다.

    실측 ``hasFuelEnergySource``: Energy_Source_ID 가 180행 중 36행 비어 있다.
    모델링 오류가 아니라 소스 결측이므로 SME 가 CSV 를 채우면 되살아난다.
    """
    _stub_judge(monkeypatch, has=True, ratio=0.8, reason="column=X")
    g, _ = _graph_with_axiom()
    stats = step.apply(g, ctx).stats
    assert stats["existential_unsupported_partial_fill"] == 1
    assert stats["existential_unsupported_no_evidence"] == 0
    assert stats["existential_unsupported_detail"][0]["cause"] == "partial_fill"


def test_skolemized_uriref_restriction_is_detected(ctx, tacit_dir, monkeypatch):
    """BNode 요구는 ``step_19_bnode_skolemize`` 뒤에 무력해진다 (반복 실패 형태)."""
    _stub_judge(monkeypatch, has=False, ratio=0.0)
    named = D("Owner_hasThing_someValuesFrom")
    g, restriction = _graph_with_axiom(named)
    assert isinstance(restriction, URIRef)
    assert step.apply(g, ctx).stats["existential_axioms_pruned"] == 1


# ── PRESERVATION (오탐 방지) ───────────────────────────────────────────


def test_preserves_axiom_backed_only_by_tacit(ctx, tacit_dir, monkeypatch):
    """이 파일의 핵심: CSV 근거가 없어도 tacit 이 채우면 **살아 있는 공리**다.

    실측 ``AirEmissionMonitoring ⊑ ∃hasStackEquipment.EquipmentMaster`` —
    CSV 판정기는 근거 0 을 내지만 tacit 이 1,560/1,560 을 채워 S9 에서 위반이 아니다.
    CSV 축만 보면 산 공리를 지운다.
    """
    _stub_judge(monkeypatch, has=False, ratio=0.0, reason="no_column")
    _write_tacit(tacit_dir, """
        inst:a1 steel:hasThing inst:b1 .
        inst:a2 steel:hasThing inst:b2 .
    """)
    monkeypatch.setattr(_CSV, lambda cls: (["PK"], [["1"], ["2"]]))
    g, restriction = _graph_with_axiom()
    result = step.apply(g, ctx)
    assert result.stats["existential_axioms_pruned"] == 0, result.stats
    assert (D("Owner"), RDFS.subClassOf, restriction) in g


def test_tacit_denominator_is_owner_rows_not_tacit_subjects(ctx, tacit_dir, monkeypatch):
    """분모를 tacit 주어 수로 쓰면 언제나 1.0 이 되어 축이 죽는다.

    tacit 이 2개만 채우는데 owner 테이블이 10행이면 커버리지는 0.2 다.
    """
    _stub_judge(monkeypatch, has=False, ratio=0.0)
    _write_tacit(tacit_dir, """
        inst:a1 steel:hasThing inst:b1 .
        inst:a2 steel:hasThing inst:b2 .
    """)
    monkeypatch.setattr(_CSV, lambda cls: (["PK"], [[str(i)] for i in range(10)]))
    g, _ = _graph_with_axiom()
    stats = step.apply(g, ctx).stats
    assert stats["existential_axioms_pruned"] == 1
    assert stats["existential_unsupported_detail"][0]["tacit_coverage"] == 0.2


def test_preserves_axiom_backed_by_csv(ctx, tacit_dir, monkeypatch):
    """PRESERVATION: 전수 채운 FK 컬럼이 있으면 보존 (실측 47/51 이 이 경로)."""
    _stub_judge(monkeypatch, has=True, ratio=1.0, reason="column=Equipment_ID")
    g, restriction = _graph_with_axiom()
    result = step.apply(g, ctx)
    assert result.stats["existential_axioms_pruned"] == 0
    assert (D("Owner"), RDFS.subClassOf, restriction) in g


def test_undecidable_csv_verdict_is_preserved(ctx, tacit_dir, monkeypatch):
    """PRESERVATION: 판정기가 "판정 불가" 로 ``(True, 1.0)`` 을 내면 보존한다.

    ``no_csv_for_range`` / ``empty_csv`` / ``no_pk_candidate_in_range`` 경로.
    """
    _stub_judge(monkeypatch, has=True, ratio=1.0, reason="no_csv_for_range")
    g, restriction = _graph_with_axiom()
    assert step.apply(g, ctx).stats["existential_axioms_pruned"] == 0
    assert (D("Owner"), RDFS.subClassOf, restriction) in g


def test_op_declaration_survives_pruning(ctx, tacit_dir, monkeypatch):
    """PRESERVATION: 공리만 지우고 OP 선언은 남긴다.

    0행 phantom OP 판정은 step_22e/22f 의 축이다 — 여기서 겹치면 두 판정이 섞인다.
    """
    _stub_judge(monkeypatch, has=False, ratio=0.0)
    g, _ = _graph_with_axiom()
    step.apply(g, ctx)
    assert (D("hasThing"), RDF.type, OWL.ObjectProperty) in g


def test_shared_restriction_node_is_not_orphaned(ctx, tacit_dir, monkeypatch):
    """restriction 을 두 클래스가 공유하면 노드 자체는 남겨야 한다.

    ``step_19b`` 가 소유자를 분리하기 **전** 상태에서 실제로 공유가 일어난다. 노드를
    지우면 남은 클래스의 부모가 빈 껍데기가 된다.
    """
    calls = {"n": 0}

    def judge(_g, _op, owner, *_a, **_k):
        # Owner 만 근거 없음, Keeper 는 근거 있음.
        calls["n"] += 1
        return (False, 0.0, "x") if str(owner).endswith("Owner") else (True, 1.0, "ok")

    monkeypatch.setattr(_JUDGE, judge)
    g, restriction = _graph_with_axiom()
    g.add((D("Keeper"), RDF.type, OWL.Class))
    g.add((D("Keeper"), RDFS.subClassOf, restriction))

    result = step.apply(g, ctx)
    assert result.stats["existential_axioms_checked"] == 2
    assert result.stats["existential_axioms_pruned"] == 1
    assert (D("Owner"), RDFS.subClassOf, restriction) not in g
    assert (D("Keeper"), RDFS.subClassOf, restriction) in g
    # 공유 노드의 정의 트리플이 살아 있어야 Keeper 의 공리가 의미를 유지한다.
    assert (restriction, OWL.someValuesFrom, D("Filler")) in g
    assert (restriction, OWL.onProperty, D("hasThing")) in g


def test_foreign_property_axiom_is_ignored(ctx, tacit_dir, monkeypatch):
    """PRESERVATION: 외래 프로퍼티에 걸린 공리는 우리 CSV 로 판정할 수 없다."""
    _stub_judge(monkeypatch, has=False, ratio=0.0)
    foreign = URIRef("https://spec.industrialontologies.org/ontology/construct/hasP")
    g = Graph()
    restriction = BNode()
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, foreign))
    g.add((restriction, OWL.someValuesFrom, D("Filler")))
    g.add((D("Owner"), RDFS.subClassOf, restriction))
    result = step.apply(g, ctx)
    assert result.stats["existential_axioms_checked"] == 0
    assert (D("Owner"), RDFS.subClassOf, restriction) in g


# ── 모드 스위치 ────────────────────────────────────────────────────────


def test_warn_mode_measures_without_modifying(ctx, tacit_dir, monkeypatch):
    monkeypatch.setenv("TBOX_EXISTENTIAL_AUDIT", "warn")
    _stub_judge(monkeypatch, has=False, ratio=0.0)
    g, restriction = _graph_with_axiom()
    result = step.apply(g, ctx)
    assert result.stats["existential_axioms_unsupported"] == 1
    assert result.stats["existential_axioms_pruned"] == 0
    assert result.triples_delta == 0
    assert (D("Owner"), RDFS.subClassOf, restriction) in g


def test_off_mode_is_noop(ctx, tacit_dir, monkeypatch):
    monkeypatch.setenv("TBOX_EXISTENTIAL_AUDIT", "off")
    _stub_judge(monkeypatch, has=False, ratio=0.0)
    g, restriction = _graph_with_axiom()
    result = step.apply(g, ctx)
    assert result.triples_delta == 0
    assert result.stats["existential_axioms_checked"] == 0
    assert (D("Owner"), RDFS.subClassOf, restriction) in g


# ── 판정기 공유 / 배선 ────────────────────────────────────────────────


def test_audit_reuses_the_generation_judge():
    """사본을 만들면 생성과 감사가 갈린다 — 이 리포의 반복 실패 형태.

    소스 검사로 못 박는다: 감사 스텝이 ``_fk_evidence_for_op`` 를 **import** 해야
    하고, 자체 근거 판정 로직을 갖지 않아야 한다.
    """
    import inspect

    src = inspect.getsource(step)
    assert "_fk_evidence_for_op" in src
    assert "_REQUIRED_FILL_RATIO" in src, "임계값도 생성 쪽과 공유해야 한다"
    # 값 교집합 판정을 여기서 다시 구현하면 두 답이 갈린다.
    assert "target_values" not in src, "근거 판정 로직이 복제됐다"


def test_step_is_registered_before_restriction_dedup():
    """20(restriction dedup) 앞이어야 지운 공리가 되살아나지 않는다."""
    from tools.quality_steps import _MAIN_POST_STEP9, _POST_STEPS

    main = [getattr(fn, "__module__", "") for fn in _MAIN_POST_STEP9]
    post = [getattr(fn, "__module__", "") for fn in _POST_STEPS]
    assert any("step_13e_existential_axiom_audit" in n for n in main), main
    # dedup(20) 은 _POST_STEPS 에 있고 그 리스트가 본문 뒤에 돌므로, 본문에 등록된
    # 이 스텝은 구조적으로 dedup 보다 앞이다.
    assert any("step_20" in n for n in post), post
    assert not any("step_13e" in n for n in post), "감사가 dedup 뒤로 밀렸다"


# ── 산출물 대조 ────────────────────────────────────────────────────────


def test_deployed_tbox_prunes_only_the_measured_axioms():
    """배포 T-Box 실측(51개 중 3개 제거 / 48 보존)과 대조한다.

    개수를 못 박는 대신 **보존이 압도적인지**를 본다 — 판정기가 무너지면 대량 삭제로
    나타난다. 공리를 잃는 쪽이 근거 없는 공리보다 위험하다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    stats = step.apply(g, StepContext(domain_ns=NS)).stats
    checked = stats["existential_axioms_checked"]
    if checked == 0:
        pytest.skip("배포 T-Box 에 someValuesFrom 공리가 없다")
    assert stats["existential_axioms_pruned"] <= checked * 0.2, stats
    assert stats["existential_axioms_preserved"] >= checked * 0.8, stats
