"""Step 13 — ``someValuesFrom`` 생성이 멱등한가 + 근거 없는 존재 공리를 막는가.

## 왜 이 파일이 있는가 (2026-09-03~04 실측)

S3 를 자기 출력에 한 번 더 돌렸을 때 두 결함이 동시에 터졌다.

**① 멱등 가드가 노드 종류로 공리를 식별했다.** 가드가
``isinstance(parent, BNode)`` 를 요구했는데, 이 스텝보다 **뒤에** 도는
``step_19_bnode_skolemize`` 가 restriction 을 전부 명명 URIRef 로 바꾼다::

    배포 T-Box someValuesFrom 40개 (전부 URIRef)
    step_13 재실행 → 36개 추가, 그중 35개가 기존과 동일한 (op, owner) 쌍

같은 형태를 이 리포는 이미 겪었다 — ``check_shacl_owl_cardinality_sync`` 가
restriction 이 BNode 일 것을 요구해 스콜렘화 후 32개를 하나도 추출하지 못했다.

**② CSV 가 없는 클래스는 근거 검사를 무조건 통과했다.**
``_fk_evidence_for_op`` 가 ``dom_csv is None`` 이면 ``(True, 1.0)`` 을 돌려줬다 —
"확인할 수 없다" 가 "전수 채움" 으로 기록된 것이다. 그 대가::

    ManufacturingProcessStep ⊑ ∃followedBy.ManufacturingProcessStep
      → 추론 restriction 위반 34,562건

공정 체인의 **마지막 단계(압연)에는 후행이 없다**. tacit 은 4개 공정 노드 중 3개에만
``followedBy`` 를 건다 (실측 3/4 = 0.75).

## 이 파일이 주장하는 것

멱등성은 "재실행 후 개수가 같다" 로, 근거 판정은 "부분 커버리지를 거부하고 **측정
불가는 거부하지 않는다**" 로 주장한다. 후자가 중요하다 — 측정 불가를 위반으로 읽으면
정당한 공리를 대량으로 잃는다 (실측: 기존 40개 중 CSV 없는 경로는 2개뿐이고 둘 다
개체 0이라 판정 근거가 없다).
"""
from __future__ import annotations

import os

import pytest
from rdflib import RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, XSD

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_13_pk_functional_someValuesFrom as step
from tools.quality_steps._base import StepContext


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _svf_nodes(g: Graph) -> set:
    return set(g.subjects(OWL.someValuesFrom, None))


def _svf_pairs(g: Graph) -> set[tuple[str, str]]:
    """(owner local, onProperty local) 쌍 — 노드 종류와 무관한 공리 정체성."""
    out = set()
    for restriction in g.subjects(OWL.someValuesFrom, None):
        op = g.value(restriction, OWL.onProperty)
        for owner in g.subjects(RDFS.subClassOf, restriction):
            if isinstance(owner, URIRef):
                out.add((str(owner).split("#")[-1], str(op).split("#")[-1]))
    return out


# ── ① 멱등성 — 스콜렘화된 restriction 을 알아보는가 ──────────────────────


def _graph_with_named_restriction() -> Graph:
    """step_19 를 거친 상태 재현 — restriction 이 **명명 URIRef** 다."""
    g = Graph()
    for cls in ("Detail", "Master"):
        g.add((D(cls), RDF.type, OWL.Class))
    op = D("detailHasMaster")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, D("Detail")))
    g.add((op, RDFS.range, D("Master")))
    restriction = D("Detail_detailHasMaster_someValuesFrom")     # 스콜렘화 산물
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, op))
    g.add((restriction, OWL.someValuesFrom, D("Master")))
    g.add((D("Detail"), RDFS.subClassOf, restriction))
    return g


def test_named_restriction_is_recognised_as_already_present():
    """THE REGRESSION: URIRef restriction 이 있으면 중복 추가하지 않는다."""
    g = _graph_with_named_restriction()
    assert step._already_restricted(g, D("Detail"), D("detailHasMaster")) is True, (
        "스콜렘화된(URIRef) restriction 을 못 알아봤다 — 재실행마다 중복이 생긴다"
    )


def test_bnode_restriction_still_recognised():
    """PRESERVATION: 첫 실행의 BNode restriction 도 그대로 알아본다."""
    g = _graph_with_named_restriction()
    g.remove((D("Detail"), RDFS.subClassOf, D("Detail_detailHasMaster_someValuesFrom")))
    anon = BNode()
    g.add((anon, RDF.type, OWL.Restriction))
    g.add((anon, OWL.onProperty, D("detailHasMaster")))
    g.add((anon, OWL.someValuesFrom, D("Master")))
    g.add((D("Detail"), RDFS.subClassOf, anon))
    assert step._already_restricted(g, D("Detail"), D("detailHasMaster")) is True


def test_unrelated_property_is_not_suppressed():
    """PRESERVATION: 다른 프로퍼티의 restriction 은 억제 근거가 아니다."""
    g = _graph_with_named_restriction()
    assert step._already_restricted(g, D("Detail"), D("someOtherOp")) is False


def test_shipped_tbox_reapply_adds_nothing():
    """산출물 확인 — 배포 T-Box 에 step_13 을 다시 돌려도 변화가 없어야 한다.

    이 스텝이 비멱등이면 S3 재실행이 곧 공리 중복이다. 단위 픽스처가 통과해도
    실제 T-Box 에서 무발화일 수 있으므로 실물로 확인한다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    before_nodes, before_pairs, before_len = _svf_nodes(g), _svf_pairs(g), len(g)
    stats = step.apply(g, StepContext(domain_ns=str(DOMAIN_NS))).stats
    assert stats["restrictions_added"] == 0, (
        f"배포 T-Box 에 someValuesFrom {stats['restrictions_added']}개가 "
        "중복 추가됐다 — step_13 이 비멱등이다"
    )
    assert len(_svf_nodes(g)) == len(before_nodes)
    assert _svf_pairs(g) == before_pairs
    assert len(g) == before_len, "트리플 수가 변했다 — 다른 축이 비멱등이다"


# ── ② 근거 — CSV 가 없는 클래스에 tacit 커버리지를 쓰는가 ─────────────────


def _write_tacit(directory, name: str, lines: list[str]):
    header = [
        f"@prefix steel: <{DOMAIN_NS}> .",
        f"@prefix steel-inst: <{str(DOMAIN_NS).rstrip('#')}/instances#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "",
    ]
    path = os.path.join(str(directory), f"{name}.ttl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(header + lines) + "\n")
    return path


@pytest.fixture
def chain_tacit(tmp_path, monkeypatch):
    """추상 부모 ``Step`` + 자손 4개. CSV 테이블은 없다 (부모는 추상)."""
    tacit = tmp_path / "tacit"
    tacit.mkdir()
    monkeypatch.setattr("config.SOURCE_TACIT_DIR", str(tacit), raising=False)
    import domain.tbox_utils as tu
    monkeypatch.setattr(tu, "SOURCE_TACIT_DIR", str(tacit), raising=False)
    monkeypatch.setattr(tu, "_tacit_cache", None, raising=False)
    return tacit


def _chain_graph() -> Graph:
    g = Graph()
    g.add((D("Step"), RDF.type, OWL.Class))
    for child in ("StepA", "StepB", "StepC", "StepD"):
        g.add((D(child), RDF.type, OWL.Class))
        g.add((D(child), RDFS.subClassOf, D("Step")))
    op = D("followedBy")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.domain, D("Step")))
    g.add((op, RDFS.range, D("Step")))
    return g


def _chain_lines(links: int) -> list[str]:
    """4개 노드를 타입하고, 앞 ``links`` 개에만 followedBy 를 건다."""
    names = ["StepA", "StepB", "StepC", "StepD"]
    out = [f"steel-inst:{n}_1 rdf:type steel:{n} ." for n in names]
    for i in range(links):
        out.append(
            f"steel-inst:{names[i]}_1 steel:followedBy steel-inst:{names[i + 1]}_1 ."
        )
    return out


def test_partial_tacit_coverage_blocks_existential(chain_tacit):
    """THE REGRESSION: 체인 끝에 후행이 없으면 ∃followedBy 는 거짓이다.

    4개 중 3개만 followedBy 를 가지므로 커버리지 0.75 → 공리를 만들지 않는다.
    """
    _write_tacit(chain_tacit, "flow", _chain_lines(3))
    g = _chain_graph()
    covered, total = step._tacit_coverage_for_op(g, D("followedBy"), D("Step"))
    assert (covered, total) == (3, 4), (covered, total)
    has_basis, ratio, why = step._fk_evidence_for_op(
        g, D("followedBy"), D("Step"), D("Step"), str(DOMAIN_NS),
    )
    assert ratio == 0.75, why
    assert step._check_fk_null_ratio(
        "followedBy", "Step", g=g, op=D("followedBy"),
        domain_cls=D("Step"), range_cls=D("Step"), steel_str=str(DOMAIN_NS),
    ) is True, "부분 커버리지인데 존재 공리를 허용했다"

    stats = step.apply(g, StepContext(domain_ns=str(DOMAIN_NS))).stats
    assert stats["restrictions_added"] == 0, stats
    assert ("Step", "followedBy") not in _svf_pairs(g)


def test_full_tacit_coverage_allows_existential(chain_tacit):
    """PRESERVATION: 전수 채움이면 공리를 만든다 (가드가 항상 막으면 안 된다)."""
    lines = [f"steel-inst:Step{c}_1 rdf:type steel:Step{c} ." for c in "ABCD"]
    lines += [
        f"steel-inst:Step{c}_1 steel:followedBy steel-inst:StepA_1 ." for c in "ABCD"
    ]
    _write_tacit(chain_tacit, "flow", lines)
    g = _chain_graph()
    covered, total = step._tacit_coverage_for_op(g, D("followedBy"), D("Step"))
    assert (covered, total) == (4, 4)
    stats = step.apply(g, StepContext(domain_ns=str(DOMAIN_NS))).stats
    assert stats["restrictions_added"] == 1, stats
    assert ("Step", "followedBy") in _svf_pairs(g)


def test_no_individuals_keeps_previous_behaviour(chain_tacit):
    """측정 불가는 거부하지 않는다 — tacit 이 개체를 안 타입하면 이전대로 허용.

    실측: 배포 T-Box 의 CSV-없는 경로 2건(CoilProduct/SlabProduct)이 정확히 이
    경우다. 여기서 막으면 정당한 공리를 대량으로 잃는다.
    """
    _write_tacit(chain_tacit, "flow", ["steel-inst:X_1 rdf:type steel:Unrelated ."])
    g = _chain_graph()
    assert step._tacit_coverage_for_op(g, D("followedBy"), D("Step")) == (0, 0)
    has_basis, ratio, why = step._fk_evidence_for_op(
        g, D("followedBy"), D("Step"), D("Step"), str(DOMAIN_NS),
    )
    assert (has_basis, ratio) == (True, 1.0), why
    assert why == "no_csv_for_domain_no_individuals", why


def test_class_family_follows_graph_edges_not_names(chain_tacit):
    """자손 폐쇄는 이름이 아니라 subClassOf 간선으로 판정한다."""
    g = _chain_graph()
    g.add((D("Totally_Different"), RDF.type, OWL.Class))
    g.add((D("Totally_Different"), RDFS.subClassOf, D("StepA")))
    family = {str(c).split("#")[-1] for c in step._class_family(g, D("Step"))}
    assert family == {"Step", "StepA", "StepB", "StepC", "StepD", "Totally_Different"}


def test_shipped_tbox_has_no_partially_covered_existential():
    """산출물 확인 — 배포 T-Box 의 존재 공리가 부분 커버리지가 아닌가."""
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    # 판정은 ``effective_existential_coverage`` (CSV ∪ tacit) 로 한다.
    # ``_check_fk_null_ratio`` 단독은 domain 에 CSV 가 있으면 tacit 을 보지 않아
    # tacit 이 전수 채우는 공리를 "근거 부족" 으로 낸다 (2026-09-05 실측:
    # ``AirEmissionMonitoring ⊑ ∃hasStackEquipment`` — tacit 1,560/1,560, S9 위반 0).
    # 그 축을 기준으로 삼으면 이 테스트가 산 공리의 삭제를 요구한다.
    from tools.quality_steps.step_13_pk_functional_someValuesFrom import (
        _REQUIRED_FILL_RATIO,
    )
    from tools.quality_steps.step_13e_existential_axiom_audit import (
        effective_existential_coverage,
    )

    bad = []
    for restriction in g.subjects(OWL.someValuesFrom, None):
        op = g.value(restriction, OWL.onProperty)
        rng = g.value(restriction, OWL.someValuesFrom)
        for owner in g.subjects(RDFS.subClassOf, restriction):
            if not isinstance(owner, URIRef):
                continue
            effective, evidence = effective_existential_coverage(
                g, op, owner, rng, str(DOMAIN_NS),
            )
            if effective < _REQUIRED_FILL_RATIO:
                bad.append((str(owner).split("#")[-1],
                            str(op).split("#")[-1], evidence))
    assert not bad, f"근거가 부족한 존재 공리가 배포 T-Box 에 남아 있다: {bad}"


def test_tacit_loader_is_shared_not_copied():
    """판정 근거 로더는 정본 하나만 쓴다 (step_13 / step_13c 사본 금지)."""
    import inspect

    from tools.quality_steps import step_13c_op_max_cardinality_direction as s13c

    for module in (step, s13c):
        src = inspect.getsource(module)
        assert "load_tacit_graph" in src, (
            f"{module.__name__} 가 공용 tacit 로더를 쓰지 않는다"
        )
        assert "def _tacit_graph(" not in src, (
            f"{module.__name__} 에 tacit 로더 사본이 있다 — 판정이 갈라진다"
        )
    assert Literal and XSD  # import 사용 표시
