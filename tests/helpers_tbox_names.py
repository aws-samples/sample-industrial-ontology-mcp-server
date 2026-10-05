"""배포 T-Box 에서 **이름을 유도**하는 테스트 헬퍼 (하드코딩 금지).

S2 재생성마다 OP/DP 이름이 갈리는데 ``rules/``·테스트는 git 추적이고 T-Box 는
gitignore 라 이름을 박으면 구조적으로 낡는다. 2026-09-05 실측: 창고 방향 테스트
2건이 ``transportationHasOriginWarehouse`` 를 박아 뒀는데 데이터는 정상인데도
(300/300 행 일치) "출발 창고 트리플이 0건" 으로 실패했다.
"""
from __future__ import annotations


def op_for_column(column: str, domain_local: str, range_local: str) -> str:
    """``dcterms:source`` 로 OP 를 해상한다 — 이름을 박지 않는다.

    2026-09-05: 이 테스트들이 ``transportationHasOriginWarehouse`` 를 박아 뒀는데 S2
    세대 교체로 그 OP 가 0행이 되고 실제 데이터는 ``hasOriginWarehouse`` (300 트리플)
    로 갔다. 그래서 "출발 창고 트리플이 0건" 이라고 **데이터가 정상인데** 실패했다
    (실측: 300/300 행이 CSV 와 일치, 뭉개짐 없음).

    이름 대신 (domain, range, source 컬럼) 으로 해상하면 개명에 무관해진다. 후보가
    여럿이면 A-Box 가 실제로 채우는 것을 고른다 — 0행 유령이 섞일 수 있기 때문이다.
    """
    import os

    from rdflib import RDF, RDFS, Graph, Namespace, URIRef
    from rdflib.namespace import OWL

    import config as _cfg
    from domain.namespaces import DOMAIN_NS

    dcterms = Namespace("http://purl.org/dc/terms/")
    tbox = Graph()
    tbox.parse(_cfg.TBOX_PATH, format="turtle")
    ns = str(DOMAIN_NS)
    candidates = []
    for op in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(ns)):
            continue
        doms = {str(d).split("#")[-1] for d in tbox.objects(op, RDFS.domain)}
        rngs = {str(r).split("#")[-1] for r in tbox.objects(op, RDFS.range)}
        srcs = {str(s).strip().upper() for s in tbox.objects(op, dcterms.source)}
        if domain_local in doms and range_local in rngs and column.upper() in srcs:
            candidates.append(str(op).split("#")[-1])
    assert candidates, (
        f"{domain_local} → {range_local} 에서 {column} 를 주장하는 OP 가 T-Box 에 없다"
    )
    if len(candidates) == 1 or not os.path.exists(_cfg.ABOX_PATH):
        return sorted(candidates)[0]
    from domain.tbox_utils import _new_graph, fast_parse_turtle

    abox = _new_graph()
    fast_parse_turtle(abox, _cfg.ABOX_PATH)
    populated = [
        name for name in sorted(candidates)
        if next(abox.triples((None, URIRef(ns + name), None)), None) is not None
    ]
    assert populated, (
        f"{column} 를 주장하는 OP {sorted(candidates)} 중 A-Box 를 채우는 것이 없다"
    )
    return populated[0]


