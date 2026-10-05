"""로컬 SPARQL 쿼리 도구

all_inferred.ttl (또는 T-Box + A-Box + 암묵지)을 rdflib/Oxigraph 로 인-프로세스
로드하여 외부 SPARQL 엔드포인트 없이 로컬에서 쿼리를 실행한다.

첫 호출 시 그래프를 로드하고 모듈 레벨에 캐시하여
이후 호출에서는 로드 없이 즉시 쿼리를 실행한다.
"""
from __future__ import annotations

import json
import logging
import os
import time

from rdflib import Graph

from config import INFERRED_PATH
from domain.namespaces import prepend_prefixes as _prepend_prefixes
from domain.sparql_templates import format_sparql_results as _format_results
from domain.sparql_templates import reject_sparql_egress
from tools.common import error_response

logger = logging.getLogger(__name__)


def _reject_query_egress(query: str) -> None:
    """로컬 그래프 밖으로 요청할 수 있는 SPARQL 구문을 거부한다.

    rdflib SPARQL 파서의 구문 트리에서 SERVICE, FROM/FROM NAMED, LOAD,
    USING 노드를 찾는다 (``domain.sparql_templates.reject_sparql_egress``).
    해석할 수 없는 텍스트도 거부한다. 호출자는 PREFIX 를 붙인 뒤 실제로 실행할
    최종 문자열을 넘긴다.

    Raises:
        ValueError: egress 구문이 있거나 텍스트를 해석할 수 없을 때.
    """
    reject_sparql_egress(query)


def _get_graph(source: str = "inferred") -> tuple[Graph, str]:
    """캐시된 그래프를 반환하거나, 없으면 로드한다.

    캐시는 domain.tbox_utils.load_graph() 내부에서 mtime 기반으로 관리.

    Args:
        source: "inferred" (all_inferred.ttl) 또는 "merge" (T-Box + A-Box + tacit 병합)

    Returns:
        (Graph, 로드 메시지)
    """
    start = time.monotonic()

    from domain.tbox_utils import load_graph
    use_inferred = source == "inferred"
    g, tacit_count = load_graph(use_inferred=use_inferred)

    duration = round(time.monotonic() - start, 1)

    if use_inferred and os.path.exists(INFERRED_PATH):
        loaded = "all_inferred.ttl 로드"
    else:
        loaded = "T-Box + A-Box + tacit 병합"

    return g, f"{loaded} ({len(g):,} triples, {duration}s)"



def sparql_local(query: str, source: str = "inferred") -> str:
    """로컬 그래프에서 SPARQL SELECT 쿼리를 실행한다.

    외부 SPARQL 엔드포인트 없이 all_inferred.ttl 을 rdflib 로 로드하여 쿼리.
    첫 호출 시 로드 후 캐시되어 이후 호출은 즉시 실행.

    Args:
        query: SPARQL SELECT 쿼리. PREFIX 생략 시 표준 PREFIX 자동 추가.
        source: "inferred" (추론 결과, 기본값) 또는 "merge" (T-Box+A-Box+tacit 병합).
    """
    try:
        # 가드는 그래프 로드 전에, 실제로 실행할 최종 문자열에 적용한다.
        full_query = _prepend_prefixes(query)
        _reject_query_egress(full_query)
        g, load_msg = _get_graph(source)

        start = time.monotonic()
        results = g.query(full_query)
        duration = round(time.monotonic() - start, 3)

        if results.type == "SELECT":
            rows = _format_results(results)
            return json.dumps({
                "success": True,
                "load": load_msg,
                "query_time_seconds": duration,
                "count": len(rows),
                "vars": [str(v) for v in results.vars],
                "results": rows,
            }, ensure_ascii=False, indent=2)
        elif results.type == "ASK":
            return json.dumps({
                "success": True,
                "load": load_msg,
                "query_time_seconds": duration,
                "result": bool(results),
            }, ensure_ascii=False, indent=2)
        elif results.type == "CONSTRUCT":
            ttl_result = results.serialize(format="turtle")
            if isinstance(ttl_result, bytes):
                ttl_result = ttl_result.decode("utf-8")
            return json.dumps({
                "success": True,
                "load": load_msg,
                "query_time_seconds": duration,
                "triples": len(results),
                "turtle": ttl_result,
            }, ensure_ascii=False, indent=2)
        else:
            return json.dumps({
                "success": True,
                "load": load_msg,
                "query_time_seconds": duration,
                "result_type": str(results.type),
            }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def sparql_local_reload(source: str = "inferred") -> str:
    """로컬 SPARQL 그래프 캐시를 강제 리로드한다.

    T-Box/A-Box/tacit/추론 결과가 변경된 후 호출하면 최신 데이터로 갱신.

    Args:
        source: "inferred" 또는 "merge".
    """
    from domain.tbox_utils import invalidate_graph_cache
    invalidate_graph_cache()

    g, load_msg = _get_graph(source)
    return json.dumps({
        "success": True,
        "message": f"리로드 완료: {load_msg}",
    }, ensure_ascii=False, indent=2)
