"""고아 노드 게이트가 **반증 가능** 한지 회귀 가드.

배경 (2026-08-08 실측): ``check_orphan_nodes`` 는 연결을 "``rdf:type`` 이 아닌
트리플의 주어이거나, 어떤 IRI 객체" 로 정의했다. 그런데 A-Box 생성기는 모든
인스턴스에 ``prov:wasDerivedFrom <row_uri>`` 를 무조건 붙인다
(``abox_generation.py:3348``). 그 한 줄이 조건을 만족하므로 **이 게이트는
파이프라인 산출물에서 0 이 아닌 값을 낼 수 없었다.**

즉 FK 해석이 통째로 실패해 테이블 하나가 그래프에서 떨어져 나가도 S9 는 조용했고,
운영자는 S12 에서 "2-hop 질의가 0건" 으로 만나 질의 문제로 디버깅했다.
문서화된 계약(``docs/reference/quality-framework.md`` "고아 노드 | COUNT = 0")이
반증 불가였다는 뜻이다.

수정 후 실측 (workshop/pre-generated/a_box.ttl, 737,414 트리플):
구 정의 0건 → 신 정의 31건. 예: ``SupplierMaster`` 50개 중 49개는 도메인 OP 링크가
있고 1개가 없다 — 진짜 FK 커버리지 갭이다.

2026-08-29 pre-generated 갱신 (A-Box 741,868 / T-Box+A-Box 747,170) 후 재확인:
**31건 그대로 재현** 되고 ``SupplierMaster_SUP031`` 예시도 동일하다. 즉 이 결함은
특정 산출물 세대의 우연이 아니다 (산출물이 갱신되면 이 확인을 다시 하라 — 게이트가
아니라 docstring 이므로 수치가 낡아도 테스트는 초록이다).
"""
from __future__ import annotations

import pytest
from rdflib import RDF, Graph, Literal, Namespace, URIRef

from tools.validation_support.checks.structural import check_orphan_nodes

STEEL = "http://example.com/steel-ontology#"
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")


def _c(name: str) -> URIRef:
    return URIRef(STEEL + name)


def _instance(*, add) -> Graph:
    """Equipment 인스턴스 1개 + ``add`` 가 붙이는 트리플."""
    g = Graph()
    inst = URIRef(STEEL + "Equipment_EQ001")
    g.add((inst, RDF.type, _c("Equipment")))
    add(g, inst)
    return g


def test_provenance_only_instance_is_an_orphan():
    """THE REGRESSION: prov 트리플만 가진 인스턴스는 고아다.

    생성기가 모든 인스턴스에 붙이는 트리플이라, 이것을 연결로 세면 게이트가
    구조적으로 실패할 수 없다.
    """
    g = _instance(add=lambda g, i: g.add((i, PROV.wasDerivedFrom, URIRef("prov://row/1"))))
    result = check_orphan_nodes(g)
    assert result["orphan_count"] == 1, (
        "prov 트리플이 연결로 계산됐다 — 게이트가 반증 불가 상태로 돌아갔다"
    )
    assert result["passed"] is False


def test_datatype_literal_only_instance_is_an_orphan():
    """DP 리터럴은 그래프 연결이 아니다 (값이 있어도 아무것과 안 이어졌다)."""
    g = _instance(add=lambda g, i: [
        g.add((i, _c("equipmentName"), Literal("Pump"))),
        g.add((i, _c("equipmentStatusValue"), Literal("RUN"))),
    ])
    assert check_orphan_nodes(g)["orphan_count"] == 1


def test_dcterms_metadata_is_not_connectivity():
    """``dcterms:`` 계열 메타 표기도 연결이 아니다."""
    g = _instance(add=lambda g, i: g.add((i, DCTERMS.identifier, Literal("EQ001"))))
    assert check_orphan_nodes(g)["orphan_count"] == 1


@pytest.mark.parametrize("direction", ["outgoing", "incoming"])
def test_domain_object_property_counts_as_connected(direction):
    """POSITIVE: 도메인 OP 로 이어져 있으면 고아가 아니다 (양방향)."""
    other = URIRef(STEEL + "Plant_P1")

    def add(g: Graph, inst: URIRef) -> None:
        g.add((other, RDF.type, _c("Plant")))
        if direction == "outgoing":
            g.add((inst, _c("locatedIn"), other))
        else:
            g.add((other, _c("hasEquipment"), inst))

    result = check_orphan_nodes(_instance(add=add))
    # 반대쪽(Plant)도 같은 OP 로 이어져 있으므로 둘 다 고아가 아니다.
    assert result["orphan_count"] == 0, f"samples={result['samples']}"
    assert result["passed"] is True


def test_foreign_namespace_property_is_not_connectivity():
    """남의 네임스페이스 프로퍼티로 이어진 것은 도메인 연결이 아니다."""
    g = _instance(add=lambda g, i: g.add((
        i, URIRef("http://elsewhere.org/x#relatedTo"), URIRef("http://elsewhere.org/x#Thing1"),
    )))
    assert check_orphan_nodes(g)["orphan_count"] == 1


def test_empty_graph_passes():
    """typed 인스턴스가 없으면 판정 대상이 없다 (기존 동작 유지)."""
    result = check_orphan_nodes(Graph())
    assert result["passed"] is True and result["orphan_count"] == 0


def test_samples_name_the_offending_entities():
    """운영자가 무엇을 고칠지 알 수 있게 samples 에 엔티티·타입이 담긴다."""
    g = _instance(add=lambda g, i: g.add((i, PROV.wasDerivedFrom, URIRef("prov://row/1"))))
    sample = check_orphan_nodes(g)["samples"][0]
    assert sample["entity"] == "Equipment_EQ001"
    assert sample["type"] == "Equipment"
