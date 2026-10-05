"""수동 추가분 DP 가 S2 산출물과 같은 컬럼을 주장하면 양보하는가.

2026-08-29 실측. 파이프라인 재실행에서 ③(합성 조인키 → FK ID) 이 **부분적으로만**
동작했다. 6개 클래스 중 3개의 FK 리터럴이 0건이었다::

    dcterms:source 충돌 3건 — transliteration 폴백 사용
      (ProcessSteelmakingFurnace, PRODUCT_ID): {steelmakingProductIdRef, steelmakingProductId}
      (ProcessRolling,            PRODUCT_ID): {rollingProductIdRef,      rollingProductId}
      (ProcessContinuousCasting,  PRODUCT_ID): {continuousCastingProductIdRef, …}

S2 가 이번 실행에서 ``steelmakingProductId`` 를 **스스로 만들었고**, 패치의
``steelmakingProductIdRef`` 와 같은 (클래스, 컬럼) 을 주장했다. A-Box 생성기는 두 DP
가 한 컬럼을 주장하면 어느 쪽이 정본인지 판별할 수 없어 **양쪽 모두 버린다**
(``abox_generation`` 의 ``source_conflicts``) — 그래서 보완이 아니라 **파괴**가 됐다.

## 왜 패치 파일을 손으로 맞추면 안 되나

S2 산출물은 실행마다 달라진다. 이번엔 3개를 만들었지만 다음엔 안 만들 수 있고, 그러면
패치에서 지운 선언이 다시 필요해진다. 어느 쪽으로 맞춰도 반대 경우에 깨진다.

그래서 **병합 시점에 판정**한다: 그래프에 같은 (domain, source) 를 주장하는 DP 가 이미
있으면 패치 DP 를 건너뛴다. 패치의 역할은 "S2 가 안 만들 때의 보완" 이므로 S2 산출물이
있으면 양보하는 것이 맞다.

## 이 테스트의 방향

"충돌을 피한다" 만 주장하면 패치를 전부 건너뛰어도 통과한다. 세 축을 고정한다:

* 회피 — 같은 (domain, source) 를 주장하는 패치 DP 는 건너뛴다
* 보존 — 충돌하지 않는 패치 DP 는 정상 병합된다 (과잉 회피 방지)
* 범위 — 같은 컬럼이라도 **다른 domain** 이면 충돌이 아니다
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Namespace, URIRef

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.quality_steps.step_30_tbox_manual_additions import _conflicting_patch_dps

NS = Namespace(str(DOMAIN_NS))
DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")
PATCH_FILE = pathlib.Path("rules/domain/tbox_manual_additions.ttl")


def _dp(graph: Graph, name: str, domain: str, source: str) -> URIRef:
    uri = NS[name]
    graph.add((uri, RDF.type, OWL.DatatypeProperty))
    graph.add((uri, RDFS.domain, NS[domain]))
    graph.add((uri, RDFS.range, XSD.string))
    graph.add((uri, DCTERMS_SOURCE, __import__("rdflib").Literal(source)))
    return uri


def _graphs(existing: list[tuple], patch: list[tuple]) -> tuple[Graph, Graph]:
    g, p = Graph(), Graph()
    bind_namespaces(g)
    bind_namespaces(p)
    for name, dom, src in existing:
        _dp(g, name, dom, src)
    for name, dom, src in patch:
        _dp(p, name, dom, src)
    return g, p


# ── 회피: 같은 (domain, source) 는 건너뛴다 ─────────────────────────────


def test_same_domain_and_column_is_conflicting():
    """THE REGRESSION: S2 가 만든 DP 와 같은 컬럼을 주장하면 패치를 건너뛴다."""
    g, p = _graphs(
        existing=[("steelmakingProductId", "ProcessSteelmakingFurnace", "Product_ID")],
        patch=[("steelmakingProductIdRef", "ProcessSteelmakingFurnace", "Product_ID")],
    )

    conflicting = _conflicting_patch_dps(g, p)

    assert NS["steelmakingProductIdRef"] in conflicting


def test_column_comparison_is_case_insensitive():
    """``Product_ID`` / ``PRODUCT_ID`` 는 같은 컬럼이다.

    A-Box 생성기가 ``upper()`` 로 인덱스를 만들므로 여기서도 같게 봐야 한다 —
    대소문자만 다르면 충돌을 놓치고 그 컬럼이 조용히 폴백된다.
    """
    g, p = _graphs(
        existing=[("dpA", "C", "product_id")],
        patch=[("dpB", "C", "PRODUCT_ID")],
    )

    assert NS["dpB"] in _conflicting_patch_dps(g, p)


def test_multiple_conflicts_are_all_reported():
    """실측 3건처럼 여러 충돌을 한 번에 잡는다."""
    g, p = _graphs(
        existing=[("aId", "A", "Product_ID"), ("bId", "B", "Product_ID")],
        patch=[("aIdRef", "A", "Product_ID"), ("bIdRef", "B", "Product_ID"),
               ("cIdRef", "C", "Product_ID")],
    )

    conflicting = _conflicting_patch_dps(g, p)

    assert conflicting == {NS["aIdRef"], NS["bIdRef"]}, (
        "C 는 기존 DP 가 없으므로 충돌이 아니다"
    )


# ── 보존: 충돌하지 않는 것은 병합한다 (NEGATIVE 방향) ───────────────────


def test_new_column_is_not_conflicting():
    """기존에 없는 컬럼을 주장하는 패치 DP 는 정상 병합된다."""
    g, p = _graphs(
        existing=[("someOther", "C", "Other_Col")],
        patch=[("newIdRef", "C", "Equipment_ID")],
    )

    assert _conflicting_patch_dps(g, p) == set()


def test_same_column_different_domain_is_not_conflicting():
    """같은 컬럼명이라도 domain 이 다르면 충돌이 아니다.

    ``Product_ID`` 는 여러 테이블에 등장하고 각각 다른 클래스의 DP 가 된다 —
    domain 을 무시하면 정당한 패치를 대량으로 잃는다.
    """
    g, p = _graphs(
        existing=[("rollingProductId", "ProcessRolling", "Product_ID")],
        patch=[("blastFurnaceProductIdRef", "ProcessBlastFurnace", "Product_ID")],
    )

    assert _conflicting_patch_dps(g, p) == set()


def test_patch_dp_without_source_is_not_conflicting():
    """``dcterms:source`` 가 없는 패치 DP 는 컬럼을 주장하지 않는다."""
    g = Graph()
    bind_namespaces(g)
    _dp(g, "existing", "C", "Product_ID")
    p = Graph()
    bind_namespaces(p)
    p.add((NS["noSource"], RDF.type, OWL.DatatypeProperty))
    p.add((NS["noSource"], RDFS.domain, NS["C"]))

    assert _conflicting_patch_dps(g, p) == set()


def test_object_properties_are_out_of_scope():
    """OP 는 ``dcterms:source`` 축이 아니므로 이 판정 대상이 아니다."""
    g = Graph()
    bind_namespaces(g)
    p = Graph()
    bind_namespaces(p)
    p.add((NS["someOp"], RDF.type, OWL.ObjectProperty))
    p.add((NS["someOp"], RDFS.domain, NS["C"]))

    assert _conflicting_patch_dps(g, p) == set()


# ── 배선 + 패치 파일 정합 ───────────────────────────────────────────────


def test_merge_step_uses_the_guard():
    """``apply`` 가 실제로 이 판정을 쓰는가 — 함수만 있고 배선이 없으면 무의미하다."""
    src = pathlib.Path(
        "tools/quality_steps/step_30_tbox_manual_additions.py",
    ).read_text(encoding="utf-8")

    assert "_conflicting_patch_dps(g, patch)" in src, "apply 가 호출하지 않는다"
    assert "if s in skipped_dp_conflict:" in src, "skip 배선이 없다"


def test_merge_does_not_add_conflicting_dp(tmp_path, monkeypatch):
    """**병합 결과로** 확인한다 — 충돌 DP 가 그래프에 들어가지 않는다.

    소스 문자열 검사만으로는 배선이 다른 방식으로 깨졌을 때 놓친다 (이 리포는
    "단위 테스트 초록인데 산출물이 안 바뀐" 사고를 겪었다). 실제 패치 파일을 만들어
    ``apply`` 를 돌리고 그래프를 본다.
    """
    import tools.quality_steps.step_30_tbox_manual_additions as step

    patch_file = tmp_path / "tbox_manual_additions.ttl"
    patch_file.write_text(
        "steel:conflictDp a owl:DatatypeProperty ;\n"
        "    rdfs:domain steel:C ;\n"
        "    rdfs:range xsd:string ;\n"
        '    dcterms:source "Product_ID" .\n'
        "\n"
        "steel:freshDp a owl:DatatypeProperty ;\n"
        "    rdfs:domain steel:C ;\n"
        "    rdfs:range xsd:string ;\n"
        '    dcterms:source "Other_Col" .\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(step, "_patch_path", lambda: str(patch_file))

    graph = Graph()
    bind_namespaces(graph)
    _dp(graph, "existingDp", "C", "Product_ID")     # 같은 (C, Product_ID) 선점

    class _Ctx:
        pass

    step.apply(graph, _Ctx())

    assert (NS["conflictDp"], RDF.type, OWL.DatatypeProperty) not in graph, (
        "충돌하는 패치 DP 가 병합됐다 — 양쪽이 폴백으로 떨어진다"
    )
    assert (NS["freshDp"], RDF.type, OWL.DatatypeProperty) in graph, (
        "충돌하지 않는 패치 DP 가 병합되지 않았다 — 과잉 회피다"
    )
    assert (NS["existingDp"], RDF.type, OWL.DatatypeProperty) in graph


def test_patch_file_has_no_removed_declarations():
    """충돌하던 3개가 패치 파일에서 제거됐는가.

    병합 가드가 있어도 파일에 남겨두면 S2 가 그 DP 를 안 만든 실행에서 다시
    충돌 위험이 생긴다 (S2 산출물이 실행마다 달라진다).
    """
    if not PATCH_FILE.exists():
        pytest.skip("패치 파일 없음")

    text = PATCH_FILE.read_text(encoding="utf-8")
    for removed in ("steelmakingProductIdRef", "rollingProductIdRef",
                    "continuousCastingProductIdRef"):
        assert f"steel:{removed} a owl:DatatypeProperty" not in text, (
            f"{removed} 선언이 남아 있다 — S2 가 같은 컬럼 DP 를 만들면 충돌한다"
        )


def test_patch_file_keeps_the_working_declarations():
    """S2 가 만들지 않는 3개는 유지돼야 한다 (실측: 이들이 FK 리터럴을 채운다)."""
    if not PATCH_FILE.exists():
        pytest.skip("패치 파일 없음")

    text = PATCH_FILE.read_text(encoding="utf-8")
    for kept in ("equipmentStatusEquipmentIdRef", "blastFurnaceProductIdRef",
                 "realTimeDataTagIdRef"):
        assert f"steel:{kept} a owl:DatatypeProperty" in text, f"{kept} 선언이 없다"


def test_deployed_abox_has_fk_literals():
    """배포 A-Box 에 FK 리터럴이 실제로 채워졌는가 — 충돌 해소의 최종 증거.

    실측: 충돌 상태에서는 3개 DP 가 0건이었다.
    """
    abox = pathlib.Path("data/generated/abox/a_box.ttl")
    if not abox.exists():
        pytest.skip("A-Box 없음")

    counts = dict.fromkeys(
        ("equipmentStatusEquipmentIdRef", "blastFurnaceProductIdRef",
         "realTimeDataTagIdRef"), 0,
    )
    with abox.open(encoding="utf-8") as fh:
        for line in fh:
            for dp in counts:
                if f"steel:{dp} " in line:
                    counts[dp] += 1

    empty = [dp for dp, n in counts.items() if n == 0]
    assert not empty, f"FK 리터럴이 0건인 DP: {empty} (dcterms:source 충돌 의심)"
