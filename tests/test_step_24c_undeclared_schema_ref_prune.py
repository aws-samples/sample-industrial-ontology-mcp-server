"""Step 24c — 선언되지 않은 클래스를 가리키는 계층 참조를 제거하는가.

## 왜 이 파일이 있는가

``check_schema_reference_integrity`` (validate_kg 23번) 는 2026-08-24 에 이 결함을
잡도록 도입됐지만 **S3 에 대응 수정 스텝이 없었다.** 그래서 매 S2 마다 같은 위반이
재발했다. 2026-09-03 실측 (S2 재실행 직후 배포 T-Box)::

    MaintenanceHistory ⊑ MaintenanceActivity     ← MaintenanceActivity 미선언
    validate_kg: 스키마 참조 무결성 FAIL (983 검사 / 위반 1)

이름까지 그 게이트 docstring 의 2026-08-24 예시와 동일하다 — 열흘 넘게 게이트만
발화하고 아무도 고치지 않았다.

## 이 파일이 주장하는 것

**과잉 제거를 하지 않는다는 것.** 이 스텝은 그래프에서 트리플을 지우므로, 잡는
능력보다 **지우지 말아야 할 것을 보존하는가** 가 위험한 축이다 (이 리포의
"NEGATIVE 방향 테스트"). 그래서 보존 케이스가 검출 케이스보다 많다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_24c_undeclared_schema_ref_prune as step
from tools.quality_steps._base import StepContext

#: 실재하는 IOF 용어 네임스페이스. ``/ontology/core/`` 는 **실재하지 않는다** —
#: 이 리포는 모듈별 IRI 를 허구로 쓰다 unsat 8건을 드러낸 이력이 있다
#: (``domain/namespaces.py::FOREIGN_PREFIXES`` 가 정본).
_IOF_CORE = "https://spec.industrialontologies.org/ontology/construct/"


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _run(g: Graph, **env):
    for key, value in env.items():
        os.environ[key] = value
    try:
        return step.apply(g, StepContext(domain_ns=str(DOMAIN_NS)))
    finally:
        for key in env:
            os.environ.pop(key, None)


def _base_graph() -> Graph:
    """선언된 클래스 3개 + 프로퍼티 1개."""
    g = Graph()
    for cls in ("Child", "RealParent", "OtherParent"):
        g.add((D(cls), RDF.type, OWL.Class))
    g.add((D("someProp"), RDF.type, OWL.ObjectProperty))
    return g


def _parents(g: Graph, child: str) -> set[str]:
    return {
        str(o).split("#")[-1] for o in g.objects(D(child), RDFS.subClassOf)
        if isinstance(o, URIRef)
    }


# ── THE REGRESSION: 유령 부모를 제거하는가 ──────────────────────────────


def test_dangling_subclassof_is_pruned():
    """미선언 부모를 가리키는 subClassOf 는 제거된다 (다른 부모가 남아 있을 때)."""
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    g.add((D("Child"), RDFS.subClassOf, D("Ghost")))      # Ghost 미선언
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 1, stats
    assert stats["undeclared_ref_pruned"] == 1, stats
    assert _parents(g, "Child") == {"RealParent"}, (
        f"유령 부모가 남았거나 정당한 부모까지 지웠다: {_parents(g, 'Child')}"
    )


def test_dangling_subpropertyof_is_pruned():
    """subPropertyOf 축도 같은 계층 축이므로 제거 대상이다."""
    g = _base_graph()
    g.add((D("someProp"), RDFS.subPropertyOf, D("realParentProp")))
    g.add((D("realParentProp"), RDF.type, OWL.ObjectProperty))
    g.add((D("someProp"), RDFS.subPropertyOf, D("ghostProp")))
    stats = _run(g).stats
    assert stats["undeclared_ref_pruned"] == 1, stats
    remaining = {str(o).split("#")[-1]
                 for o in g.objects(D("someProp"), RDFS.subPropertyOf)}
    assert remaining == {"realParentProp"}, remaining


# ── PRESERVATION: 지우지 말아야 할 것 ───────────────────────────────────


def test_declared_parent_is_preserved():
    """선언된 부모는 위반이 아니다 — 발화 자체가 없어야 한다."""
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    g.add((D("Child"), RDFS.subClassOf, D("OtherParent")))
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 0, stats
    assert _parents(g, "Child") == {"RealParent", "OtherParent"}


def test_sole_parent_is_kept_even_if_undeclared():
    """유일한 부모면 지우지 않는다 — 지우면 계층에서 고립된다."""
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("Ghost")))      # 유일한 부모
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 1, stats
    assert stats["undeclared_ref_pruned"] == 0, stats
    assert stats["undeclared_ref_kept_sole_parent"] == 1, stats
    assert _parents(g, "Child") == {"Ghost"}, (
        "유일한 부모를 지워 클래스를 계층에서 고립시켰다"
    )


def test_named_restriction_parent_is_preserved():
    """명명된 ``owl:Restriction`` 부모는 공리 노드다 — 미선언이 아니다.

    이 리포는 명명 Restriction 을 클래스로 오인해 클래스 수가 2.4배 부풀고
    baseline 이 FAIL 로 고정된 이력이 있다.
    """
    g = _base_graph()
    restr = D("Child_someProp_maxCardinality")
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, D("someProp")))
    g.add((restr, OWL.maxCardinality, Literal(1)))
    g.add((D("Child"), RDFS.subClassOf, restr))
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 0, stats
    assert restr in set(g.objects(D("Child"), RDFS.subClassOf)), (
        "공리 노드(Restriction)를 유령 부모로 오판해 제거했다"
    )


def test_foreign_namespace_parent_is_preserved():
    """외래 온톨로지(IOF/BFO)는 이 그래프에 선언이 없는 것이 정상이다."""
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, URIRef(_IOF_CORE + "MaterialArtifact")))
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 0, stats
    assert any("industrialontologies" in str(o)
               for o in g.objects(D("Child"), RDFS.subClassOf)), (
        "외래 부모를 지웠다 — owl:imports 대상이라 선언이 없는 것이 정상이다"
    )


def test_dangling_domain_range_is_reported_not_pruned():
    """domain/range 는 제거가 곧 제약 약화라 보고만 한다 (미선언 ≠ universal)."""
    g = _base_graph()
    g.add((D("someProp"), RDFS.domain, D("GhostDomain")))
    g.add((D("someProp"), RDFS.range, D("GhostRange")))
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 2, stats
    assert stats["undeclared_ref_pruned"] == 0, stats
    assert stats["undeclared_ref_reported_not_pruned"] == 2, stats
    assert D("GhostDomain") in set(g.objects(D("someProp"), RDFS.domain))
    assert D("GhostRange") in set(g.objects(D("someProp"), RDFS.range))


def test_foreign_subject_is_not_this_tbox_responsibility():
    """외래 주어의 참조는 이 T-Box 가 고칠 대상이 아니다."""
    g = _base_graph()
    g.add((URIRef(_IOF_CORE + "Foo"), RDFS.subClassOf, D("Ghost")))
    stats = _run(g).stats
    assert stats["undeclared_ref_violations"] == 0, stats


# ── 모드 스위치 + 계약 ──────────────────────────────────────────────────


def test_warn_mode_detects_without_removing():
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    g.add((D("Child"), RDFS.subClassOf, D("Ghost")))
    stats = _run(g, TBOX_UNDECLARED_REF_PRUNE="warn").stats
    assert stats["undeclared_ref_would_prune"] == 1, stats
    assert stats["undeclared_ref_pruned"] == 0, stats
    assert D("Ghost") in set(g.objects(D("Child"), RDFS.subClassOf))


def test_off_mode_skips():
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("Ghost")))
    stats = _run(g, TBOX_UNDECLARED_REF_PRUNE="off").stats
    assert "undeclared_ref_skipped" in stats, stats
    assert D("Ghost") in set(g.objects(D("Child"), RDFS.subClassOf))


def test_gate_and_step_use_the_same_judge():
    """게이트와 스텝이 **같은 판정 함수** 를 쓰는지 — 사본이 생기면 어긋난다.

    스텝이 지운 뒤 게이트가 통과해야 한다. 판정이 갈라지면 스텝이 고쳤는데
    게이트가 계속 FAIL 하거나 그 반대가 된다 (이 리포에서 반복된 형태).
    """
    from tools.validation_support.checks.semantic import (
        check_schema_reference_integrity,
    )

    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    g.add((D("Child"), RDFS.subClassOf, D("Ghost")))
    assert check_schema_reference_integrity(g)["passed"] is False
    _run(g)
    after = check_schema_reference_integrity(g)
    assert after["passed"] is True, (
        f"스텝이 지웠는데 게이트가 여전히 FAIL — 판정 사본이 생겼다: {after}"
    )


def test_step_result_contract():
    g = _base_graph()
    g.add((D("Child"), RDFS.subClassOf, D("RealParent")))
    g.add((D("Child"), RDFS.subClassOf, D("Ghost")))
    res = _run(g)
    assert res.name == "step_24c_undeclared_schema_ref_prune"
    assert res.step_number == "24c"
    assert res.triples_delta == -1
    for key in ("undeclared_ref_checked", "undeclared_ref_violations",
                "undeclared_ref_pruned", "undeclared_ref_kept_sole_parent",
                "undeclared_ref_reported_not_pruned", "undeclared_ref_mode"):
        assert key in res.stats, f"stats 에 {key} 가 없다"


def test_registered_in_pipeline_before_ontoclean():
    """등록 위치 — 계층 생성(24b) 뒤, OntoClean(25) 앞이어야 한다.

    25 는 부모의 메타속성을 읽어 C2/C3 를 판정하므로 유령 부모가 남아 있으면
    실재하지 않는 노드를 기준으로 위반을 센다. 배선을 소스로 확인한다 — 단위
    테스트가 초록인데 스텝이 파이프라인에 등록되지 않은 전례가 있다.
    """
    from tools.quality_steps import _POST_STEPS

    names = [fn.__module__.rsplit(".", 1)[-1] for fn in _POST_STEPS]
    assert "step_24c_undeclared_schema_ref_prune" in names, (
        f"24c 가 _POST_STEPS 에 등록되지 않았다: {names}"
    )
    idx = names.index("step_24c_undeclared_schema_ref_prune")
    assert names.index("step_24b_odp_auto_apply") < idx, "24b 보다 앞에 있다"
    assert idx < names.index("step_25_ontoclean"), "25(OntoClean) 보다 뒤에 있다"


def test_shipped_tbox_has_no_undeclared_schema_reference():
    """산출물 확인 — 배포 T-Box 에 유령 계층 참조가 남지 않았는가.

    단위 픽스처가 통과해도 실제 T-Box 에서 무발화일 수 있다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    g = Graph()
    g.parse(TBOX_PATH, format="turtle")
    stats = _run(g, TBOX_UNDECLARED_REF_PRUNE="warn").stats
    prunable = stats["undeclared_ref_would_prune"]
    assert prunable == 0, (
        "배포 T-Box 에 제거 가능한 유령 계층 참조가 남아 있다: "
        f"{stats.get('undeclared_ref_pruned_sample')}"
    )
