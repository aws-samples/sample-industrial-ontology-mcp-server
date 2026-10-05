"""도메인 질의 테스트 도구 — CQ 기반 KG 연결성 검증

Competency Question(CQ)의 도메인 클래스에 인스턴스가 존재하고,
클래스 간 ObjectProperty 연결 경로가 있는지 프로그래매틱으로 검증한다.
Zero-Graph-Load 아키텍처로 abox_stats.json + 시맨틱 딕셔너리 기반 분석.

파이프라인 위치: S11_DICT_VALIDATE → **S12_QUERY_TEST** → S13_REPORT
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import webbrowser

from config import (
    COMPETENCY_QUESTIONS_PATH,
    SEMANTIC_DICT_PATH,
    SOURCE_QUERY_TESTS_DIR,
    resolve_generated_path,
)
from domain.namespaces import prepend_prefixes
from domain.tbox_utils import _new_graph
from tools.common import error_response, resolve_child_path

logger = logging.getLogger(__name__)


_TABLE_CLASS_MAP: dict[str, str] | None = None
_TABLE_CLASS_MAP_MTIME: float = 0.0


def _mapping_mtime() -> float:
    """table_class_mapping.json 의 mtime (없으면 0.0)."""
    from domain.table_mapping import mapping_mtime
    return mapping_mtime()


def _load_table_class_map() -> dict[str, str]:
    """Load rules/domain/table_class_mapping.json (CSV table name → domain class).

    The authoritative CSV-table→class mapping is produced by the A-Box
    generator and shared by CQ validators. The S12 query test must use the
    same map, otherwise CQ `domains` (which hold raw CSV table names
    such as ``SOURCE_TABLE_001``) never resolve to the semantic class names
    (``OrderClass``) recorded in abox_stats/per_class and the semantic
    dictionary — yielding false "0 instances" failures.

    Mapping values may carry a prefix (``<ns>:OrderClass``); the prefix is
    stripped so the local name matches per_class keys.

    Cached with **mtime invalidation** — the A-Box generator's loaders do the
    same. A permanent per-process cache meant editing the mapping mid-session
    changed what S7 saw but not what S12 saw, pinning ``pass_rate 0%`` until the
    server restarted (2026-08-08 규명).
    """
    global _TABLE_CLASS_MAP, _TABLE_CLASS_MAP_MTIME
    mtime = _mapping_mtime()
    if _TABLE_CLASS_MAP is not None and mtime == _TABLE_CLASS_MAP_MTIME:
        return _TABLE_CLASS_MAP

    from domain.table_mapping import load_table_class_mapping
    mapping = load_table_class_mapping()
    _TABLE_CLASS_MAP = mapping
    _TABLE_CLASS_MAP_MTIME = mtime
    return mapping


def _op_is_live(op_info: dict | None) -> bool:
    """이 ObjectProperty 가 A-Box 에 값을 갖는가 (판정 불가면 산 것으로 취급).

    ## 왜 필요한가 — 통과율이 선언 개수로 올라갔다

    5b 연결성 체크(``_find_connecting_op``)는 ``rdfs:domain``/``rdfs:range`` 만 보고
    ``is_populated``/``triple_count`` 를 **한 번도 읽지 않는다**. 그래서 A-Box 트리플
    0건인 OP 를 T-Box 에 선언하기만 하면 CQ 통과가 뒤집힌다. 2026-08-30 반대실험::

        현 딕셔너리(121 OP)                        8/12 = 66.7%
        + 0행 OP ``blastFurnaceProcessUsesTag``    9/12 = 75.0%   ← 데이터 증가 0

    배포 이력에서 실제로 일어났다 — 8/30 baseline(기록 75%)의 CQ11 은
    ``planFulfilledByResult``(0행) → ``inventoryTransactionRelatedProductionPlan``(0행)
    즉 **0행 × 0행 멀티홉**으로 통과했다. 즉 58.3 → 66.7 → 75% 개선 궤적이 상당 부분
    0행 OP 선언 증가였다.

    현 딕셔너리에도 ``is_populated=False`` 인 OP 가 13개 있고 그중 2개가 쌍 판정을
    떠받친다.

    ## ``is not False`` 여야 한다

    ``.get("is_populated")`` 를 truthy 로 보면 **키가 없는 딕셔너리에서 전 OP 가 죽은
    것으로 판정된다** — S6.5 v1 딕셔너리(``include_stats=False``)와 테스트 픽스처가
    바로 그 형태다. 그러면 파이프라인이 통째로 실패한다. 키 부재는 "통계 없음 =
    판정 불가" 이고, 판정 불가는 산 것으로 취급해야 한다 (이 리포의 "미측정 ≠ 값 0").
    """
    if not isinstance(op_info, dict):
        return True
    return op_info.get("is_populated") is not False


def _domain_to_class_name(domain: str) -> str:
    """Convert a CQ domain token (CSV table name) → domain class name.

    Resolution order:
      1. rules/domain/table_class_mapping.json — authoritative CSV-table→class map
         shared with the A-Box generator and CQ validators.
      2. PascalCase fallback — for tokens absent from the map. Preserves
         all-uppercase tokens (acronyms) such as GHG, NDT, OWL, which
         ``str.capitalize()`` would lowercase (GHG → Ghg). Matches the names
         emitted by the T-Box generator, which keeps CSV header casing intact.

        SOURCE_TABLE_001                 → OrderClass    (via mapping)
        ghg_emission                     → GhgEmission   (fallback)
        GHG_Emission                     → GHGEmission   (fallback)
    """
    mapped = _load_table_class_map().get(domain)
    if mapped:
        return mapped
    return "".join((p[:1].upper() + p[1:]) if p else "" for p in domain.split("_"))


#: SPARQL 의 ``prefix:local`` 자리에 보간할 수 있는 로컬 이름 형식.
#: 단어 문자로 시작하고 단어 문자와 ``-`` 만 포함한다. 공백, 괄호, ``<`` ``>``,
#: ``.`` ``;`` ``#``, 따옴표처럼 질의 구조를 바꾸는 문자는 들어갈 수 없다.
_LOCAL_NAME_RE = re.compile(r"\w[\w-]*")


def _is_local_name(name: object) -> bool:
    """SPARQL 보간에 안전한 로컬 이름인지 (전체 일치) 판정한다."""
    return isinstance(name, str) and _LOCAL_NAME_RE.fullmatch(name) is not None


def _require_local_names(*names: object) -> None:
    """보간할 식별자가 모두 로컬 이름 형식인지 확인한다.

    Raises:
        ValueError: 형식이 아닌 이름이 하나라도 있을 때.
    """
    invalid = [name for name in names if not _is_local_name(name)]
    if invalid:
        raise ValueError(
            "SPARQL 로컬 이름 형식이 아닌 식별자: "
            + ", ".join(repr(str(name)[:80]) for name in invalid)
        )


def _run_local_query(graph, query: str):
    """최종 질의 문자열에 egress 가드를 적용한 뒤 로컬 그래프에서 실행한다.

    이 모듈의 모든 ``graph.query`` 호출은 이 함수를 거친다.
    """
    from tools.sparql_local import _reject_query_egress

    _reject_query_egress(query)
    return graph.query(query)


def _resolve_cq_class_names(domains) -> tuple[list[str], list[dict]]:
    """CQ domains 를 클래스 로컬 이름으로 바꾼다.

    로컬 이름 형식이 아닌 값은 SPARQL 에 넣지 않고 실패 체크로 기록한다.

    Returns:
        ``(유효한 클래스 이름 목록, 거부된 도메인의 실패 체크 목록)``
    """
    names: list[str] = []
    rejected: list[dict] = []
    for domain in domains:
        cls_name = _domain_to_class_name(domain) if isinstance(domain, str) else None
        if _is_local_name(cls_name):
            names.append(cls_name)
            continue
        rejected.append({
            "check": "CQ 도메인 이름 형식 확인",
            "passed": False,
            "detail": (
                "SPARQL 로컬 이름이 아니어서 질의에서 제외: "
                f"{str(domain)[:80]!r}"
            ),
            "_invalid_domain": True,
        })
    return names, rejected


_CQ_PATH = COMPETENCY_QUESTIONS_PATH


def _verify_class_join(
    graph,
    class_a: str,
    op_name: str,
    class_b: str,
    *,
    ns_prefix: str | None = None,
    ns_uri: str | None = None,
    max_hops: int = 2,
) -> dict:
    """R11-M6: 두 클래스가 실제로 KG 에서 연결됐는지 4-단계 fallback 로 판정.

    CQ 가 명시한 OP 이름과 방향이 A-Box 의 실제 트리플 방향과 어긋날 수 있음
    (abox_generation 의 OP 선택 로직과 CQ 의 화살표가 독립적으로 설계된 결과).
    또한 CSV 원본에 두 클래스 간 직접 FK 가 없고 중간 클래스를 경유해야
    연결되는 경우도 있음 (e.g., AlarmEvents → TagMaster → EquipmentStatus).
    fallback 시도 (먼저 매치하는 단계에서 종료):

      1. 원래 방향: ?x a A ; :OP ?y . ?y a B
      2. 역방향: ?x a B ; :OP ?y . ?y a A
      3. any-path: ?x a A . ?y a B . { ?x ?p ?y } UNION { ?y ?p ?x }
      4. 2-hop: ?x a A . ?y a B . ?x-?mid-?y 또는 ?y-?mid-?x (max_hops=2, 기본)

    Args:
        max_hops: 2-hop fallback 활성화 여부 (1 = 비활성, 2 = 활성 기본).

    Args:
        graph: rdflib Graph.
        class_a: 소스 클래스 (로컬 이름).
        op_name: CQ 가 명시한 OP 로컬 이름 (힌트 — any_path 에선 무시).
        class_b: 타깃 클래스 (로컬 이름).
        ns_prefix: SPARQL PREFIX 이름 (기본: NS_PREFIX).
        ns_uri: 해당 namespace URI (기본: DOMAIN_NS).

    Returns:
        {"passed": bool, "direction": "forward"|"reverse"|"any_path"|"none",
         "count": int, "attempts": [..attempted directions with counts..]}

    Raises:
        ValueError: class_a / op_name / class_b 가 SPARQL 로컬 이름 형식이 아닐 때.
    """
    _require_local_names(class_a, op_name, class_b)
    if ns_prefix is None or ns_uri is None:
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ns_prefix = ns_prefix or NS_PREFIX
        ns_uri = ns_uri or str(DOMAIN_NS)

    attempts: list[dict] = []

    def _run_count(sparql: str) -> int:
        full = f"PREFIX {ns_prefix}: <{ns_uri}>\n" + sparql
        try:
            rows = list(_run_local_query(graph, full))
            return int(rows[0][0]) if rows else 0
        except Exception as e:
            logger.debug("verify_class_join query 실패: %s", e)
            return -1

    # 1. 원래 방향
    cnt = _run_count(
        f"SELECT (COUNT(*) AS ?c) WHERE {{ "
        f"?x a {ns_prefix}:{class_a} ; {ns_prefix}:{op_name} ?y . "
        f"?y a {ns_prefix}:{class_b} }}"
    )
    attempts.append({"direction": "forward", "count": cnt})
    if cnt > 0:
        return {"passed": True, "direction": "forward", "count": cnt, "attempts": attempts}

    # 2. 역방향 (OP 이름은 그대로, subject/object 클래스만 swap)
    cnt = _run_count(
        f"SELECT (COUNT(*) AS ?c) WHERE {{ "
        f"?x a {ns_prefix}:{class_b} ; {ns_prefix}:{op_name} ?y . "
        f"?y a {ns_prefix}:{class_a} }}"
    )
    attempts.append({"direction": "reverse", "count": cnt})
    if cnt > 0:
        return {"passed": True, "direction": "reverse", "count": cnt, "attempts": attempts}

    # 3. any-path — 두 클래스 인스턴스 사이 어떤 OP 든 연결돼 있으면 PASS.
    #
    #    **SPARQL 로 풀지 않는다.** 순진한 패턴
    #        ?x a A . ?y a B . { ?x ?p ?y } UNION { ?y ?p ?x }
    #    은 두 클래스의 **데카르트 곱** 을 후보로 만든다. 2026-07-26 실측:
    #      - 트리플 29,528 개 (MaterialA 약 1.3만 × MaterialB 약 1.6만, 연결 0건) → 151초
    #      - 실제 A-Box 533MB / 1,054만 트리플 → 73분 경과 후에도 미완료.
    #        그 사이 같은 MCP 서버의 다른 도구까지 전부 블로킹됐다.
    #    LIMIT 1 로 바꿔도 **연결이 없는 경우** 는 전 조합을 확인해야 하므로
    #    개선되지 않는다 (최악 경로가 곧 실패 판정 경로다).
    #
    #    대신 rdflib 인덱스를 직접 쓴다: 두 클래스의 인스턴스 집합을 한 번씩
    #    훑어 만들고(각 O(n)), 작은 쪽 인스턴스의 실제 이웃만 확인한다. 이웃 수는
    #    인스턴스당 몇 개 수준이라 전체가 O(n + 간선수) 로 끝난다.
    exists, sample = _exists_direct_link(graph, class_a, class_b, ns_uri)
    attempts.append({"direction": "any_path", "count": 1 if exists else 0})
    if exists:
        return {
            "passed": True, "direction": "any_path", "count": 1,
            "via": sample, "attempts": attempts,
        }

    # 4. 2-hop — 중간 클래스 경유. 직접 FK 없어도 간접 연결 가능하면 PASS.
    #   존재 여부만 확인 (ASK + 4방향 UNION 을 각각 LIMIT 1).
    #   대용량 그래프에서 COUNT 전체 조인이 너무 비싸므로 exists-shortcut 접근.
    if max_hops >= 2:
        exists = _run_exists_2hop(
            graph, class_a, class_b, ns_prefix, ns_uri
        )
        attempts.append({"direction": "2_hop", "count": 1 if exists else 0})
        if exists:
            return {"passed": True, "direction": "2_hop", "count": 1, "attempts": attempts}

    return {"passed": False, "direction": "none", "count": 0, "attempts": attempts}


def _exists_direct_link(graph, class_a: str, class_b: str, ns_uri: str):
    """두 클래스 인스턴스 사이에 직접 연결(어느 방향이든)이 있는지 판정.

    SPARQL 데카르트 곱을 피하기 위해 rdflib 인덱스를 직접 순회한다:

      1. 각 클래스의 인스턴스 집합 수집 — ``triples((None, RDF.type, cls))``,
         각 O(해당 클래스 인스턴스 수).
      2. **작은 쪽** 집합의 각 인스턴스에서 나가는/들어오는 트리플만 확인.
         인스턴스당 이웃은 보통 수십 개 이하라 전체가 O(n + 간선수) 다.

    Args:
        graph: rdflib Graph.
        class_a / class_b: 클래스 로컬 이름.
        ns_uri: 도메인 네임스페이스 URI.

    Returns:
        ``(존재 여부, 예시 프로퍼티 local name 또는 None)``
    """
    from rdflib import RDF as _RDF
    from rdflib import URIRef as _URIRef

    ns = ns_uri if ns_uri.endswith(("#", "/")) else ns_uri + "#"
    a_uri, b_uri = _URIRef(ns + class_a), _URIRef(ns + class_b)
    set_a = {s for s in graph.subjects(_RDF.type, a_uri)}
    set_b = {s for s in graph.subjects(_RDF.type, b_uri)}
    if not set_a or not set_b:
        return False, None

    # 작은 쪽을 순회하고 큰 쪽은 멤버십 조회만 한다.
    small, large = (set_a, set_b) if len(set_a) <= len(set_b) else (set_b, set_a)
    for node in small:
        for _s, pred, obj in graph.triples((node, None, None)):
            if obj in large:
                return True, str(pred).rsplit("#", 1)[-1]
        for subj, pred, _o in graph.triples((None, None, node)):
            if subj in large:
                return True, str(pred).rsplit("#", 1)[-1]
    return False, None


def _run_exists(graph, pattern: str, ns_prefix: str, ns_uri: str) -> bool:
    """패턴을 만족하는 해가 **하나라도 있는지** 를 LIMIT 1 로 판정.

    연결 여부만 알면 되는 단계에서 COUNT 대신 쓴다. COUNT 는 전체 해집합을
    세므로 대용량 그래프에서 비용이 폭발한다.
    """
    q = (f"PREFIX {ns_prefix}: <{ns_uri}>\n"
         f"SELECT ?x WHERE {{ {pattern} }} LIMIT 1")
    try:
        return bool(list(_run_local_query(graph, q)))
    except Exception as exc:  # noqa: BLE001 — 검증 실패가 파이프라인을 막지 않는다
        logger.debug("exists query 실패: %s", exc)
        return False


def _run_exists_2hop(graph, class_a: str, class_b: str,
                     ns_prefix: str, ns_uri: str) -> bool:
    """중간 노드 하나를 경유하는 간접 연결이 있는지 판정 (A—M—B, 방향 무관).

    **SPARQL 을 쓰지 않는다.** 4방향 패턴을 SPARQL 로 풀면 자유 변수 3개
    (``?x ?m ?y``) 가 곱해져 비용이 이차 이상으로 폭발한다. 2026-07-26 실측
    (연결 0건 = 최악 경로):

        트리플  1,200 →  0.17초
        트리플  4,500 →  2.75초
        트리플 13,200 → 26.9초        ← 규모의 3배에 시간은 10배
        실제 A-Box 1,054만 → 사실상 완료 불가

    대신 인덱스로 **A 의 이웃 집합** 과 **B 의 이웃 집합** 을 각각 만들고 교집합을
    본다. 교집합이 비지 않으면 그 원소가 중간 노드다. 비용은 O(간선수) 다.

    A/B 자신은 중간 노드에서 제외한다 (원래 SPARQL 의 ``FILTER(?m != ?x && ?m != ?y)``
    와 같은 의미 — 직접 연결은 3단계가 이미 판정했다).

    **``rdf:type`` 은 이웃 간선에서 제외한다.** 타입 트리플을 따라가면 객체가
    *클래스* 이므로 "A 와 B 가 같은 클래스의 인스턴스" 라는 사실이 곧 "연결"
    로 오판된다. 추론 그래프에서는 OWL RL 이 모든 개체에 ``owl:Thing`` 을
    붙이므로 **모든 클래스 쌍이 무조건 PASS** 가 된다 — 게이트가 통째로 무의미
    해진다 (2026-08-08 규명: 연결 0건인 두 클래스가 ``owl:Thing`` 타입만 추가하면
    True 로 뒤집힌다). 간접 연결은 ObjectProperty 로 이어져야 의미가 있다.
    """
    from rdflib import RDF as _RDF
    from rdflib import URIRef as _URIRef

    ns = ns_uri if ns_uri.endswith(("#", "/")) else ns_uri + "#"
    set_a = set(graph.subjects(_RDF.type, _URIRef(ns + class_a)))
    set_b = set(graph.subjects(_RDF.type, _URIRef(ns + class_b)))
    if not set_a or not set_b:
        return False

    def _neighbours(nodes: set) -> set:
        """nodes 의 이웃 (양방향). 리터럴·타입 간선·nodes 자신은 제외."""
        from rdflib import Literal as _Literal

        out: set = set()
        for node in nodes:
            for _s, pred, obj in graph.triples((node, None, None)):
                if pred == _RDF.type:
                    continue          # 클래스는 중간 노드가 아니다 (docstring 참조)
                if not isinstance(obj, _Literal):
                    out.add(obj)
            for subj, pred, _o in graph.triples((None, None, node)):
                if pred == _RDF.type:
                    continue
                out.add(subj)
        return out - nodes

    mids = _neighbours(set_a) - set_b
    if not mids:
        return False
    return bool(mids & (_neighbours(set_b) - set_a))


def _cq_to_test_case(cq: dict) -> dict:
    """Competency Question을 테스트 케이스 형식으로 변환한다.

    CQ의 expected_answer_type에 따라 어설션을 자동 생성:
    - list/trend → min_rows=1 (결과가 반드시 있어야 함)
    - count → min_rows=1, required_vars에 cnt/count 포함
    - comparison/causal → min_rows=1
    """
    answer_type = cq.get("expected_answer_type", "list")
    required_vars: list[str] = []
    if answer_type == "count":
        required_vars = ["cnt", "count"]

    return {
        "id": f"cq_{cq.get('id', 'unknown').lower()}",
        "question": cq.get("question_ko", cq.get("question_en", "")),
        "category": "+".join(cq.get("domains", [])[:3]),
        "difficulty": cq.get("difficulty", "medium"),
        "domains": cq.get("domains", []),
        "golden_sparql": "",
        "assertions": {
            "min_rows": 1,
            "required_vars": required_vars,
            "sparql_must_contain": cq.get("domains", [])[:2],
            "sparql_must_not_contain": ["DELETE", "INSERT"],
        },
    }


def _load_semantic_dict() -> dict:
    """시맨틱 딕셔너리를 로드한다."""
    if not os.path.exists(SEMANTIC_DICT_PATH):
        return {}
    with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_abox_stats() -> dict | None:
    """A-Box 통계 JSON을 로드한다 (generate_abox에서 생성)."""
    from config import GENERATED_ABOX_DIR
    stats_path = os.path.join(GENERATED_ABOX_DIR, "abox_stats.json")
    if not os.path.exists(stats_path):
        return None
    try:
        with open(stats_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


#: 실행 가능한 질의 전문을 담는 필드 이름. ``sparql_must_contain`` 처럼 질의 일부
#: 토큰을 나열하는 어설션 필드는 실행되지 않으므로 여기에 맞지 않아야 한다.
_EXECUTABLE_QUERY_FIELD = re.compile(r"^(?:golden_sparql|sparql|query)$|_(?:sparql|query)$")


def _validate_embedded_queries(value: object, field_name: str = "") -> None:
    """실행 질의 필드에 로컬 그래프 밖 egress 구문이 없는지 재귀 검사한다.

    빈 문자열은 실행되지 않으므로 검사하지 않는다 (``_cq_to_test_case`` 는
    ``golden_sparql`` 을 빈 문자열로 만든다).
    """
    from tools.sparql_local import _reject_query_egress

    if isinstance(value, dict):
        for key, child in value.items():
            _validate_embedded_queries(child, str(key))
        return
    if isinstance(value, list):
        for child in value:
            _validate_embedded_queries(child, field_name)
        return
    if (
        isinstance(value, str)
        and value.strip()
        and _EXECUTABLE_QUERY_FIELD.search(field_name.lower())
    ):
        _reject_query_egress(value)


def test_domain_queries(test_cases_path: str = "", open_report: bool = False,
                        graph_source: str = "merge",
                        verify_sparql: bool = False,
                        verify_joins: bool = True) -> str:
    """로컬 RDF와 통계 파일에 CQ query를 실행하고 HTML 보고서를 작성한다.

    Database query 작업은 로컬 in-memory RDF graph에 대한 read-only SPARQL로
    제한된다. 원격 database나 network service를 호출하지 않는다.

    예상 소요시간: <1초 (그래프 로드 없음, Bedrock 호출 없음)
    verify_sparql=True 시: 로컬 SPARQL로 실제 조인 결과 수 확인 (~수분)
    verify_joins=True 시: 모든 CQ 클래스 쌍에 대해 고정 COUNT SPARQL 실행
        (그래프 1회 로드 + CQ별 조인 COUNT. 대용량 inferred 그래프에선 10~16분 소요 —
        진행 상황은 "CQ N/M 검증 중" 로그로 추적)

    Zero-Graph-Load 아키텍처:
    - 인스턴스 카운트: abox_stats.json 참조 (generate_abox에서 생성)
    - 연결성: T-Box 스키마(시맨틱 딕셔너리) 기반 분석
    - DP 값 존재: abox_stats.json의 OP 트리플 수로 간접 확인
    - SPARQL 그래프 로드 완전 제거 → 데이터 크기에 무관한 O(k) 성능

    Args:
        test_cases_path: data/source/query_tests 아래 CQ JSON 파일명.
        open_report: True면 HTML 보고서를 브라우저에서 자동 오픈. 기본값은 False.
        graph_source: verify_sparql/verify_joins=True 시 사용. 기본 "merge".
        verify_sparql: True면 스키마 연결성 후 로컬 SPARQL로 조인 결과 수 검증.
        verify_joins: True면 모든 CQ 클래스 쌍에 대해 고정 COUNT SPARQL 조인 검증.

    """
    try:
        start_all = time.monotonic()
        logger.info(
            "[S12 query-test] 시작 — verify_joins=%s verify_sparql=%s graph_source=%s",
            verify_joins, verify_sparql, graph_source,
        )

        # 1. CQ 로드
        cq_path = (
            resolve_child_path(
                SOURCE_QUERY_TESTS_DIR,
                test_cases_path,
                allowed_suffixes=(".json",),
            )
            if test_cases_path
            else _CQ_PATH
        )
        if not os.path.exists(cq_path):
            return error_response(
                "Competency Questions가 없습니다.",
                hint="generate_competency_questions로 먼저 생성하세요.",
                logger=logger,
            )
        with open(cq_path, encoding="utf-8") as f:
            cqs = json.load(f)
        _validate_embedded_queries(cqs)

        # 2. 시맨틱 딕셔너리에서 OP/클래스 정보 로드
        sem_dict = _load_semantic_dict()
        obj_props = sem_dict.get("object_properties", {}) if sem_dict else {}
        classes_dict = (sem_dict or {}).get("classes", {})

        # 3. A-Box 통계 로드 (그래프 로드 불필요)
        abox_stats = _load_abox_stats()
        per_class = abox_stats.get("per_class", {}) if abox_stats else {}
        stats_available = bool(per_class)

        if not stats_available:
            logger.warning("abox_stats.json 없음 — 그래프 로드 폴백")
            # 폴백: 기존 방식으로 그래프 로드
            from tools.sparql_local import _get_graph
            graph, load_msg = _get_graph(graph_source)
        else:
            graph = None
            load_msg = f"abox_stats.json 사용 ({len(per_class)} 클래스, 그래프 로드 없음)"

        # 4. 연결성 검증을 위한 헬퍼
        from tools.competency_questions import _find_connecting_op

        # 5. CQ별 검증
        test_results = []
        passed = 0
        failed = 0

        for cq_idx, cq in enumerate(cqs, 1):
            cq_id = cq.get("id", "unknown")
            logger.info(
                "[S12 query-test] CQ %d/%d 검증 중 — %s", cq_idx, len(cqs), cq_id,
            )
            question = cq.get("question_ko", cq.get("question_en", ""))
            domains = cq.get("domains", [])
            # 로컬 이름 형식이 아닌 도메인은 SPARQL 에 보간하지 않고 실패로 기록한다.
            cls_names, invalid_domain_checks = _resolve_cq_class_names(domains)
            checks = list(invalid_domain_checks)
            all_passed = not invalid_domain_checks
            # 이 CQ 에서 **선언만으로** 통과한 연결 (A-Box 0행 OP 경유).
            # 통과율을 깎지 않고 별도 축으로 보고한다 — 통과율이 올랐을 때
            # "데이터가 늘었나 선언이 늘었나" 를 구분할 수 있어야 한다.
            schema_only_pairs: list[str] = []

            # 5a. 인스턴스 존재 확인
            for cls_name in cls_names:
                if stats_available:
                    cls_stats = per_class.get(cls_name, {})
                    count = cls_stats.get("instance_count", 0)
                else:
                    # 폴백: SPARQL 쿼리
                    from domain.namespaces import NS_PREFIX
                    q = prepend_prefixes(
                        f"SELECT (COUNT(?x) AS ?cnt) WHERE {{ ?x a {NS_PREFIX}:{cls_name} }}"
                    )
                    rows = list(_run_local_query(graph, q))
                    count = int(rows[0][0]) if rows else 0

                check_passed = count > 0
                checks.append({
                    "check": f"{cls_name} 인스턴스 존재 확인",
                    "passed": check_passed,
                    "detail": f"{count}건" if check_passed else "0건 — 인스턴스 없음",
                })
                if not check_passed:
                    all_passed = False

            # 5b. 연결성 확인 (스키마 기반 — SPARQL 불필요)
            for i in range(len(cls_names)):
                for j in range(i + 1, len(cls_names)):
                    a, b = cls_names[i], cls_names[j]
                    connecting_op = _find_connecting_op(a, b, obj_props)

                    if connecting_op:
                        # 이 체크는 **선언** 축이다 — 통과시키되 그 연결이 데이터로
                        # 뒷받침되는지 따로 센다 (아래 ``schema_only_*`` 참조).
                        live = _op_is_live(obj_props.get(connecting_op))
                        if not live:
                            schema_only_pairs.append(
                                f"{a}↔{b} via {connecting_op}",
                            )
                        checks.append({
                            "check": f"{a} ↔ {b} 연결 확인 (via {connecting_op})",
                            "passed": True,
                            "detail": (
                                f"연결됨 (스키마: {connecting_op})" if live
                                else f"연결됨 (스키마: {connecting_op}) — "
                                     f"⚠ 그 OP 는 A-Box 0행 (선언만)"
                            ),
                            "_schema_only": not live,
                        })
                    else:
                        # 멀티홉: a→mid→b
                        multihop_found = False
                        # CQ 도메인 내 중간 클래스 + 전체 클래스에서 허브 탐색
                        hub_candidates = list(cls_names) + [
                            c for c in per_class if c not in cls_names
                        ] if stats_available else cls_names
                        for mid in hub_candidates:
                            if mid in (a, b):
                                continue
                            op_a_mid = _find_connecting_op(a, mid, obj_props)
                            op_mid_b = _find_connecting_op(mid, b, obj_props)
                            if op_a_mid and op_mid_b:
                                # 세그먼트 **각각** 을 본다 — 8/30 baseline 의 CQ11 은
                                # 0행 × 0행 멀티홉으로 통과했다 (_op_is_live docstring).
                                dead = [
                                    name for name in (op_a_mid, op_mid_b)
                                    if not _op_is_live(obj_props.get(name))
                                ]
                                if dead:
                                    schema_only_pairs.append(
                                        f"{a}→{mid}→{b} via {'/'.join(dead)}",
                                    )
                                checks.append({
                                    "check": f"{a} → {mid} → {b} 멀티홉 연결 확인",
                                    "passed": True,
                                    "detail": (
                                        f"연결됨 (스키마: {op_a_mid} → {op_mid_b})"
                                        if not dead else
                                        f"연결됨 (스키마: {op_a_mid} → {op_mid_b}) — "
                                        f"⚠ A-Box 0행 세그먼트: {', '.join(dead)}"
                                    ),
                                    "_schema_only": bool(dead),
                                })
                                multihop_found = True
                                break

                        if not multihop_found:
                            # 간접 연결: OP 그래프에서 공유 클래스 탐색
                            indirect_found = False
                            if stats_available:
                                # 스키마에서 a→?hub 또는 ?hub→a, b→?hub 또는 ?hub→b 경로 검색
                                a_neighbors = set()
                                b_neighbors = set()
                                for _op_name, op_info in obj_props.items():
                                    raw_d = op_info.get("domain") or ""
                                    raw_r = op_info.get("range") or ""
                                    d = (raw_d[0] if isinstance(raw_d, list) else raw_d).replace("_", "")
                                    r = (raw_r[0] if isinstance(raw_r, list) else raw_r).replace("_", "")
                                    if a in d:
                                        a_neighbors.add(r)
                                    if a in r:
                                        a_neighbors.add(d)
                                    if b in d:
                                        b_neighbors.add(r)
                                    if b in r:
                                        b_neighbors.add(d)
                                shared = a_neighbors & b_neighbors
                                if shared:
                                    indirect_found = True
                                    checks.append({
                                        "check": f"{a} ↔ {b} 간접 연결 확인 (공유 이웃)",
                                        "passed": True,
                                        "detail": f"연결됨 (공유: {', '.join(list(shared)[:3])})",
                                    })

                            if not indirect_found:
                                checks.append({
                                    "check": f"{a} ↔ {b} 연결 확인",
                                    "passed": False,
                                    "detail": "연결 경로 없음",
                                })
                                all_passed = False

            # 5c. DP 값 존재 확인 (stats 기반: OP 트리플 > 0이면 데이터 존재)
            for cls_name in cls_names:
                cls_info = classes_dict.get(cls_name, {})
                dps = cls_info.get("datatype_properties", [])
                if not dps:
                    continue
                sample_dps = dps[:3] if isinstance(dps, list) else list(dps)[:3]
                dp_names = [dp.get("name", dp) if isinstance(dp, dict) else str(dp) for dp in sample_dps]
                # 딕셔너리 DP 이름도 보간 대상이므로 로컬 이름 형식만 남긴다.
                skipped_dps = [dp for dp in dp_names if not _is_local_name(dp)]
                if skipped_dps:
                    logger.warning(
                        "[S12 query-test] %s: 로컬 이름 형식이 아닌 DP %d개 제외",
                        cls_name, len(skipped_dps),
                    )
                dp_names = [dp for dp in dp_names if _is_local_name(dp)]
                if not dp_names:
                    continue

                if stats_available:
                    # A-Box 생성 시 CSV에서 변환했으므로, 인스턴스가 있으면 DP 값도 존재
                    cls_stats = per_class.get(cls_name, {})
                    count = cls_stats.get("instance_count", 0)
                else:
                    from domain.namespaces import NS_PREFIX
                    pfx = NS_PREFIX
                    dp_patterns = " ".join(f"OPTIONAL {{ ?x {pfx}:{dp} ?v{i} }}" for i, dp in enumerate(dp_names))
                    filters = " || ".join(f"BOUND(?v{i})" for i in range(len(dp_names)))
                    q = prepend_prefixes(
                        f"SELECT (COUNT(?x) AS ?cnt) WHERE {{ "
                        f"?x a {pfx}:{cls_name} . {dp_patterns} "
                        f"FILTER({filters}) }}"
                    )
                    rows = list(_run_local_query(graph, q))
                    count = int(rows[0][0]) if rows else 0

                check_passed = count > 0
                checks.append({
                    "check": f"{cls_name} DatatypeProperty 값 존재 확인 ({', '.join(dp_names[:2])}...)",
                    "passed": check_passed,
                    "detail": f"{count}건" if check_passed else "0건 — DP 값 없음",
                })
                if not check_passed:
                    all_passed = False

            # 5e. SPARQL 조인 검증 (verify_joins=True: 3-단계 fallback COUNT 쿼리)
            # R11-M6: CQ 화살표와 A-Box 방향 불일치 대응.
            # forward → reverse → any-path 순서로 시도하고 direction 을 기록.
            if verify_joins and all_passed and len(cls_names) >= 2:
                if graph is None:
                    from tools.sparql_local import _get_graph
                    logger.info(
                        "[S12 query-test] verify_joins용 그래프 로드 시작 "
                        "(source=%s) — 대용량이면 수십초 소요", graph_source,
                    )
                    _t_g = time.monotonic()
                    graph, _ = _get_graph(graph_source)
                    logger.info(
                        "[S12 query-test] 그래프 로드 완료 — %d triples (%.1fs)",
                        len(graph), time.monotonic() - _t_g,
                    )
                for i in range(len(cls_names)):
                    for j in range(i + 1, len(cls_names)):
                        a, b = cls_names[i], cls_names[j]
                        connecting_op = _find_connecting_op(a, b, obj_props)
                        if not connecting_op:
                            continue
                        try:
                            vr = _verify_class_join(graph, a, connecting_op, b)
                            check_passed = vr["passed"]
                            if check_passed:
                                if vr["direction"] == "forward":
                                    detail = f"{vr['count']}건"
                                else:
                                    detail = f"{vr['count']}건 (direction={vr['direction']})"
                            else:
                                detail = "0건 — 실제 조인 결과 없음 (forward/reverse/any-path 모두 0)"
                            checks.append({
                                "check": f"SPARQL 조인: {a} ↔ {b} (hint OP={connecting_op})",
                                "passed": check_passed,
                                "detail": detail,
                                # 5d 와 **같은 키** 를 심는다. 이 키가 없으면 아래 6절의
                                # 제안 분기(``_sparql_result_count == 0``)가 ``.get()``
                                # 의 None 을 받아 ``None == 0`` → False 로 미스하고,
                                # 5e 실패가 cq_feedback 에 **한 건도** 남지 않는다.
                                # 실측: 배포 cq_feedback.json 5 iteration 제안 40건이
                                # 전부 missing_connection, failed_query 0건. 8/29
                                # iteration(failed=3)에 CQ11 제안이 없는데 그 CQ 의
                                # 유일한 실패가 5e 조인 0건이었다.
                                "_sparql_result_count": vr.get("count", 0),
                                "_sparql_error": None,
                                "_join_verification": True,
                            })
                            if not check_passed:
                                all_passed = False
                        except Exception as e:
                            checks.append({
                                "check": f"SPARQL 조인: {a} ↔ {b}",
                                "passed": False,
                                "detail": f"오류: {str(e)[:80]}",
                                "_sparql_result_count": 0,
                                "_sparql_error": str(e)[:200],
                                "_join_verification": True,
                            })
                            all_passed = False

            # 5d. SPARQL 실행 검증 (verify_sparql=True일 때만)
            if verify_sparql and all_passed and len(cls_names) >= 2:
                if graph is None:
                    from tools.sparql_local import _get_graph
                    graph, _ = _get_graph(graph_source)
                # 두 클래스 간 OP 조인 결과 수 확인
                a, b = cls_names[0], cls_names[1]
                from domain.namespaces import NS_PREFIX
                pfx = NS_PREFIX
                connecting_op = _find_connecting_op(a, b, obj_props)
                if connecting_op:
                    sparql_q = ""
                    try:
                        # OP 이름은 딕셔너리 값이므로 보간 전에 형식을 확인한다.
                        _require_local_names(connecting_op)
                        sparql_q = prepend_prefixes(
                            f"SELECT (COUNT(*) AS ?cnt) WHERE {{ "
                            f"?x a {pfx}:{a} ; {pfx}:{connecting_op} ?y . "
                            f"?y a {pfx}:{b} }}"
                        )
                        rows = list(_run_local_query(graph, sparql_q))
                        join_count = int(rows[0][0]) if rows else 0
                        check_passed = join_count > 0
                        checks.append({
                            "check": f"SPARQL 조인 검증: {a} → {b} (via {connecting_op})",
                            "passed": check_passed,
                            "detail": f"{join_count}건" if check_passed else "0건 — 조인 결과 없음",
                            # D2: attach sparql + execution outcome so the
                            # downstream feedback loop can classify this as
                            # zero_results / parse_error / timeout etc.
                            "_sparql": sparql_q,
                            "_sparql_error": None,
                            "_sparql_result_count": join_count,
                        })
                        if not check_passed:
                            all_passed = False
                    except Exception as e:
                        checks.append({
                            "check": f"SPARQL 조인 검증: {a} → {b}",
                            "passed": False,
                            "detail": f"SPARQL 오류: {str(e)[:100]}",
                            # D2: the raw exception is captured for the
                            # feedback loop to infer the error category.
                            "_sparql": sparql_q,
                            "_sparql_error": e,
                            "_sparql_result_count": 0,
                        })
                        all_passed = False

            status = "PASS" if all_passed else "FAIL"
            if all_passed:
                passed += 1
            else:
                failed += 1

            test_results.append({
                "id": cq_id,
                "status": status,
                "question": question,
                "difficulty": cq.get("difficulty", ""),
                "domains": domains,
                "checks": checks,
                "checks_passed": sum(1 for c in checks if c["passed"]),
                "checks_total": len(checks),
                "failures": [c["check"] + " — " + c["detail"] for c in checks if not c["passed"]],
                # 선언만으로 통과한 연결. 통과 판정은 바꾸지 않고 축을 나눠 드러낸다.
                "schema_only_connections": len(schema_only_pairs),
                "schema_only_detail": schema_only_pairs[:10],
            })

        duration_all = round(time.monotonic() - start_all, 1)
        total = len(cqs)
        logger.info(
            "[S12 query-test] 완료 — %d/%d CQ pass (%.1fs)",
            passed, total, duration_all,
        )

        summary = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / max(total, 1) * 100, 1),
            "duration_seconds": duration_all,
            "graph_source": "abox_stats" if stats_available else graph_source,
            "graph_info": load_msg,
            "method": ("zero_graph_load+sparql_verify" if verify_sparql else ("zero_graph_load+join_verify" if verify_joins else "zero_graph_load")) if stats_available else "sparql_fallback",
            "bedrock_calls": 0,
            # **선언 축 vs 데이터 축.** pass_rate 는 5b(선언) 기준이라 A-Box 0행 OP 를
            # 하나 선언하면 오른다 (실측: 66.7% → 75.0%, 데이터 증가 0). 이 수치가
            # 없으면 통과율 상승이 "데이터가 늘었나 선언이 늘었나" 를 구분할 수 없다.
            # 배포 이력 실측 — 8/30 baseline 의 CQ11 은 0행 × 0행 멀티홉으로 통과했다.
            "schema_only_connections": sum(
                r.get("schema_only_connections", 0) for r in test_results
            ),
            "cqs_with_schema_only_pass": sum(
                1 for r in test_results
                if r.get("schema_only_connections") and r["status"] != "FAIL"
            ),
        }

        # 6. 실패 CQ → 딕셔너리 개선 피드백
        improvement_suggestions = []
        for r in test_results:
            if r["status"] != "FAIL":
                continue
            for check in r["checks"]:
                if check["passed"]:
                    continue
                check_str = check["check"]
                detail = check["detail"]
                if check.get("_invalid_domain"):
                    # 거부된 원문은 피드백 파일과 다음 프롬프트로 옮기지 않는다.
                    improvement_suggestions.append({
                        "cq_id": r["id"],
                        "type": "invalid_domain",
                        "action": (
                            "CQ domains 에 SPARQL 로컬 이름 형식이 아닌 값이 있어 "
                            "질의에서 제외했다. CSV 테이블명 또는 클래스 로컬 이름만 넣어라."
                        ),
                    })
                elif "인스턴스 존재" in check_str and "0건" in detail:
                    cls_name = check_str.split(" ")[0]
                    improvement_suggestions.append({
                        "cq_id": r["id"],
                        "type": "missing_instances",
                        "class": cls_name,
                        "action": f"A-Box에 {cls_name} 인스턴스 없음. CSV 데이터 또는 T-Box 클래스 매핑 확인.",
                    })
                elif "연결 확인" in check_str and "경로 없음" in detail:
                    parts = check_str.replace("연결 확인", "").strip().split(" ↔ ")
                    if len(parts) == 2:
                        improvement_suggestions.append({
                            "cq_id": r["id"],
                            "type": "missing_connection",
                            "class_a": parts[0].strip(),
                            "class_b": parts[1].strip(),
                            "action": f"T-Box에 {parts[0].strip()} ↔ {parts[1].strip()} 간 ObjectProperty 추가 필요.",
                        })
                elif "DP" in check_str and "0건" in detail:
                    cls_name = check_str.split(" ")[0]
                    improvement_suggestions.append({
                        "cq_id": r["id"],
                        "type": "missing_dp_values",
                        "class": cls_name,
                        "action": f"{cls_name}의 DatatypeProperty 값 없음. CSV→T-Box DP 매핑 확인.",
                    })
                elif check.get("_join_verification"):
                    # 5e 조인 검증 실패 — **``missing_connection`` 과 다른 타입**이다.
                    #
                    # 여기는 "OP 선언은 있는데 데이터가 0행" 이므로 처방이 다르다
                    # (OP 를 추가하라 ✗ / FK·tacit 을 채우라 ○). 같은 타입으로 합치면
                    # 지문이 병합돼 age_iterations 가 부풀고, 그것이 이 리포에 기록된
                    # "지문 과잉병합 = 지표 매수" 다.
                    parts = check_str.split(":", 1)[-1].split("(")[0].strip()
                    pair = [p.strip() for p in parts.split("↔")]
                    improvement_suggestions.append({
                        "cq_id": r["id"],
                        "type": "empty_connection",
                        "class_a": pair[0] if pair else "",
                        "class_b": pair[1] if len(pair) > 1 else "",
                        "action": (
                            f"{parts} 는 T-Box 에 관계가 **선언돼 있으나** A-Box 조인이 "
                            f"0행이다 ({detail}). OP 를 더 선언하지 말고 FK 컬럼·tacit "
                            f"으로 값을 채우거나, 그 관계가 데이터로 성립하지 않음을 "
                            f"인정하고 CQ 를 조정하라."
                        ),
                    })
                elif "SPARQL" in check_str and (
                    check.get("_sparql_error") is not None
                    or check.get("_sparql_result_count") == 0
                ):
                    # D2: SPARQL 실행 자체의 실패/빈 결과를 failed_query 로 분류.
                    try:
                        from tools.cq_feedback import _build_failed_query_suggestion
                        improvement_suggestions.append(
                            _build_failed_query_suggestion(
                                cq_id=r["id"],
                                sparql=check.get("_sparql", ""),
                                error=check.get("_sparql_error"),
                                result_count=int(
                                    check.get("_sparql_result_count", 0) or 0
                                ),
                            )
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "failed_query suggestion 생성 실패 (비차단): %s", e,
                        )
                else:
                    # 어떤 분기에도 걸리지 않은 실패 — **반드시 기록한다.**
                    # 새 체크 종류가 생겨도 조용히 소실되지 않게 한다 (5e 가 정확히
                    # 그렇게 5 iteration 동안 비가시였다).
                    improvement_suggestions.append({
                        "cq_id": r["id"],
                        "type": "unclassified_failure",
                        "check": check_str[:120],
                        "action": (
                            f"분류되지 않은 실패: {detail[:120]} — 이 체크 종류에 맞는 "
                            f"제안 형태를 tools/query_test.py 6절에 추가하라."
                        ),
                    })

        # 6.5. T3: cq_feedback.json 에 iteration 기록 (non-blocking)
        # 이전 iteration 에서 fail 이었던 CQ 중 이번에 PASS 한 것을 resolved 로 기록.
        # 실패가 S12 전체를 막지 않도록 try/except 로 감싼다.
        #
        # 주의: load_active_suggestions() 는 dedupe + circuit breaker 가 적용돼
        # cq_id 가 누락될 수 있다. resolved 정확도를 위해 이전 iteration 의 raw
        # suggestions 를 직접 읽는다.
        try:
            from tools.cq_feedback import (
                _load_feedback as _fb_load_raw,
            )
            from tools.cq_feedback import (
                append_iteration as _fb_append,
            )
            prev_data = _fb_load_raw()
            prev_iterations = prev_data.get("iterations", [])
            prev_failed_ids: set[str] = set()
            if prev_iterations:
                latest_sugs = prev_iterations[-1].get("suggestions", []) or []
                prev_failed_ids = {
                    s.get("cq_id") for s in latest_sugs if s.get("cq_id")
                }
            current_passed_ids = {r["id"] for r in test_results if r["status"] == "PASS"}
            resolved = sorted(prev_failed_ids & current_passed_ids)
            _fb_append(
                pass_rate=summary["pass_rate"],
                total_cqs=summary["total"],
                passed=summary["passed"],
                failed=summary["failed"],
                suggestions=improvement_suggestions,
                resolved_cq_ids=resolved,
            )
        except Exception as e:  # noqa: BLE001 — feedback 실패는 S12 를 막지 않는다
            logger.warning("cq_feedback 기록 실패 (비차단): %s", e)

        # 7. HTML 보고서 생성
        report_path = resolve_generated_path("reports/query_test_report.html")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_html = _generate_cq_report_html(test_results, summary)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_html)

        if open_report:
            from tools.common import path_to_file_uri
            webbrowser.open(path_to_file_uri(report_path))

        return json.dumps({
            "success": True,
            "summary": summary,
            "report_path": str(report_path),
            "results": [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "question": r["question"][:80],
                    "checks": f"{r['checks_passed']}/{r['checks_total']}",
                    "failures": r["failures"],
                    # 선언만으로 통과한 연결. 사영에서 빠뜨리면 summary 총계만 남아
                    # "어느 CQ 가 유령에 기대는가" 를 알 수 없다.
                    **({"schema_only_connections": r["schema_only_connections"],
                        "schema_only_detail": r["schema_only_detail"]}
                       if r.get("schema_only_connections") else {}),
                }
                for r in test_results
            ],
            "improvement_suggestions": improvement_suggestions,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def validate_cq_tbox_only(open_report: bool = False) -> str:
    """S4 직후 CQ 답변가능성 사전 검증 — T-Box 스키마만으로 CQ 커버리지를 확인한다.

    A-Box 없이 T-Box 클래스/프로퍼티만으로 CQ가 답변 가능한지 조기 판별.
    S12(test_domain_queries)까지 가지 않아도 T-Box 구조 문제를 탐지.

    검증 항목:
    1. CQ 도메인 클래스가 T-Box에 존재하는지
    2. 도메인 클래스 간 ObjectProperty 연결 경로가 있는지

    ``competency_questions.validate_competency_questions`` (A-Box·추론 그래프까지
    보는 전체 검증, ``graph_source`` 인자) 와 **다른 도구** 다. 2026-08-08 까지 두
    도구가 같은 이름이었고 FastMCP 가 동명 툴을 경고만 남기고 첫 등록을 유지하는
    탓에 이쪽이 MCP 로 **호출 불가** 였다 (docs 는 이쪽의 ``pass_rate`` 를 설명하고
    있었으므로 문서와 실제가 어긋난 상태였다).
    """
    try:
        if not os.path.exists(_CQ_PATH):
            return error_response("CQ가 없습니다.", hint="generate_competency_questions로 먼저 생성.", logger=logger)
        with open(_CQ_PATH, encoding="utf-8") as f:
            cqs = json.load(f)

        from config import TBOX_PATH
        if not os.path.exists(TBOX_PATH):
            return error_response("T-Box가 없습니다.", hint="generate_tbox로 먼저 생성.", logger=logger)

        from rdflib import OWL, RDF, RDFS

        from domain.namespaces import DOMAIN_NS
        g = _new_graph()
        g.parse(TBOX_PATH, format="turtle")
        steel_ns = DOMAIN_NS

        tbox_classes = set()
        for cls in g.subjects(RDF.type, OWL.Class):
            cls_str = str(cls)
            if cls_str.startswith(steel_ns):
                tbox_classes.add(cls_str.split("#")[-1].split("/")[-1])

        tbox_ops = {}
        for prop in g.subjects(RDF.type, OWL.ObjectProperty):
            prop_str = str(prop)
            if not prop_str.startswith(steel_ns):
                continue
            local = prop_str.split("#")[-1]
            domains = [str(d).split("#")[-1] for d in g.objects(prop, RDFS.domain) if str(d).startswith(steel_ns)]
            ranges = [str(r).split("#")[-1] for r in g.objects(prop, RDFS.range) if str(r).startswith(steel_ns)]
            tbox_ops[local] = {"domain": domains[0] if domains else "", "range": ranges[0] if ranges else ""}

        results = []
        passed = 0
        failed = 0
        missing_classes_all = set()
        missing_connections = []

        for cq in cqs:
            cq_id = cq.get("id", "unknown")
            question = cq.get("question_ko", cq.get("question_en", ""))
            domains = cq.get("domains", [])
            cls_names = [_domain_to_class_name(d) for d in domains]
            checks = []
            all_ok = True

            for cls_name in cls_names:
                exists = cls_name in tbox_classes
                checks.append({"check": f"T-Box 클래스: {cls_name}", "passed": exists,
                                "detail": "정의됨" if exists else "T-Box에 없음"})
                if not exists:
                    all_ok = False
                    missing_classes_all.add(cls_name)

            for i in range(len(cls_names)):
                for j in range(i + 1, len(cls_names)):
                    a, b = cls_names[i], cls_names[j]
                    if a not in tbox_classes or b not in tbox_classes:
                        continue
                    connected = False
                    via = None
                    for op_name, op_info in tbox_ops.items():
                        d, r = op_info.get("domain", ""), op_info.get("range", "")
                        if (d == a and r == b) or (d == b and r == a):
                            connected = True
                            via = op_name
                            break
                    if not connected:
                        for mid in tbox_classes:
                            if mid in (a, b):
                                continue
                            op1 = op2 = None
                            for on, oi in tbox_ops.items():
                                d, r = oi.get("domain", ""), oi.get("range", "")
                                if not op1 and ((d == a and r == mid) or (d == mid and r == a)):
                                    op1 = on
                                if not op2 and ((d == mid and r == b) or (d == b and r == mid)):
                                    op2 = on
                            if op1 and op2:
                                connected = True
                                via = f"{op1}→{mid}→{op2}"
                                break
                    checks.append({"check": f"{a} ↔ {b}", "passed": connected,
                                    "detail": f"연결됨 ({via})" if connected else "경로 없음"})
                    if not connected:
                        all_ok = False
                        missing_connections.append(f"{a}↔{b}")

            status = "PASS" if all_ok else "FAIL"
            if all_ok:
                passed += 1
            else:
                failed += 1
            results.append({"id": cq_id, "status": status, "question": question,
                            "failures": [c["check"] + " — " + c["detail"] for c in checks if not c["passed"]]})

        total = len(cqs)
        suggestions = []
        if missing_classes_all:
            suggestions.append({"type": "missing_classes", "classes": sorted(missing_classes_all),
                                "action": "T-Box에 해당 클래스 추가 또는 CQ 도메인 수정"})
        if missing_connections:
            suggestions.append({"type": "missing_connections", "pairs": missing_connections[:10],
                                "action": "해당 클래스 간 ObjectProperty를 T-Box에 추가"})

        return json.dumps({
            "success": True,
            "summary": {"total": total, "passed": passed, "failed": failed,
                        "pass_rate": round(passed / max(total, 1) * 100, 1)},
            "tbox_coverage": {"classes": len(tbox_classes), "object_properties": len(tbox_ops)},
            "results": [{"id": r["id"], "status": r["status"], "question": r["question"][:80],
                         "failures": r["failures"]} for r in results],
            "suggestions": suggestions,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def _esc(value: object) -> str:
    """보고서 HTML 에 넣을 값을 텍스트로 이스케이프한다 (속성 따옴표 포함)."""
    return html.escape(str(value), quote=True)


def _generate_cq_report_html(results: list, summary: dict) -> str:
    """CQ 연결성 테스트 HTML 보고서를 생성한다.

    CQ 의 id, 질문, 난이도와 체크·실패 문구는 사용자 또는 LLM 이 만든 값이므로
    보간하는 모든 값을 ``html.escape`` 로 이스케이프한다. 질문은 자른 뒤에
    이스케이프해 엔티티가 중간에 잘리지 않게 한다.
    """
    rows_html = ""
    for r in results:
        status_cls = "pass" if r["status"] == "PASS" else "fail"
        checks_detail = "<br>".join(
            f"{'✅' if c['passed'] else '❌'} {_esc(c['check'])} — {_esc(c['detail'])}"
            for c in r["checks"]
        )
        failures_html = (
            "<br>".join(_esc(f) for f in r["failures"]) if r["failures"] else "—"
        )
        rows_html += f"""<tr>
            <td>{_esc(r['id'])}</td>
            <td>{_esc(str(r['question'])[:60])}</td>
            <td>{_esc(r.get('difficulty', ''))}</td>
            <td class="{status_cls}">{_esc(r['status'])}</td>
            <td>{_esc(r['checks_passed'])}/{_esc(r['checks_total'])}</td>
            <td style="font-size:12px; text-align:left;">{checks_detail}</td>
            <td style="color:red; font-size:12px;">{failures_html}</td>
        </tr>"""

    pass_rate = summary.get("pass_rate", 0)
    rate_color = "#2ecc71" if pass_rate >= 80 else "#f39c12" if pass_rate >= 50 else "#e74c3c"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CQ 연결성 테스트 보고서</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 20px; background: #f8f9fa; }}
  h1 {{ color: #2c3e50; }}
  .summary {{ display: flex; gap: 15px; margin: 20px 0; }}
  .stat {{ background: white; padding: 15px 25px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }}
  .stat .num {{ font-size: 28px; font-weight: bold; }}
  .stat .label {{ font-size: 12px; color: #7f8c8d; margin-top: 4px; }}
  .method {{ background: #eaf4fc; padding: 12px 20px; border-radius: 8px; margin: 15px 0; font-size: 13px; color: #2c3e50; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  th {{ background: #2c3e50; color: white; padding: 12px; text-align: center; font-size: 13px; }}
  td {{ padding: 10px; border-bottom: 1px solid #ecf0f1; text-align: center; font-size: 13px; }}
  .pass {{ color: #2ecc71; font-weight: bold; }}
  .fail {{ color: #e74c3c; font-weight: bold; }}
</style></head>
<body>
<h1>CQ 연결성 테스트 보고서</h1>
<div class="method">
  <strong>검증 방식:</strong> 프로그래매틱 연결성 검사 (LLM 호출 없음, Bedrock 0회)<br>
  <strong>검증 내용:</strong> 각 CQ의 도메인 클래스에 인스턴스가 존재하고, 클래스 간 ObjectProperty 연결 경로가 있는지 확인<br>
  <strong>의미:</strong> PASS = 이 KG로 해당 CQ에 답할 수 있는 데이터와 연결 경로가 존재 / FAIL = 데이터 또는 연결 누락
</div>
<div class="summary">
  <div class="stat"><div class="num">{_esc(summary['total'])}</div><div class="label">전체 CQ</div></div>
  <div class="stat"><div class="num" style="color:#2ecc71">{_esc(summary['passed'])}</div><div class="label">ANSWERABLE</div></div>
  <div class="stat"><div class="num" style="color:#e74c3c">{_esc(summary['failed'])}</div><div class="label">NOT ANSWERABLE</div></div>
  <div class="stat"><div class="num" style="color:{rate_color}">{_esc(pass_rate)}%</div><div class="label">답변 가능률</div></div>
  <div class="stat"><div class="num">{_esc(summary['duration_seconds'])}s</div><div class="label">소요시간</div></div>
</div>
<table>
<tr><th>ID</th><th>질문</th><th>난이도</th><th>결과</th><th>체크</th><th>상세 검증</th><th>실패 사유</th></tr>
{rows_html}
</table>
</body></html>"""
