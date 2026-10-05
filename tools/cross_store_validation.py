"""Multi-Store Cross-Validation — P4.

동일한 의미의 질의를 여러 스토어(merge, inferred, Neo4j LPG)로
병렬 실행하고 결과 집합을 비교하여 변환 손실/불일치를 탐지한다.

차등 테스트(differential testing) 원리: 같은 진실을 묻는 쿼리는 스토어가
달라도 결과가 일치해야 한다. 분기 = 변환 버그/손실 가능성.

고정 질의 초기 5개:
- class_count: 클래스별 인스턴스 수
- op_count: ObjectProperty별 트리플 수
- master_cardinality: Master 인스턴스 총합
- dp_coverage: DatatypeProperty 사용 건수
- orphan_count: 타입 없는 URI 수

Neo4j는 NEO4J_URI/USER/PASSWORD env 설정 시에만 비교. 미설정이면
"neo4j_skipped"로 기록.
"""
from __future__ import annotations

import json
import logging
import os

from config import GENERATED_REPORTS_DIR, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from domain.namespaces import prepend_prefixes
from tools.common import success_response

logger = logging.getLogger(__name__)


# 질의 정의: 각 스토어별로 "같은 의미"의 쿼리를 매핑.
# 결과 형식: 스칼라 카운트 1개. 분기 비교는 수치 일치만 확인.
_QUERIES: list[dict] = [
    {
        "id": "total_triples",
        "description": "전체 트리플 수",
        "sparql": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
        "cypher": "MATCH (n) OPTIONAL MATCH (n)-[r]->() RETURN count(n) + count(r) AS n",
    },
    {
        "id": "class_count",
        "description": "rdfs:Class 선언 수 (OWL Class 포함)",
        "sparql": (
            "SELECT (COUNT(DISTINCT ?c) AS ?n) WHERE { "
            "  { ?c a owl:Class } UNION { ?c a rdfs:Class } "
            "}"
        ),
        "cypher": None,  # LPG에 직접 매핑 어려움 — skip
    },
    {
        "id": "op_count",
        "description": "ObjectProperty 선언 수",
        "sparql": (
            "SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE { ?p a owl:ObjectProperty }"
        ),
        "cypher": None,
    },
    {
        "id": "typed_instance_count",
        "description": "rdf:type 선언된 steel 인스턴스 수",
        "sparql": (
            "SELECT (COUNT(DISTINCT ?i) AS ?n) WHERE { "
            "  ?i a ?c . "
            "  FILTER(isIRI(?i)) "
            "  FILTER(STRSTARTS(STR(?i), \"" + os.environ.get("DOMAIN_INST_NS_OBJ", "") + "\")) "
            "}"
        ),
        "cypher": "MATCH (n) WHERE n.uri STARTS WITH $prefix RETURN count(DISTINCT n) AS n",
    },
    {
        "id": "op_triples",
        "description": "ObjectProperty 트리플 수 (IRI→IRI)",
        "sparql": (
            "SELECT (COUNT(*) AS ?n) WHERE { "
            "  ?s ?p ?o . "
            "  FILTER(isIRI(?s) && isIRI(?o)) "
            "}"
        ),
        "cypher": "MATCH ()-[r]->() RETURN count(r) AS n",
    },
]


def _run_sparql_count(source: str, query: str) -> int | None:
    from tools.sparql_local import _get_graph
    try:
        g, _ = _get_graph(source)
        full = prepend_prefixes(query)
        rows = list(g.query(full))
        if not rows:
            return 0
        first = rows[0]
        val = first[0] if hasattr(first, "__getitem__") else None
        if val is None:
            return 0
        try:
            return int(val.toPython())
        except Exception:
            return int(str(val))
    except Exception as e:
        logger.warning("SPARQL 실행 실패 (%s): %s", source, e)
        return None


def _run_cypher_count(query: str, params: dict | None = None) -> int | None:
    if not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD):
        return None
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None
    try:
        with GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)) as drv, drv.session() as sess:
            rec = sess.run(query, params or {}).single()
            if rec is None:
                return 0
            return int(rec["n"])
    except Exception as e:
        logger.warning("Cypher 실행 실패: %s", e)
        return None


def _compare_row(query: dict) -> dict:
    """단일 질의를 3~4 스토어에서 실행하고 값 비교."""
    results: dict[str, int | None] = {}
    results["sparql_merge"] = _run_sparql_count("merge", query["sparql"])
    # inferred는 all_inferred.ttl 존재 시에만
    from config import INFERRED_PATH
    if os.path.exists(INFERRED_PATH):
        results["sparql_inferred"] = _run_sparql_count("inferred", query["sparql"])
    else:
        results["sparql_inferred"] = None

    if query.get("cypher"):
        from domain.namespaces import DOMAIN_INST_NS
        params = {"prefix": DOMAIN_INST_NS}
        results["neo4j"] = _run_cypher_count(query["cypher"], params)
    else:
        results["neo4j"] = None

    # 분기 판정: non-None 값들 중 최대/최소 비율
    vals = [v for v in results.values() if isinstance(v, int)]
    if len(vals) < 2:
        status = "insufficient_stores"
        max_diff_pct = None
    else:
        lo, hi = min(vals), max(vals)
        if hi == 0:
            max_diff_pct = 0.0
        else:
            max_diff_pct = round((hi - lo) / hi * 100, 2)
        # 동일 의미 쿼리라도 inferred는 merge보다 클 수 있음 (합리적)
        # 5% 초과 분기만 divergent로 분류 (merge vs inferred는 별도 기준 필요)
        if max_diff_pct == 0:
            status = "identical"
        elif max_diff_pct <= 5:
            status = "near_identical"
        else:
            status = "divergent"

    return {
        "id": query["id"],
        "description": query["description"],
        "results": results,
        "status": status,
        "max_diff_pct": max_diff_pct,
    }


def run_cross_store_validation(include_neo4j: bool = True) -> dict:
    comparisons = [_compare_row(q) for q in _QUERIES]
    total = len(comparisons)
    identical = sum(1 for c in comparisons if c["status"] == "identical")
    near = sum(1 for c in comparisons if c["status"] == "near_identical")
    divergent = sum(1 for c in comparisons if c["status"] == "divergent")
    insuff = sum(1 for c in comparisons if c["status"] == "insufficient_stores")

    summary = {
        "total_queries": total,
        "identical": identical,
        "near_identical": near,
        "divergent": divergent,
        "insufficient_stores": insuff,
        "agreement_rate": round(
            (identical + near) / max(total - insuff, 1) * 100, 1,
        ) if (total - insuff) > 0 else None,
    }
    return {"summary": summary, "comparisons": comparisons}


def verify_cross_store_parity() -> str:
    """SPARQL(merge) / SPARQL(inferred) / Neo4j 3개 스토어 동일-의미 쿼리 비교 (P4).

    고정 질의 5종을 각 스토어에서 실행하고 스칼라 결과를 비교. 분기율 5% 초과
    시 divergent로 분류. Neo4j 미설정 시 자동 skip.

    예상 소요시간: 5~30초. Bedrock 호출 0회.
    """
    report = run_cross_store_validation()
    # 저장
    try:
        os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
        with open(os.path.join(GENERATED_REPORTS_DIR, "cross_store_parity.json"),
                  "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("cross_store_parity 저장 실패: %s", e)
    return success_response(report)
