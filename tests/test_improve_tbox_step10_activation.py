"""Step 10 활성화 회귀 테스트.

배경: golden set drift 조사에서 드러난 ``UnboundLocalError`` latent bug.
본문 Step 15 의 함수 스코프
``from config import SOURCE_RAWDATA_DIR`` 때문에 Step 10 이 silent fail 했고,
P0-1 Phase 4/5 에서 step 모듈 추출 후 정상 활성화. 본 테스트는 재발 방지.
"""
from __future__ import annotations

import inspect
import textwrap

from rdflib import Graph

from domain.namespaces import DOMAIN_NS


def _new_graph() -> Graph:
    return Graph()


def test_sourcerawdata_dir_not_reimported_in_improve_tbox():
    """원인 A 재발 방지 — improve_tbox 내 ``from config import SOURCE_RAWDATA_DIR`` 0건."""
    from tools import ontology_quality as oq

    source = inspect.getsource(oq.improve_tbox)
    assert "from config import SOURCE_RAWDATA_DIR" not in source, (
        "improve_tbox 내 SOURCE_RAWDATA_DIR 재 import 가 UnboundLocalError 를 유발함. "
        "module-level import (line 40) 만 사용하거나, 해당 step 을 별도 모듈로 추출하세요."
    )


def test_step_10_runs_without_unbound_local(caplog):
    """Step 10 실행 시 ``UnboundLocalError`` warning 발생하지 않음."""
    from tools.ontology_quality import improve_tbox

    tbox = textwrap.dedent(f"""\
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:ItemMaster a owl:Class ;
            rdfs:label "Item"@en, "품목"@ko ;
            rdfs:comment "품목"@ko .
        """)
    with caplog.at_level("WARNING"):
        result_ttl, _ = improve_tbox(tbox)
    assert all(
        "공유 FK domain 확장 실패" not in rec.message
        and "SOURCE_RAWDATA_DIR" not in rec.message
        for rec in caplog.records
    ), "Step 10 이 UnboundLocalError 로 silent fail 함"


def test_step_10_measures_shared_fk_without_destroying_domain(tmp_path, monkeypatch):
    """Step 10 이 공유 FK 를 **측정** 하되 도메인을 파괴하지 않는다.

    2026-08-11 계약 변경: 예전에는 CSV 3개 이상이 공유하는 FK 를 잇는 OP 의
    ``rdfs:domain`` 을 ``owl:Thing`` 으로 확장했다. 그 확장은 **이득 0**
    (A-Box 재생성 결과 완전 동일 — 생성기가 ``owl:Thing`` domain 을 읽지 못한다)
    이면서 손해가 셋이었다: conformance 검사가 그 OP 를 통째로 건너뛰어 range
    검사까지 잃고(A-Box 12,846 트리플 무검사), ``inverseOf`` 가 없는 OP 는
    ``step_09a`` 가 복원할 수 없어 domain 이 영구 소실되며, 중복 게이트도 시야를
    잃는다. 그래서 측정만 남기고 확장을 제거했다.

    이 테스트가 고정하는 것: **측정은 계속 동작하고**(스텝이 silent fail 하지
    않는다는 이 파일의 원래 목적) **domain 은 보존된다**.
    """
    import tools.ontology_quality as oq

    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    # ProductMaster 를 FK target 으로 갖는 3개 CSV (3개 미만이면 broaden 안 함)
    (csv_dir / "Process_A.csv").write_text("Product_ID\nP001\n")
    (csv_dir / "Process_B.csv").write_text("Product_ID\nP002\n")
    (csv_dir / "Process_C.csv").write_text("Product_ID\nP003\n")
    (csv_dir / "Product_Master.csv").write_text("Product_ID\nP001\n")
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(csv_dir))

    tbox = textwrap.dedent(f"""\
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:ProductMaster a owl:Class ;
            rdfs:label "PM"@en, "제품"@ko ;
            rdfs:comment "제품"@ko .

        steel:ProcessA a owl:Class ;
            rdfs:label "PA"@en, "공정 A"@ko ;
            rdfs:comment "공정 A"@ko .

        steel:hasProductMaster a owl:ObjectProperty ;
            rdfs:domain steel:ProcessA ;
            rdfs:range steel:ProductMaster ;
            rdfs:label "has product"@en, "제품 보유"@ko ;
            rdfs:comment "제품 보유"@ko .
        """)
    result_ttl, stats = oq.improve_tbox(tbox)

    # (1) 측정은 동작한다 — 이 파일의 원래 목적(step_10 이 silent fail 하지 않음).
    assert stats.get("shared_fk_ops_measured", 0) >= 1, (
        "Step 10 이 공유 FK 를 측정하지 못했다 (silent fail 재발?). "
        f"stats={ {k: v for k, v in stats.items() if 'shared_fk' in k} }"
    )
    # (2) 확장은 하지 않는다 — 카운터가 0 이어야 한다.
    assert stats.get("domain_broadened", 0) == 0, (
        "Step 10 이 domain 을 확장했다 (폐기된 동작). "
        f"domain_broadened={stats.get('domain_broadened')}"
    )
    # (3) THE PRESERVATION: concrete domain 이 그대로 남아야 한다.
    from rdflib import OWL, RDFS, URIRef

    g = _new_graph()
    g.parse(data=result_ttl, format="turtle")
    prop = URIRef(DOMAIN_NS + "hasProductMaster")
    domains = set(g.objects(prop, RDFS.domain))
    assert OWL.Thing not in domains, "concrete domain 을 owl:Thing 으로 바꿨다"
    assert URIRef(DOMAIN_NS + "ProcessA") in domains, (
        f"원래 domain 을 잃었다: {sorted(str(d) for d in domains)}"
    )
