"""Step 10 이 ``rdfs:domain`` 을 파괴하지 않고 측정만 하는지 고정.

## 왜

예전 step_10 은 3개 이상 테이블이 공유하는 FK 를 잇는 OP 의 ``rdfs:domain`` 을
``owl:Thing`` 으로 바꿨다. 2026-08-11 실측으로 그 확장이 **이득 0 / 손해 3** 임이
확인됐다:

- **이득 0**: read-only 화 후 A-Box 재생성 결과가 완전 동일 (인스턴스 70,701 /
  OP 트리플 89,494). A-Box 생성기가 ``owl:Thing`` domain 을 읽지 못해 애초에
  쓰지 않는다.
- **손해 1**: 이 스텝이 ``owl:Thing`` domain 의 **유일한 출처** (스텝별 추적 0→49).
- **손해 2**: ``surfaceQualityHasProduct`` / ``wasteGeneratedByEquipment`` /
  ``ghgEmissionOfEquipment`` 는 ``owl:inverseOf`` 가 없어 ``step_09a`` 가 복원할 수
  없다 — **영구 손실**이었다.
- **손해 3**: conformance 검사와 중복 게이트가 ``owl:Thing`` domain OP 를 집계에서
  빼 A-Box 트리플 12,846건이 무검사였고 중복이 과소 집계됐다.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.quality_steps._base import StepContext
from tools.quality_steps.step_10_shared_fk_op_domain import apply

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


@pytest.fixture
def shared_fk_csvs(monkeypatch):
    """3개 테이블이 같은 FK(``Item_Code``) 를 쓰는 최소 CSV 세트."""
    import config
    from tools import ontology_quality as oq

    tmp = tempfile.mkdtemp()
    for table in ("Order_Head", "Shipment", "Stock_Level"):
        Path(tmp, f"{table}.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    Path(tmp, "Item_Master.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", tmp, raising=False)
    return tmp


def _graph_with_shared_fk_op() -> Graph:
    g = Graph()
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:OrderHead a owl:Class .\n"
        f"{NS_PREFIX}:ItemMaster a owl:Class .\n"
        f"{NS_PREFIX}:hasItemMaster a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:OrderHead ; rdfs:range {NS_PREFIX}:ItemMaster .\n"
    ), format="turtle")
    return g


# ── THE REGRESSION: domain 을 파괴하지 않는다 ─────────────────────────


def test_step_10_never_replaces_a_concrete_domain_with_owl_thing(shared_fk_csvs):
    """핵심 회귀: concrete domain 을 ``owl:Thing`` 으로 바꾸면 안 된다.

    복원 경로(``step_09a``)는 ``owl:inverseOf`` 가 있어야 동작하므로, inverse 가
    없는 OP 는 이 확장으로 domain 을 **영구히** 잃는다.
    """
    g = _graph_with_shared_fk_op()
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    domains = set(g.objects(D("hasItemMaster"), RDFS.domain))
    assert OWL.Thing not in domains, "concrete domain 을 owl:Thing 으로 바꿨다"
    assert D("OrderHead") in domains, f"원래 domain 을 잃었다: {domains}"


def test_step_10_is_read_only(shared_fk_csvs):
    """그래프를 전혀 바꾸지 않는다 — ``triples_delta`` 가 0 이어야 한다."""
    g = _graph_with_shared_fk_op()
    snapshot = set(g)
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert set(g) == snapshot, "read-only 스텝이 그래프를 수정했다"
    assert res.triples_delta == 0


def test_step_10_still_measures_the_shared_fk_fact(shared_fk_csvs):
    """PRESERVATION of purpose: 공유 FK 사실은 계속 측정·보고해야 한다.

    측정을 잃으면 "3개 이상 테이블이 공유하는 FK" 라는 신호가 사라져 왜 이
    스텝이 있는지 알 수 없게 된다.
    """
    g = _graph_with_shared_fk_op()
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert res.stats["shared_fk_ops_measured"] == 1, (
        f"공유 FK 측정을 잃었다: {res.stats}"
    )
    assert res.stats["domain_broadened"] == 0, "확장 카운터가 0 이 아니다"
    assert any("hasItemMaster" in s for s in res.stats["shared_fk_op_samples"])


def test_step_10_ignores_fk_shared_by_fewer_than_three_tables(monkeypatch):
    """PRESERVATION: 3개 미만 공유는 측정 대상이 아니다 (기존 임계값 유지)."""
    import config
    from tools import ontology_quality as oq

    tmp = tempfile.mkdtemp()
    Path(tmp, "Order_Head.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    Path(tmp, "Item_Master.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", tmp, raising=False)

    g = _graph_with_shared_fk_op()
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert res.stats["shared_fk_ops_measured"] == 0


def test_s3_output_has_no_owl_thing_domain_ops(s3_output_ttl):
    """실측 고정: S3 산출물에 ``owl:Thing`` domain OP 가 없어야 한다.

    이 스텝이 유일한 출처였으므로 read-only 화 후 0 이어야 한다. 0 이 아니면
    다른 스텝이 새로 만들기 시작했다는 뜻이고, 그러면 conformance 검사와 중복
    게이트가 다시 시야를 잃는다.
    """
    if s3_output_ttl is None:
        pytest.skip("S2 초안 픽스처 없음")
    g = Graph()
    g.parse(data=s3_output_ttl, format="turtle")
    thing_ops = sorted(
        str(o).split("#")[-1] for o in g.subjects(RDF.type, OWL.ObjectProperty)
        if str(o).startswith(DOMAIN_NS) and OWL.Thing in set(g.objects(o, RDFS.domain))
    )
    assert thing_ops == [], f"owl:Thing domain OP 가 생겼다: {thing_ops[:8]}"


def test_ops_without_inverse_keep_their_domain(s3_output_ttl):
    """복원 경로가 없는 OP 의 domain 이 보존되는지 실측으로 고정.

    ``surfaceQualityHasProduct`` / ``wasteGeneratedByEquipment`` /
    ``ghgEmissionOfEquipment`` 는 ``owl:inverseOf`` 가 없어 step_09a 가 복원하지
    못한다 — 이 스텝이 확장하면 영구 손실이었다.
    """
    if s3_output_ttl is None:
        pytest.skip("S2 초안 픽스처 없음")
    g = Graph()
    g.parse(data=s3_output_ttl, format="turtle")
    for local in ("surfaceQualityHasProduct", "wasteGeneratedByEquipment",
                  "ghgEmissionOfEquipment"):
        uri = D(local)
        if (uri, RDF.type, OWL.ObjectProperty) not in g:
            continue                      # 도메인 설정이 다르면 없을 수 있다
        domains = set(g.objects(uri, RDFS.domain))
        assert OWL.Thing not in domains, f"{local} 의 domain 이 owl:Thing 이 됐다"
        assert domains, f"{local} 이 domain 을 잃었다"
