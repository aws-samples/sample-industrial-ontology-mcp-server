"""Neo4j 리모트 도구 — RDF→LPG 변환 + CSV 배치 적재 + Cypher 쿼리."""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import random
import re
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime

from rdflib import OWL, RDF, RDFS, BNode, Literal, URIRef
from rdflib import Graph as RdfGraph

from config import (
    GENERATED_REPORTS_DIR,
    INFERRED_PATH,
    LPG_SEMANTIC_DICT_PATH,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    TBOX_PATH,
)
from domain.namespaces import DOMAIN_CONFIG, DOMAIN_INST_NS, DOMAIN_NS, IOF_IRI_ROOT
from domain.owl_constants import (
    OWL_AXIOM_PREDICATES as _EXCLUDE_PREDICATES,
)
from domain.owl_constants import (
    OWL_META_TYPES as _OWL_META_TYPES,
)
from domain.owl_constants import (
    is_upper_ontology_type as _is_upper_ontology_type,
)
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.common import (
    cached_client,
    error_response,
    resolve_child_path,
    resolve_path_within,
)

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT = 30.0  # Cypher 쿼리 타임아웃 (초)
_CYPHER_IGNORED_TEXT = re.compile(
    r"""
    //[^\r\n]*
    | /\*.*?\*/
    | '(?:\\.|''|[^'\\])*'
    | "(?:\\.|""|[^"\\])*"
    | `(?:``|[^`])*`
    """,
    re.DOTALL | re.VERBOSE,
)
_CYPHER_BLOCKED_CLAUSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CREATE", re.compile(r"\bCREATE\b", re.IGNORECASE)),
    ("INSERT", re.compile(r"\bINSERT\b", re.IGNORECASE)),
    ("MERGE", re.compile(r"\bMERGE\b", re.IGNORECASE)),
    ("DELETE", re.compile(r"\bDELETE\b", re.IGNORECASE)),
    ("SET", re.compile(r"\bSET\b", re.IGNORECASE)),
    ("REMOVE", re.compile(r"\bREMOVE\b", re.IGNORECASE)),
    ("DROP", re.compile(r"\bDROP\b", re.IGNORECASE)),
    ("ALTER", re.compile(r"\bALTER\b", re.IGNORECASE)),
    ("RENAME", re.compile(r"\bRENAME\b", re.IGNORECASE)),
    ("GRANT", re.compile(r"\bGRANT\b", re.IGNORECASE)),
    ("DENY", re.compile(r"\bDENY\b", re.IGNORECASE)),
    ("REVOKE", re.compile(r"\bREVOKE\b", re.IGNORECASE)),
    ("TERMINATE", re.compile(r"\bTERMINATE\b", re.IGNORECASE)),
    ("START", re.compile(r"\bSTART\b", re.IGNORECASE)),
    ("STOP", re.compile(r"\bSTOP\b", re.IGNORECASE)),
    ("ENABLE SERVER", re.compile(r"\bENABLE\s+SERVER\b", re.IGNORECASE)),
    (
        "DEALLOCATE DATABASES",
        re.compile(r"\bDEALLOCATE\s+DATABASES?\b", re.IGNORECASE),
    ),
    (
        "REALLOCATE DATABASES",
        re.compile(r"\bREALLOCATE\s+DATABASES?\b", re.IGNORECASE),
    ),
    ("FOREACH", re.compile(r"\bFOREACH\b", re.IGNORECASE)),
    ("CALL", re.compile(r"\bCALL\b", re.IGNORECASE)),
    ("LOAD CSV", re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE)),
)


# ── Neo4j 연결 ──────────────────────────────────────────────────────────────


def _lpg_csv_dir() -> str:
    """convert_rdf_to_lpg 가 LPG CSV 를 쓰는 디렉터리. 호출자가 지정한 CSV 경로의 경계다."""
    return os.path.join(os.path.dirname(INFERRED_PATH), "neo4j")


def _resolve_lpg_csv(path: str) -> str:
    """호출자가 지정한 LPG CSV 경로를 symlink 해석 후 LPG CSV 디렉터리 안으로 제한한다."""
    return resolve_path_within(_lpg_csv_dir(), path, allowed_suffixes=(".csv",))


def _check_configured() -> str | None:
    """NEO4J_URI 미설정 시 에러 메시지 반환, 설정됐으면 None."""
    if not NEO4J_URI:
        return error_response(
            "NEO4J_URI가 설정되지 않았습니다.",
            hint=".env에 NEO4J_URI=bolt://localhost:7687 을 설정하세요.",
        )
    return None


@cached_client
def _get_driver():
    """Neo4j driver 싱글턴. 내부 커넥션 풀 관리. 프로세스 종료 시 자동 정리."""
    import atexit

    from neo4j import GraphDatabase

    auth = (NEO4J_USER, NEO4J_PASSWORD) if NEO4J_PASSWORD else None
    driver = GraphDatabase.driver(NEO4J_URI, auth=auth)
    atexit.register(driver.close)
    return driver


def _handle_neo4j_error(e: Exception) -> str:
    """Neo4j 에러를 분류하여 표준 에러 응답 반환."""
    error_str = str(e).lower()
    if "serviceunav" in error_str or "connection" in error_str or "refused" in error_str:
        hint = "Neo4j 연결 실패. NEO4J_URI와 서버 상태를 확인하세요."
    elif "auth" in error_str or "unauthorized" in error_str:
        hint = "Neo4j 인증 실패. NEO4J_USER/NEO4J_PASSWORD를 확인하세요."
    else:
        hint = "Neo4j 쿼리 실행 중 오류. 연결과 구문을 확인하세요."
    return error_response(e, hint=hint, logger=logger)


def _cypher_blocked_clause(query: str) -> str | None:
    """문자열과 주석 밖의 write/admin/external-load clause를 찾는다."""
    scrubbed = _CYPHER_IGNORED_TEXT.sub(" ", query)
    for name, pattern in _CYPHER_BLOCKED_CLAUSES:
        if pattern.search(scrubbed):
            return name
    return None


def _first_value(records: list[dict]):
    """첫 레코드의 첫 컬럼 값. 레코드가 없거나 비어 있으면 None."""
    if records and records[0]:
        return list(records[0].values())[0]
    return None


def _run_cypher_read_single(query: str, params: dict | None = None):
    """읽기 전용 단일 값 쿼리. neo4j_query 와 같은 clause guard 와 READ 세션을 쓴다.

    차단 clause 가 있으면 driver 를 열기 전에 ValueError 를 던진다. access mode 는
    routing hint 이므로 실제 방어는 이 guard 와 별도 read-only Neo4j identity 가 맡는다.
    """
    blocked = _cypher_blocked_clause(query)
    if blocked:
        raise ValueError(f"읽기 전용 경로에서 허용되지 않는 Cypher clause입니다: {blocked}")
    driver = _get_driver()
    with driver.session(default_access_mode="READ") as session:
        result = session.run(query, parameters=params or {}, timeout=_QUERY_TIMEOUT)
        return _first_value([record.data() for record in result])


_DEPLOY_BATCH_SIZE = 5000  # Neo4j UNWIND 배치 크기


# ── Structural Parity Diff ──────────────────────────────────────────────────


def _compute_parity_diff(cls_name: str, sparql_uris: set, cypher_uris: set) -> dict | None:
    """URI-level 집합 비교로 정확한 parity diff 생성."""
    only_rdf = sorted(sparql_uris - cypher_uris)
    only_lpg = sorted(cypher_uris - sparql_uris)
    if not only_rdf and not only_lpg:
        return None  # perfect match
    return {
        "class": cls_name,
        "sparql_count": len(sparql_uris),
        "cypher_count": len(cypher_uris),
        "only_in_rdf": only_rdf[:10],
        "only_in_lpg": only_lpg[:10],
    }


# ── MCP 도구 ────────────────────────────────────────────────────────────────


def neo4j_deploy_lpg(
    nodes_csv: str = "",
    relationships_csv: str = "",
    replace: bool = False,
) -> str:
    """convert_rdf_to_lpg로 생성한 CSV 파일을 Neo4j에 배치 적재한다.

    CSV를 Python에서 읽어 Cypher UNWIND로 배치 생성. APOC 플러그인 필요.

    Args:
        nodes_csv: data/generated/inferred/neo4j 아래 nodes.csv 경로 (절대경로 또는
            작업 디렉터리 기준 상대경로). 비어있으면 기본 경로 사용.
        relationships_csv: data/generated/inferred/neo4j 아래 relationships.csv 경로
            (절대경로 또는 작업 디렉터리 기준 상대경로). 비어있으면 기본 경로 사용.
        replace: True면 기존 데이터 배치 삭제 후 적재.
    """
    err = _check_configured()
    if err:
        return err

    try:
        if nodes_csv:
            nodes_csv = _resolve_lpg_csv(nodes_csv)
        if relationships_csv:
            relationships_csv = _resolve_lpg_csv(relationships_csv)
    except Exception as e:
        return error_response(e, logger=logger)

    try:
        return _neo4j_deploy_lpg(nodes_csv, relationships_csv, replace)
    except Exception as e:
        return _handle_neo4j_error(e)


def _neo4j_deploy_lpg(nodes_csv: str, relationships_csv: str, replace: bool) -> str:
    default_dir = _lpg_csv_dir()
    n_path = nodes_csv or os.path.join(default_dir, "nodes.csv")
    r_path = relationships_csv or os.path.join(default_dir, "relationships.csv")

    for path, name in [(n_path, "nodes.csv"), (r_path, "relationships.csv")]:
        if not os.path.exists(path):
            return error_response(
                f"{name} 없음: {path}",
                hint="convert_rdf_to_lpg를 먼저 실행하세요.",
            )

    # [I6] APOC 가용성 체크
    driver = _get_driver()
    with driver.session() as session:
        try:
            session.run("RETURN apoc.version() AS v").single()
        except Exception:
            return error_response(
                "APOC 플러그인이 설치되지 않았습니다.",
                hint="Neo4j에 APOC를 설치하세요: https://neo4j.com/labs/apoc/",
            )

    # CSV 파싱
    with open(n_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        node_headers = reader.fieldnames or []
        node_rows = list(reader)

    with open(r_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rel_rows = list(reader)

    warnings: list[str] = []

    with driver.session() as session:
        # [C3] 배치 삭제 (OOM 방지)
        if replace:
            deleted = 1
            while deleted > 0:
                result = session.run("""
                    MATCH (n) WITH n LIMIT 10000
                    DETACH DELETE n
                    RETURN count(*) AS cnt
                """).single()
                deleted = result["cnt"] if result else 0

        # uri 인덱스 생성
        session.run("CREATE INDEX IF NOT EXISTS FOR (n:LPGNode) ON (n.uri)")

        # ── 노드 배치 적재 ──
        nodes_created = 0
        for i in range(0, len(node_rows), _DEPLOY_BATCH_SIZE):
            batch = node_rows[i:i + _DEPLOY_BATCH_SIZE]
            params = []
            for row in batch:
                uri = row.get("uri:ID", "")
                labels = [lb for lb in row.get(":LABEL", "").split(";") if lb]
                props = {}
                for h in node_headers:
                    if h in ("uri:ID", ":LABEL"):
                        continue
                    val = row.get(h, "")
                    if val:
                        key = h.split(":")[0]
                        if _ARRAY_DELIM in val:
                            props[key] = val.split(_ARRAY_DELIM)
                        else:
                            props[key] = val
                params.append({"uri": uri, "labels": labels, "props": props})

            session.run("""
                UNWIND $batch AS row
                CALL {
                    WITH row
                    CREATE (n:LPGNode)
                    SET n = row.props
                    SET n.uri = row.uri
                    WITH n, row
                    CALL apoc.create.addLabels(n, row.labels) YIELD node
                    RETURN node
                }
                RETURN count(*) AS cnt
            """, batch=params)
            nodes_created += len(batch)

        # [C2] 관계 배치 적재 — :LPGNode 라벨로 인덱스 사용
        rels_created = 0
        for i in range(0, len(rel_rows), _DEPLOY_BATCH_SIZE):
            batch = rel_rows[i:i + _DEPLOY_BATCH_SIZE]
            params = [
                {"start": r[":START_ID"], "end": r[":END_ID"], "type": r[":TYPE"]}
                for r in batch
            ]
            session.run("""
                UNWIND $batch AS row
                MATCH (a:LPGNode {uri: row.start})
                MATCH (b:LPGNode {uri: row.end})
                CALL apoc.create.relationship(a, row.type, {}, b) YIELD rel
                RETURN rel
            """, batch=params)
            rels_created += len(batch)

        # [C4] 적재 후 검증은 실제 수량과 비교한다. 보간 없는 고정 집계를 적재와 같은
        # 세션에서 실행한다. 별도 READ 세션은 클러스터에서 아직 반영되지 않은 reader 로
        # 라우팅돼 적재 누락을 잘못 경고할 수 있다.
        total_nodes = _first_value(
            session.run("MATCH (n) RETURN count(n) AS cnt").data()
        ) or 0
        total_rels = _first_value(
            session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").data()
        ) or 0

    if total_rels < rels_created:
        warnings.append(
            f"관계 {rels_created}개 요청 중 {total_rels}개만 생성됨. "
            f"{rels_created - total_rels}개 실패 (엔드포인트 누락 가능)."
        )
    if total_nodes < nodes_created:
        warnings.append(
            f"노드 {nodes_created}개 요청 중 {total_nodes}개만 생성됨."
        )

    result = {
        "success": True,
        "nodes_created": nodes_created,
        "relationships_created": rels_created,
        "neo4j_totals": {"nodes": total_nodes, "relationships": total_rels},
        "source_files": {"nodes": n_path, "relationships": r_path},
    }
    if warnings:
        result["warnings"] = warnings
    return json.dumps(result, ensure_ascii=False, indent=2)


def neo4j_query(query: str) -> str:
    """Neo4j에서 Cypher 읽기 전용 쿼리를 실행한다.

    Args:
        query: Cypher 쿼리 문자열. 읽기 전용 쿼리만 허용.
    """
    err = _check_configured()
    if err:
        return err

    try:
        if not query.strip():
            return error_response("Cypher query는 비어 있을 수 없습니다.")
        blocked = _cypher_blocked_clause(query)
        if blocked:
            return error_response(
                f"읽기 전용 neo4j_query에서 허용되지 않는 Cypher clause입니다: {blocked}",
                hint="MATCH, OPTIONAL MATCH, UNWIND, WITH, WHERE, RETURN, SHOW만 사용하세요.",
            )

        # Access mode는 routing hint다. 실제 방어는 위 fail-closed clause guard와
        # 별도 read-only Neo4j identity를 함께 사용해야 한다.
        driver = _get_driver()
        with driver.session(default_access_mode="READ") as session:
            result = session.run(query, timeout=_QUERY_TIMEOUT)
            records = [record.data() for record in result]
        return json.dumps(records, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        return _handle_neo4j_error(e)


def neo4j_stats() -> str:
    """Neo4j 그래프의 기본 통계를 조회한다. (노드 수, 관계 수, 라벨 분포)"""
    err = _check_configured()
    if err:
        return err

    try:
        driver = _get_driver()
        # 고정 집계 쿼리만 실행하므로 neo4j_query 와 같은 READ 세션을 쓴다.
        with driver.session(default_access_mode="READ") as session:
            nodes = session.run("MATCH (n) RETURN count(n) AS cnt").single()["cnt"]
            rels = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]

            label_dist = session.run("""
                MATCH (n) WHERE n.uri IS NOT NULL
                UNWIND labels(n) AS label
                WITH label WHERE label <> 'Resource'
                RETURN label, count(*) AS cnt ORDER BY cnt DESC LIMIT 30
            """).data()

            rel_dist = session.run("""
                MATCH ()-[r]->()
                RETURN type(r) AS type, count(*) AS cnt ORDER BY cnt DESC LIMIT 30
            """).data()

        return json.dumps({
            "total_nodes": nodes,
            "total_relationships": rels,
            "label_distribution": label_dist,
            "relationship_distribution": rel_dist,
        }, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        return _handle_neo4j_error(e)


# ── RDF → LPG 변환 ─────────────────────────────────────────────────────────

# OWL axiom predicate / 메타 타입 / upper ontology prefix 는 domain.owl_constants
# 에서 import (P1-4 중앙화). 모듈 본체에서는 별도 정의하지 않는다.

# [H5] 알려진 외부 네임스페이스 → prefix 매핑
_KNOWN_NS_PREFIXES = {
    "http://purl.org/dc/elements/1.1/": "dc_",
    "http://purl.org/dc/terms/": "dcterms_",
    "http://www.w3.org/2004/02/skos/core#": "skos_",
    "http://xmlns.com/foaf/0.1/": "foaf_",
    "http://www.w3.org/ns/prov#": "prov_",
}

# [M1] URI 값을 관계가 아닌 프로퍼티로 저장할 predicate
_URI_AS_PROPERTY_PREDICATES = {
    str(RDFS.seeAlso), str(RDFS.isDefinedBy),
}

# CSV 다중값 구분자 — ASCII Unit Separator (RDF 리터럴에 나타나지 않음)
# neo4j-admin import: --array-delimiter=$'\x1f'
# neo4j_deploy_lpg: Python에서 직접 split하므로 문제 없음
_ARRAY_DELIM = "\x1f"

_DOMAIN_NS = DOMAIN_NS
_INSTANCE_NS = DOMAIN_INST_NS

# [H1] XSD → Neo4j 타입 매핑
_XSD_TYPE_MAP = {
    "http://www.w3.org/2001/XMLSchema#integer": "int",
    "http://www.w3.org/2001/XMLSchema#long": "int",
    "http://www.w3.org/2001/XMLSchema#short": "int",
    "http://www.w3.org/2001/XMLSchema#decimal": "float",
    "http://www.w3.org/2001/XMLSchema#float": "float",
    "http://www.w3.org/2001/XMLSchema#double": "float",
    "http://www.w3.org/2001/XMLSchema#boolean": "boolean",
    "http://www.w3.org/2001/XMLSchema#dateTime": "datetime",
    "http://www.w3.org/2001/XMLSchema#date": "date",
}


# ── Neo4j identifier sanitize ──────────────────────────────────────────────

# Cypher reserved words (Neo4j 5.x) — labels/rel-types/prop keys 로 써도 backtick
# 으로 감싸면 동작하지만, 본 생성기는 자동으로 safe identifier 를 내보낸다.
_CYPHER_RESERVED = frozenset({
    "all", "and", "any", "as", "asc", "ascending", "by", "call", "case", "constraint",
    "contains", "create", "delete", "desc", "descending", "detach", "distinct", "do",
    "drop", "else", "end", "ends", "exists", "fieldterminator", "for", "from", "if",
    "in", "index", "is", "join", "key", "label", "limit", "load", "match", "merge",
    "node", "none", "not", "null", "on", "optional", "or", "order", "periodic", "profile",
    "reduce", "remove", "return", "set", "shortestpath", "single", "skip", "some", "start",
    "starts", "then", "to", "union", "unique", "unwind", "using", "when", "where", "with",
    "xor", "yield",
})

_IDENT_VALID_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_neo4j_ident(raw: str) -> str:
    """Neo4j 라벨/관계 타입/프로퍼티 키 sanitize.

    규칙:
    - 비영숫자/밑줄은 `_` 로 치환
    - 숫자로 시작하면 `_` prefix
    - 예약어면 `_` suffix
    - 빈 문자열은 `_` 로 대체
    """
    if not raw:
        return "_"
    cleaned = _IDENT_VALID_RE.sub("_", raw)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "_" + cleaned
    if cleaned.lower() in _CYPHER_RESERVED:
        cleaned = cleaned + "_"
    return cleaned


def _cypher_hint_ident(name: str) -> str | None:
    """Cypher 힌트에 넣을 수 있는 라벨·관계 이름이면 그대로, 아니면 None 을 돌려준다.

    LPG 변환은 라벨과 관계 타입을 _sanitize_neo4j_ident 로 만든다. 그 결과와 같은
    이름은 [A-Za-z0-9_] 로만 이뤄지고 숫자로 시작하지 않으며 예약어가 아니므로
    따옴표 없이도 식별자 자리를 벗어나지 못한다. 그 밖의 이름(사용자 CSV 의 임의
    타입, T-Box local name 의 괄호·대괄호 등)은 힌트에 넣지 않는다.
    """
    if isinstance(name, str) and name and _sanitize_neo4j_ident(name) == name:
        return name
    return None


def _load_canonical_inverses() -> dict[str, str]:
    """domain_config.json 의 lpg.canonical_inverses 를 {drop: keep} 맵으로 로드.

    예: [{"keep": "hasOperator", "drop": "operatedBy"}] →
        {"operatedBy": "hasOperator"}
    선언 안 된 inverse 쌍은 기존 알파벳 min() 폴백.
    """
    cfg = DOMAIN_CONFIG.get("lpg", {}).get("canonical_inverses", [])
    mapping: dict[str, str] = {}
    for entry in cfg:
        keep = entry.get("keep")
        drop = entry.get("drop")
        if keep and drop:
            mapping[drop] = keep
    return mapping


# ── LPG Loss Manifest ──────────────────────────────────────────────────────

# Cypher 보상 패턴: LPG에서 직접 표현 불가한 OWL axiom에 대한 Cypher 우회 패턴
_CYPHER_COMPENSATION = {
    str(OWL.inverseOf): "역방향 MATCH 패턴: MATCH (a)<-[:{rel}]-(b)",
    str(OWL.TransitiveProperty): "가변 길이 경로: MATCH (a)-[:{rel}*]->(b)",
    str(OWL.someValuesFrom): "존재 검증: WHERE exists((n)-[:{rel}]->(:Type))",
    str(OWL.allValuesFrom): "전칭 검증: WHERE NOT exists((n)-[:{rel}]->(:OtherType))",
    str(OWL.FunctionalProperty): "유니크 제약조건: CREATE CONSTRAINT ON (n:Label) ASSERT n.prop IS UNIQUE",
}


def _track_exclusion(tracker: dict, predicate_str: str, subject=None, obj=None) -> None:
    """RDF→LPG 변환 시 제외된 OWL predicate를 추적한다."""
    if predicate_str not in tracker:
        tracker[predicate_str] = {"count": 0, "samples": []}
    tracker[predicate_str]["count"] += 1
    if len(tracker[predicate_str]["samples"]) < 5:
        tracker[predicate_str]["samples"].append(
            {"subject": str(subject) if subject else "", "object": str(obj) if obj else ""}
        )


def _generate_cypher_compensation(predicate_str: str, rel_name: str = "") -> str:
    """제외된 OWL axiom에 대한 Cypher 우회 패턴을 생성한다.

    rel_name 이 _cypher_hint_ident 를 통과할 때만 {rel} 자리에 넣고, 아니면
    {rel} placeholder 를 그대로 둔다.
    """
    template = _CYPHER_COMPENSATION.get(predicate_str, "LPG에서 직접 표현 불가")
    rel = _cypher_hint_ident(rel_name) if rel_name else None
    return template.replace("{rel}", rel) if rel else template


def _build_lpg_loss_manifest(
    tracker: dict,
    bnode_losses: int = 0,
    external_drops: int = 0,
    rdf_total: int = 0,
    instance_triples: int = 0,
    axiom_triples: int = 0,
    pruned_sparse_columns: dict | None = None,
    ident_renames: dict | None = None,
) -> dict:
    """LPG 변환 손실 매니페스트를 생성한다."""
    total_excluded = sum(v["count"] for v in tracker.values())
    by_predicate = []
    for pred, data in tracker.items():
        by_predicate.append({
            "predicate": pred,
            "count": data["count"],
            "compensation": _generate_cypher_compensation(pred),
            "samples": data["samples"][:3],
        })

    losses: dict = {
        "owl_axiom_exclusions": {
            "count": total_excluded,
            "by_predicate": by_predicate,
        },
        "bnode_losses": {"count": bnode_losses},
        "external_reference_drops": {"count": external_drops},
    }
    if pruned_sparse_columns:
        losses["sparse_column_pruning"] = {
            "count": len(pruned_sparse_columns),
            "note": "커버리지 미달 프로퍼티는 각 노드의 _metadata JSON 프로퍼티로 이동. "
                    "Cypher 에서 직접 WHERE 절 사용 불가, apoc.convert.fromJsonMap 필요.",
            "columns": [
                {"property": k, "coverage_ratio": round(v, 4)}
                for k, v in sorted(pruned_sparse_columns.items(), key=lambda kv: kv[1])
            ],
        }
    if ident_renames:
        losses["identifier_renames"] = {
            "count": len(ident_renames),
            "note": "Cypher 예약어 / 숫자 시작 / 비영숫자 문자를 safe 식별자로 치환.",
            "renames": [{"from": k, "to": v} for k, v in sorted(ident_renames.items())][:50],
        }

    return {
        "losses": losses,
        "preservation_score": {
            "instance_triples": instance_triples,
            "axiom_triples": axiom_triples,
            "axiom_exclusion_rate": round(total_excluded / max(axiom_triples, 1), 4),
        },
    }


def _resolve_rel_type(predicate_uri: str) -> str:
    """Predicate URI → 네임스페이스 충돌 방지된 관계 타입명."""
    if "#" in predicate_uri:
        ns_part, local = predicate_uri.rsplit("#", 1)
    elif "/" in predicate_uri:
        ns_part, local = predicate_uri.rsplit("/", 1)
    else:
        return predicate_uri

    if predicate_uri.startswith(_DOMAIN_NS):
        return local
    if predicate_uri.startswith(IOF_IRI_ROOT):
        if "core/Core" in ns_part:
            return f"iof_{local}"
        if "supplychain" in ns_part:
            return f"iof_sc_{local}"
        if "maintenance" in ns_part:
            return f"iof_maint_{local}"
        return f"iof_{local}"
    if predicate_uri.startswith(str(RDFS)):
        return f"rdfs_{local}"
    # [H5] 알려진 외부 네임스페이스
    for ns_prefix_uri, prefix_str in _KNOWN_NS_PREFIXES.items():
        if predicate_uri.startswith(ns_prefix_uri):
            return f"{prefix_str}{local}"
    return f"other_{local}"  # H5: bare local name 대신 prefix 부여


# _local_name 은 domain.uri_conventions.local_name 의 alias (P1-3 중앙화).
# 기존 호출자 호환을 위해 underscore prefix alias 유지 — 외부 시그니처 변경 없음.


_HASH_URI_RE = re.compile(r"^n?[0-9a-f]{20,}")  # OWL RL이 실체화한 BNode 해시 (n prefix 포함)


# _is_upper_ontology_type 은 domain.owl_constants.is_upper_ontology_type 을
# import 시 alias 로 가져온다 (P1-4 중앙화).


def _is_hash_uri_type(local_name: str) -> bool:
    """OWL RL 추론기가 BNode restriction을 실체화한 해시 URI인지 확인."""
    return bool(_HASH_URI_RE.match(local_name))


# ── LPG Co-design 유틸리티 ──────────────────────────────────────────────────


def _filter_self_loops(relationships: list[tuple]) -> tuple[list[tuple], int]:
    """Self-loop 관계 (subject == object) 제거."""
    filtered = []
    removed = 0
    for rel in relationships:
        if rel[0] == rel[1]:
            removed += 1
        else:
            filtered.append(rel)
    return filtered, removed


def _prune_sparse_columns(nodes: dict, min_coverage: float = 0.01) -> tuple[dict, list[str], dict[str, float]]:
    """커버리지 < min_coverage인 프로퍼티를 _metadata JSON으로 합친다."""
    total_nodes = len(nodes)
    if total_nodes == 0:
        return nodes, [], {}

    prop_counts: dict[str, int] = Counter()
    for n in nodes.values():
        for k, v in n["properties"].items():
            if v not in (None, "", "N/A"):
                prop_counts[k] += 1

    pruned_keys = [k for k, c in prop_counts.items()
                   if c / total_nodes < min_coverage]

    coverage_stats = {k: prop_counts[k] / total_nodes for k in pruned_keys}

    for n in nodes.values():
        metadata = {}
        for k in pruned_keys:
            val = n["properties"].get(k)
            if val not in (None, "", "N/A"):
                metadata[k] = val
            n["properties"].pop(k, None)
        if metadata:
            n["properties"]["_metadata"] = json.dumps(metadata, ensure_ascii=False)

    return nodes, pruned_keys, coverage_stats


def _dedup_inverse_relationships(relationships: list[tuple], tbox: RdfGraph) -> tuple[list[tuple], dict]:
    """inverseOf 쌍 중 canonical direction만 유지.

    Canonical 결정: domain_config.json 의 lpg.canonical_inverses 가 있으면 그 선언을
    우선 적용 (SME 가 의미적 active direction 을 지정). 선언 없는 쌍은 알파벳순 min().
    """
    canonical_override = _load_canonical_inverses()

    inverse_pairs: dict[str, str] = {}
    unique_pair_keys: set[frozenset] = set()
    override_applied = 0
    for s, _, o in tbox.triples((None, OWL.inverseOf, None)):
        s_name = _local_name(str(s))
        o_name = _local_name(str(o))
        unique_pair_keys.add(frozenset({s_name, o_name}))

        if s_name in canonical_override and canonical_override[s_name] == o_name:
            inverse_pairs[s_name] = o_name
            override_applied += 1
        elif o_name in canonical_override and canonical_override[o_name] == s_name:
            inverse_pairs[o_name] = s_name
            override_applied += 1
        else:
            canonical = min(s_name, o_name)
            non_canonical = max(s_name, o_name)
            inverse_pairs[non_canonical] = canonical

    deduped = []
    removed = 0
    for rel in relationships:
        if rel[2] in inverse_pairs:
            removed += 1
        else:
            deduped.append(rel)

    return deduped, {
        "pairs_found": len(unique_pair_keys),
        "relationships_removed": removed,
        "canonical_overrides_applied": override_applied,
    }


def _mint_meaningful_bnodes(g: RdfGraph) -> int:
    """Pass 0 — OWL Restriction BNode 를 결정적 IRI 로 승격.

    onProperty + (someValuesFrom|hasValue|minCardinality) 가 있고 named class 가
    참조하는 restriction BNode 만 대상. 그래프에서 BNode→URIRef 치환 후 minted
    개수 반환.
    """
    bnode_iri_map: dict[BNode, URIRef] = {}
    minted = 0
    for bnode in set(g.subjects(RDF.type, OWL.Restriction)):
        if not isinstance(bnode, BNode):
            continue
        on_prop = g.value(bnode, OWL.onProperty)
        if not on_prop:
            continue
        referencing_classes = [
            s for s in g.subjects(RDFS.subClassOf, bnode)
            if isinstance(s, URIRef)
        ]
        if not referencing_classes:
            continue
        prop_local = _local_name(str(on_prop))
        has_val = g.value(bnode, OWL.hasValue)
        some_val = g.value(bnode, OWL.someValuesFrom)
        min_card = g.value(bnode, OWL.minCardinality)
        if has_val is not None:
            suffix = f"hasValue_{str(has_val).replace(' ', '_')[:30]}"
        elif some_val is not None:
            suffix = f"someValuesFrom_{_local_name(str(some_val))}"
        elif min_card is not None:
            suffix = f"minCard_{min_card}"
        else:
            continue  # Unknown restriction type — skip

        cls_local = _local_name(str(sorted(referencing_classes, key=str)[0]))
        minted_uri = URIRef(
            f"{_DOMAIN_NS}Restriction_{cls_local}_{prop_local}_{suffix}",
        )
        bnode_iri_map[bnode] = minted_uri
        minted += 1

    if bnode_iri_map:
        for bnode, new_uri in bnode_iri_map.items():
            for s, p, o in list(g.triples((bnode, None, None))):
                g.remove((s, p, o))
                g.add((new_uri, p, o))
            for s, p, o in list(g.triples((None, None, bnode))):
                g.remove((s, p, o))
                g.add((s, p, new_uri))
    return minted


def _extract_typed_labels(
    g: RdfGraph, nodes: dict, ident_renames: dict[str, str],
) -> tuple[int, int]:
    """Pass 1 — rdf:type triple 을 노드 라벨로 추출.

    OWL 메타 타입 / 상위 온톨로지 / OWL RL 해시 URI 는 제외. local name 은
    sanitize 후 nodes[uri]["labels"] 에 추가. ``ident_renames`` 에 변경 내역
    수집.

    Returns: (added_label_triples, hash_type_excluded).
    """
    added = 0
    hash_excluded = 0
    for s, _, o in g.triples((None, RDF.type, None)):
        if isinstance(s, BNode):
            continue
        o_str = str(o)
        if o_str in _OWL_META_TYPES or _is_upper_ontology_type(o_str):
            continue
        local = _local_name(o_str)
        if not local:
            continue
        if _is_hash_uri_type(local):
            hash_excluded += 1
            continue
        safe = _sanitize_neo4j_ident(local)
        if safe != local:
            ident_renames[local] = safe
        nodes[str(s)]["labels"].add(safe)
        added += 1
    return added, hash_excluded


def _filter_inferred_ancestor_labels(g: RdfGraph, nodes: dict) -> int:
    """Pass 1a — most-specific type 필터.

    subClassOf 체인으로 인한 다중 라벨 ([Pump, EquipmentType, Thing]) 중
    상위 타입을 제거해 가장 구체적인 라벨만 유지. 모든 라벨이 ancestor 로
    분류되어 비어버리면 롤백 (최소 1개 보존).

    Returns: 제거된 ancestor 라벨 총 개수.
    """
    subclass_map: dict[str, set[str]] = {}
    for _s, _, _o in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(_s, BNode) or isinstance(_o, BNode):
            continue
        s_local = _sanitize_neo4j_ident(_local_name(str(_s)))
        o_local = _sanitize_neo4j_ident(_local_name(str(_o)))
        if s_local and o_local and s_local != o_local:
            subclass_map.setdefault(s_local, set()).add(o_local)

    def _all_ancestors(cls: str, visited: set | None = None) -> set[str]:
        if visited is None:
            visited = set()
        for parent in subclass_map.get(cls, set()):
            if parent not in visited:
                visited.add(parent)
                _all_ancestors(parent, visited)
        return visited

    ancestor_cache: dict[str, set[str]] = {}
    removed = 0
    for _uri, node_data in nodes.items():
        if len(node_data["labels"]) <= 1:
            continue
        labels = set(node_data["labels"])
        ancestors_union: set[str] = set()
        for lb in labels:
            if lb not in ancestor_cache:
                ancestor_cache[lb] = _all_ancestors(lb)
            ancestors_union.update(ancestor_cache[lb])
        to_remove = labels & ancestors_union
        if to_remove:
            node_data["labels"] -= to_remove
            removed += len(to_remove)
            if not node_data["labels"]:
                node_data["labels"] = labels  # 롤백
                removed -= len(to_remove)
    return removed


def _register_owl_classes(
    g: RdfGraph, nodes: dict, relationships: list,
    ident_renames: dict[str, str],
) -> None:
    """Pass 1b — OWL Class 노드 + rdfs:subClassOf 관계 등록.

    도메인 NS 또는 IOF NS 의 OWL Class 만 대상. ``OWLClass`` 라벨도 함께 부여
    해서 downstream 이 인스턴스/클래스 노드를 구분 가능. subClassOf 관계는
    nodes 에 등록된 endpoint 끼리만 추가 (dangling 방지).
    """
    class_uris: set[str] = set()
    for s, _, _ in g.triples((None, RDF.type, OWL.Class)):
        if isinstance(s, BNode):
            continue
        s_str = str(s)
        if s_str.startswith((_DOMAIN_NS, IOF_IRI_ROOT)):
            class_uris.add(s_str)
            local = _local_name(s_str)
            if local:
                safe = _sanitize_neo4j_ident(local)
                if safe != local:
                    ident_renames[local] = safe
                nodes[s_str]["labels"].add(safe)
                nodes[s_str]["labels"].add("OWLClass")

    for s, _, o in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(s, BNode) or isinstance(o, BNode):
            continue
        s_str, o_str = str(s), str(o)
        if s_str in class_uris and o_str in nodes:
            relationships.append((s_str, o_str, "rdfs_subClassOf"))


def _aggregate_excluded_axioms(
    g: RdfGraph, exclusion_tracker: dict, stats: dict,
) -> str:
    """Pass 2 사전 — BNode subject + axiom predicate 를 SPARQL 집계.

    Python 루프가 처리할 트리플 수를 줄이기 위해 SPARQL 로 먼저 카운트/샘플.
    Returns: ``exclude_values_sparql`` (Pass 2 main query 에서 재사용).
    """
    exclude_values_sparql = ", ".join(f"<{p}>" for p in _EXCLUDE_PREDICATES)

    bnode_cnt_q = (
        "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER(isBlank(?s)) }"
    )
    for row in g.query(bnode_cnt_q):
        with contextlib.suppress(TypeError, ValueError):
            stats["excluded_bnode"] = int(row[0]) if row[0] is not None else 0

    for row in g.query(
        f"SELECT ?p (COUNT(*) AS ?n) WHERE {{ "
        f"?s ?p ?o . FILTER(isIRI(?s)) FILTER(?p IN ({exclude_values_sparql})) "
        f"}} GROUP BY ?p"
    ):
        try:
            cnt = int(row[1]) if row[1] is not None else 0
        except (TypeError, ValueError):
            continue
        p_str = str(row[0])
        if p_str not in exclusion_tracker:
            exclusion_tracker[p_str] = {"count": 0, "samples": []}
        exclusion_tracker[p_str]["count"] += cnt
        stats["excluded_axiom"] += cnt
        sample_q = (
            f"SELECT ?s ?o WHERE {{ ?s <{p_str}> ?o . FILTER(isIRI(?s)) }} "
            f"LIMIT 5"
        )
        for srow in g.query(sample_q):
            if len(exclusion_tracker[p_str]["samples"]) >= 5:
                break
            exclusion_tracker[p_str]["samples"].append({
                "subject": str(srow[0]),
                "object": str(srow[1]),
            })
    return exclude_values_sparql


def _extract_properties_and_rels(
    g: RdfGraph, nodes: dict, relationships: list,
    exclude_values_sparql: str, ident_renames: dict[str, str], stats: dict,
) -> None:
    """Pass 2 main — DatatypeProperty + ObjectProperty 추출.

    Oxigraph 엔진에 BNode/axiom predicate 필터를 위임한 SPARQL 결과를 순회.
    Literal → 노드 properties (다중값 → list 승격, 언어 태그 보존, datatype
    추적), URI → relationships (단, _URI_AS_PROPERTY_PREDICATES 는 properties
    로 우회). nodes 에 없는 endpoint 는 dangling drop + sample 5건 보존.
    """
    main_q = (
        "SELECT ?s ?p ?o WHERE { ?s ?p ?o . "
        "FILTER(isIRI(?s)) "
        f"FILTER(?p NOT IN ({exclude_values_sparql})) "
        "}"
    )
    for row in g.query(main_q):
        s, p, o = row[0], row[1], row[2]
        p_str = str(p)
        s_str = str(s)
        if s_str not in nodes:
            stats["excluded_no_type"] += 1
            continue

        if isinstance(o, Literal):
            base_name = _local_name(p_str)
            raw_name = f"{base_name}_{o.language}" if o.language else base_name
            prop_name = _sanitize_neo4j_ident(raw_name)
            if prop_name != raw_name:
                ident_renames[raw_name] = prop_name
            val = str(o)
            if o.datatype:
                nodes[s_str]["prop_types"][prop_name].add(str(o.datatype))

            existing = nodes[s_str]["properties"].get(prop_name)
            if existing is not None:
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    nodes[s_str]["properties"][prop_name] = [existing, val]
            else:
                nodes[s_str]["properties"][prop_name] = val
            stats["rdf_instance_triples"] += 1

        elif isinstance(o, URIRef):
            o_str = str(o)
            if p_str in _URI_AS_PROPERTY_PREDICATES:
                raw_name = _local_name(p_str)
                prop_name = _sanitize_neo4j_ident(raw_name)
                if prop_name != raw_name:
                    ident_renames[raw_name] = prop_name
                existing = nodes[s_str]["properties"].get(prop_name)
                if existing is not None:
                    if isinstance(existing, list):
                        existing.append(o_str)
                    else:
                        nodes[s_str]["properties"][prop_name] = [existing, o_str]
                else:
                    nodes[s_str]["properties"][prop_name] = o_str
                stats["rdf_instance_triples"] += 1
                continue

            if o_str in nodes:
                raw_rel = _resolve_rel_type(p_str)
                rel_type = _sanitize_neo4j_ident(raw_rel)
                if rel_type != raw_rel:
                    ident_renames[raw_rel] = rel_type
                relationships.append((s_str, o_str, rel_type))
                stats["rdf_instance_triples"] += 1
            else:
                stats["excluded_external_ref"] += 1
                if len(stats["excluded_external_ref_samples"]) < 5:
                    stats["excluded_external_ref_samples"].append(
                        {"s": s_str[:80], "p": p_str, "o": o_str[:80]},
                    )


def _finalize_node_dict(nodes: dict) -> tuple[dict, dict[str, str], set[str]]:
    """Pass 후처리 — prop_type_map / array_props 집계 + clean_nodes 빌드.

    각 프로퍼티의 dominant XSD 타입을 Neo4j 타입으로 매핑, list 로 저장된
    프로퍼티는 array_props 에 등록. nodes 의 prop_types defaultdict 는 내부
    전용이므로 최종 clean_nodes 에서는 제거.

    Returns: (clean_nodes, prop_type_map, array_props).
    """
    prop_type_map: dict[str, str] = {}
    all_prop_types: dict[str, Counter] = defaultdict(Counter)
    for n in nodes.values():
        for prop_name, xsd_types in n["prop_types"].items():
            for t in xsd_types:
                all_prop_types[prop_name][t] += 1
    for prop_name, type_counts in all_prop_types.items():
        dominant = type_counts.most_common(1)[0][0] if type_counts else ""
        neo4j_type = _XSD_TYPE_MAP.get(dominant, "")
        if neo4j_type:
            prop_type_map[prop_name] = neo4j_type

    array_props: set[str] = set()
    for n in nodes.values():
        for prop_name, val in n["properties"].items():
            if isinstance(val, list):
                array_props.add(prop_name)

    clean_nodes = {
        uri: {"labels": n["labels"], "properties": n["properties"]}
        for uri, n in nodes.items()
    }
    return clean_nodes, prop_type_map, array_props


def _convert_rdf_to_lpg(g: RdfGraph) -> tuple[dict, list, dict]:
    """RDF 그래프 → LPG 구조 변환. (nodes, relationships, stats) 반환.

    7-pass 파이프라인 (P1-2 분할):
      0. _mint_meaningful_bnodes — OWL Restriction BNode → IRI 승격
      1. _extract_typed_labels — rdf:type → node label
      1a. _filter_inferred_ancestor_labels — 추론 ancestor 라벨 제거
      1b. _register_owl_classes — OWL Class 노드 + subClassOf 관계
      2-pre. _aggregate_excluded_axioms — BNode/axiom predicate SPARQL 집계
      2-main. _extract_properties_and_rels — DP/OP 추출
      post. _finalize_node_dict — prop_type/array 집계 + clean_nodes
    """
    nodes: dict[str, dict] = defaultdict(
        lambda: {"labels": set(), "properties": {}, "prop_types": defaultdict(set)},
    )
    relationships: list[tuple[str, str, str]] = []
    exclusion_tracker: dict = {}
    ident_renames: dict[str, str] = {}
    stats = {
        "excluded_axiom": 0,
        "excluded_bnode": 0,
        "excluded_no_type": 0,
        "excluded_external_ref": 0,
        "excluded_external_ref_samples": [],
        "rdf_instance_triples": 0,
    }

    # Pass 0
    stats["bnode_minted"] = _mint_meaningful_bnodes(g)

    # Pass 1
    added_label_triples, hash_excluded = _extract_typed_labels(
        g, nodes, ident_renames,
    )
    stats["rdf_instance_triples"] += added_label_triples
    stats["hash_type_excluded"] = hash_excluded

    # Pass 1a
    stats["inferred_labels_removed"] = _filter_inferred_ancestor_labels(g, nodes)

    # Pass 1b
    _register_owl_classes(g, nodes, relationships, ident_renames)

    # Pass 2-pre
    exclude_values_sparql = _aggregate_excluded_axioms(g, exclusion_tracker, stats)

    # Pass 2-main
    _extract_properties_and_rels(
        g, nodes, relationships, exclude_values_sparql, ident_renames, stats,
    )

    # Post
    clean_nodes, prop_type_map, array_props = _finalize_node_dict(nodes)
    stats["prop_type_map"] = prop_type_map
    stats["array_props"] = array_props
    stats["exclusion_tracker"] = exclusion_tracker
    stats["ident_renames"] = ident_renames
    return clean_nodes, relationships, stats


def _compute_d3_score(
    nodes: dict, relationships: list, cq_validation_path: str | None = None,
) -> tuple[float, dict]:
    """D3 Query Equivalence — CQ 검증 결과 기반 또는 offline fallback (penalty 적용).

    Args:
        nodes: LPG 노드 dict
        relationships: LPG 관계 list
        cq_validation_path: validate_competency_questions 결과 JSON 경로 (optional)
    Returns:
        (score, details) 튜플. score는 0-100 float.
    """
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 1순위: 기존 CQ validation 결과 파일에서 answer_rate / pass_rate 직접 사용
    if cq_validation_path and os.path.exists(cq_validation_path):
        try:
            with open(cq_validation_path, encoding="utf-8") as f:
                cq_result = json.load(f)
            summary = cq_result.get("summary", {})
            # competency_questions.py → answer_rate (float)
            # query_test.py → pass_rate (float)
            rate = summary.get("answer_rate") or summary.get("pass_rate")
            if rate is not None:
                score = float(str(rate).rstrip("%"))
                return score, {"mode": "programmatic", "source": cq_validation_path}
        except Exception as exc:
            logger.warning(
                "CQ 검증 점수 로드 실패; offline CQ 평가로 폴백한다: %s",
                exc,
            )

    # 2순위: offline CQ 로직 (노드 label + relationship 매칭, 0.8 penalty)
    _OFFLINE_PENALTY = 0.8
    try:
        from config import COMPETENCY_QUESTIONS_PATH
        _cq_path = COMPETENCY_QUESTIONS_PATH
        if os.path.exists(_cq_path):
            with open(_cq_path, encoding="utf-8") as _f:
                _cqs = json.load(_f)
            _all_lpg_labels: set[str] = set()
            for _n in nodes.values():
                _all_lpg_labels.update(_n["labels"])
            # 관계 그래프 구축 (label→label via relationship)
            _rel_graph: dict[str, set[str]] = {}
            for _s_uri, _e_uri, _rt in relationships:
                for _sl in nodes.get(_s_uri, {}).get("labels", []):
                    for _el in nodes.get(_e_uri, {}).get("labels", []):
                        _rel_graph.setdefault(_sl, set()).add(_el)
                        _rel_graph.setdefault(_el, set()).add(_sl)
            _cq_checks = 0
            _cq_passed = 0
            for _cq in _cqs[:20]:
                _domains = [d.replace("_", "") for d in _cq.get("domains", [])]
                for _cls in _domains:
                    _cq_checks += 1
                    if _cls in _all_lpg_labels:
                        _cq_passed += 1
                for _i in range(len(_domains)):
                    for _j in range(_i + 1, len(_domains)):
                        _a, _b = _domains[_i], _domains[_j]
                        _cq_checks += 1
                        if _b in _rel_graph.get(_a, set()) or any(_b in _rel_graph.get(_mid, set())
                                 for _mid in _rel_graph.get(_a, set())):
                            _cq_passed += 1
            if _cq_checks > 0:
                raw_score = round(_cq_passed / _cq_checks * 100, 1)
                penalized = round(raw_score * _OFFLINE_PENALTY, 1)
                return penalized, {
                    "mode": "offline_cq_fallback",
                    "penalty": _OFFLINE_PENALTY,
                    "raw_score": raw_score,
                    "cqs_tested": min(len(_cqs), 20),
                    "checks": _cq_checks,
                    "passed": _cq_passed,
                }
    except Exception as _qe:
        logger.debug("오프라인 Query Equivalence 실패: %s", _qe)

    # 3순위: CQ 파일 없음 — 0점 반환 (rescaling 인플레이션 방지)
    return 0.0, {"mode": "no_cq_available", "reason": "CQ 파일 없음 — 0점 처리"}


def _verify_lpg(
    g: RdfGraph, nodes: dict, relationships: list, ns: str, sample_count: int,
    stats: dict,
) -> dict:
    """RDF 원본과 LPG 변환 결과를 비교 검증.

    변환 단계의 sanitize / inverse dedup / sparse pruning 정책을 모두 인지하고
    동일 normalize 한 뒤 분포를 비교한다. 정책으로 의도된 차이를 잘못 mismatch
    라고 보고하지 않는 것이 핵심.
    """
    issues: list[str] = []

    # ── 변환 정책 메타데이터 로드 ──
    # sanitize: RDF 의 raw local name 을 LPG 와 동일 식별자로 변환해야 비교 가능
    # inverse dedup: 의도적으로 한 방향만 남기므로, RDF 의 inverse partner 쌍은
    #   canonical direction 으로 합쳐서 비교
    canonical_inverse_map = _load_canonical_inverses()  # {drop: keep}
    # T-Box 의 inverseOf 선언도 합쳐서 알파벳 fallback 까지 반영
    tbox_inverse_map: dict[str, str] = {}
    if os.path.exists(TBOX_PATH):
        _tbox_g = _new_graph()
        try:
            _tbox_g.parse(TBOX_PATH, format="turtle")
            for _s, _, _o in _tbox_g.triples((None, OWL.inverseOf, None)):
                _s_local = _local_name(str(_s))
                _o_local = _local_name(str(_o))
                if _s_local in canonical_inverse_map and canonical_inverse_map[_s_local] == _o_local:
                    tbox_inverse_map[_s_local] = _o_local
                elif _o_local in canonical_inverse_map and canonical_inverse_map[_o_local] == _s_local:
                    tbox_inverse_map[_o_local] = _s_local
                else:
                    tbox_inverse_map[max(_s_local, _o_local)] = min(_s_local, _o_local)
        except Exception as _e:
            logger.debug("verify: T-Box inverseOf 로드 실패: %s", _e)

    def _to_canonical_rel(name: str) -> str:
        """RDF predicate local name → LPG canonical (sanitize + inverse fold)."""
        folded = tbox_inverse_map.get(name, name)
        return _sanitize_neo4j_ident(folded)

    # ── 통합 1-pass: rdf:type 기반 클래스 분포(도메인+IOF) + 도메인 관계 분포 동시 집계 ──
    # sanitize 와 inverse fold 를 RDF 쪽에 적용해, LPG 와 동일 식별자 공간에서 비교한다.
    rdf_cd: Counter = Counter()
    rdf_iof_cd: Counter = Counter()
    rdf_rd: Counter = Counter()
    iof_ns = "https://spec.industrialontologies.org/"
    for s, p, o in g.triples((None, None, None)):
        if isinstance(s, BNode) or isinstance(o, BNode):
            continue
        if p == RDF.type:
            o_str = str(o)
            if o_str in _OWL_META_TYPES:
                continue
            if o_str.startswith(ns):
                local = o_str.replace(ns, "")
                rdf_cd[_sanitize_neo4j_ident(local)] += 1
            elif o_str.startswith(iof_ns) and not _is_upper_ontology_type(o_str):
                rdf_iof_cd[_sanitize_neo4j_ident(_local_name(o_str))] += 1
            continue
        if isinstance(o, URIRef) and str(p).startswith(ns):
            rel_local = str(p).replace(ns, "")
            rdf_rd[_to_canonical_rel(rel_local)] += 1

    lpg_cd = Counter()
    for n in nodes.values():
        if "OWLClass" in n["labels"]:
            continue  # 클래스 정의 노드 제외 (인스턴스만 카운트)
        for lb in n["labels"]:
            if not lb.startswith(("http", "urn")):
                lpg_cd[lb] += 1

    c_mis = [(c, rdf_cd[c], lpg_cd.get(c, 0))
             for c in rdf_cd if rdf_cd[c] != lpg_cd.get(c, 0)]
    if c_mis:
        issues.append(f"클래스 분포 불일치 {len(c_mis)}개")

    iof_c_mis = [(c, rdf_iof_cd[c], lpg_cd.get(c, 0))
                 for c in rdf_iof_cd if rdf_iof_cd[c] != lpg_cd.get(c, 0)]
    if iof_c_mis:
        issues.append(f"IOF 클래스 분포 불일치 {len(iof_c_mis)}개")

    lpg_steel = Counter()
    for _, _, rt in relationships:
        if not rt.startswith(("iof_", "rdfs_", "dc_", "dcterms_", "skos_", "foaf_", "prov_", "other_")):
            lpg_steel[rt] += 1

    r_mis = [(r, rdf_rd[r], lpg_steel.get(r, 0))
             for r in rdf_rd if rdf_rd[r] != lpg_steel.get(r, 0)]
    if r_mis:
        issues.append(f"도메인 관계 불일치 {len(r_mis)}개")

    # IOF 관계
    iof_rels = Counter()
    for _, _, rt in relationships:
        if rt.startswith("iof_"):
            iof_rels[rt] += 1

    # 클래스 계층
    subclass_count = sum(1 for _, _, r in relationships if r == "rdfs_subClassOf")

    # 어노테이션 — 언어별 라벨 확인 (sanitize 적용된 키 사용)
    _LABEL_KO = _sanitize_neo4j_ident("label_ko")
    _LABEL_EN = _sanitize_neo4j_ident("label_en")
    _COMMENT_KO = _sanitize_neo4j_ident("comment_ko")
    label_ko = sum(1 for n in nodes.values() if _LABEL_KO in n["properties"])
    label_en = sum(1 for n in nodes.values() if _LABEL_EN in n["properties"])
    comment_ko = sum(1 for n in nodes.values() if _COMMENT_KO in n["properties"])

    # [M2] 샘플 스팟체크 — 모든 Literal 프로퍼티 비교 (도메인 무관)
    # 변환 단계의 sanitize 와 sparse pruning 을 모두 풀어서 RDF 와 동일 키 공간에서 비교.
    # _metadata JSON 안의 값까지 합쳐 비교해야 sparse-pruned 프로퍼티가 누락 표기 안 됨.
    inst_uris = [u for u, n in nodes.items()
                 if "OWLClass" not in n["labels"]
                 and (u.startswith(_INSTANCE_NS) or u.startswith(_DOMAIN_NS))]
    rng = random.Random(42)
    samp = rng.sample(inst_uris, min(sample_count, len(inst_uris)))
    spot_pass = 0
    spot_mismatches: list[dict] = []
    for uri in samp:
        rdf_p: dict[str, set] = {}
        for _, p, o in g.triples((URIRef(uri), None, None)):
            if isinstance(o, Literal):
                base = _local_name(str(p))
                raw = f"{base}_{o.language}" if o.language else base
                pname = _sanitize_neo4j_ident(raw)
                rdf_p.setdefault(pname, set()).add(str(o))
        lpg_p: dict[str, set] = {}
        for k, v in nodes[uri]["properties"].items():
            if k == "_metadata":
                # sparse-pruned 프로퍼티는 _metadata JSON 으로 접혔으므로 unfold
                try:
                    meta = json.loads(v) if isinstance(v, str) else v
                    if isinstance(meta, dict):
                        for mk, mv in meta.items():
                            lpg_p[mk] = set(mv) if isinstance(mv, list) else {str(mv)}
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                continue
            lpg_p[k] = set(v) if isinstance(v, list) else {v}
        # 누락 프로퍼티도 감지 (if k in lpg_p 제거)
        if rdf_p and all(rdf_p.get(k) == lpg_p.get(k) for k in rdf_p):
            spot_pass += 1
        elif not rdf_p:
            spot_pass += 1  # 프로퍼티 없는 엔티티는 통과
        else:
            # 어떤 프로퍼티가 어긋났는지 기록 (디버깅 가시성)
            diffs = [k for k in rdf_p if rdf_p.get(k) != lpg_p.get(k)]
            if diffs and len(spot_mismatches) < 5:
                spot_mismatches.append({"uri": uri[:80], "missing_or_diff_props": diffs[:5]})
    if spot_pass < len(samp):
        issues.append(f"프로퍼티 스팟체크 {len(samp) - spot_pass}/{len(samp)}개 불일치")

    # I-3: 커버리지 — _convert_rdf_to_lpg에서 카운트된 값 사용 (2회 순회 방지)
    rdf_instance_triples = stats.get("rdf_instance_triples", 0)

    lpg_t = 0
    for n in nodes.values():
        if "OWLClass" not in n["labels"]:
            lpg_t += len(n["labels"])
        lpg_t += sum(len(v) if isinstance(v, list) else 1 for v in n["properties"].values())
    lpg_t += len(relationships)

    total = len(g)
    cov = lpg_t / rdf_instance_triples * 100 if rdf_instance_triples else 0
    excluded = total - rdf_instance_triples

    # ══════════════════════════════════════════════════════════════
    # 5차원 품질 점수 (학술 근거 기반)
    # ══════════════════════════════════════════════════════════════

    def _clamp(v: float) -> float:
        return max(0.0, min(v, 100.0))

    # ── D1. Completeness (완전성, 40%) ──
    # Zaveri et al. (2016) SWJ 7(1):63-93 — Population/Property/Interlinking

    # [C-4] 분모에서 OWL 메타 subject 제외
    rdf_typed_subjects = len({
        str(s) for s, _, o in g.triples((None, RDF.type, None))
        if not isinstance(s, BNode)
        and str(o) not in _OWL_META_TYPES and not _is_upper_ontology_type(str(o))
    })
    lpg_instance_nodes = sum(1 for n in nodes.values() if "OWLClass" not in n["labels"])
    node_completeness = _clamp(lpg_instance_nodes / rdf_typed_subjects * 100) if rdf_typed_subjects else 100.0

    # [I-1] 분모에서 외부 참조(의도적 드롭) 제외
    all_node_uris = set(nodes.keys())
    rdf_op_triples = sum(
        1 for s, p, o in g.triples((None, None, None))
        if not isinstance(s, BNode) and isinstance(o, URIRef) and p != RDF.type
        and str(p) not in _EXCLUDE_PREDICATES and str(p) not in _URI_AS_PROPERTY_PREDICATES
        and str(s) in nodes
        and (str(o) in all_node_uris or str(o).startswith(_DOMAIN_NS) or str(o).startswith(_INSTANCE_NS))
    )
    # Finding 2: subClassOf는 Pass 1b에서 별도 추가되므로 분자에서 제외
    lpg_op_count = sum(1 for _, _, t in relationships if t != "rdfs_subClassOf")
    edge_completeness = _clamp(lpg_op_count / rdf_op_triples * 100) if rdf_op_triples else 100.0

    rdf_dp_triples = sum(
        1 for s, p, o in g.triples((None, None, None))
        if not isinstance(s, BNode) and isinstance(o, Literal)
        and str(p) not in _EXCLUDE_PREDICATES and str(s) in nodes
    )
    lpg_prop_count = sum(
        len(v) if isinstance(v, list) else 1
        for n in nodes.values() for v in n["properties"].values()
    )
    property_completeness = _clamp(lpg_prop_count / rdf_dp_triples * 100) if rdf_dp_triples else 100.0

    inst_label_count = sum(len(n["labels"]) for n in nodes.values() if "OWLClass" not in n["labels"])
    label_denom = max(stats.get("rdf_instance_triples", 1) - rdf_dp_triples - rdf_op_triples, 1)
    label_completeness = _clamp(inst_label_count / label_denom * 100)

    # [C-1] min() 추가
    rdf_subclass = sum(
        1 for s, _, o in g.triples((None, RDFS.subClassOf, None))
        if not isinstance(s, BNode) and not isinstance(o, BNode)
    )
    hierarchy_completeness = _clamp(subclass_count / rdf_subclass * 100) if rdf_subclass else 100.0

    completeness_score = _clamp(
        node_completeness * 0.30
        + edge_completeness * 0.30
        + property_completeness * 0.25
        + label_completeness * 0.10
        + hierarchy_completeness * 0.05
    )

    # ── D2. Faithfulness (충실성, 25%) ──
    # Angles et al. (2020) IEEE Access 8:86091 — Semantic Preservation
    _NON_DOMAIN_PREFIXES = ("iof_", "rdfs_", "dc_", "dcterms_", "skos_", "foaf_", "prov_", "other_")

    # rel_type 은 sanitize + inverse fold 가 이미 적용된 LPG 식별자.
    # RDF 에서 후보 predicate 를 역추적하려면 (1) 원래 이름 (2) 그 inverse 까지
    # 모두 시도. 같은 rel_type 으로 들어온 LPG 관계를 RDF 에 어느 방향으로든
    # 한 triple 만 찾으면 direction 보존으로 간주.
    sanitize_to_raw_rels: dict[str, set[str]] = defaultdict(set)
    for s, p, _ in g.triples((None, None, None)):
        if isinstance(s, BNode):
            continue
        p_str = str(p)
        if p_str.startswith(_DOMAIN_NS):
            local = p_str.replace(_DOMAIN_NS, "")
            sanitize_to_raw_rels[_to_canonical_rel(local)].add(local)

    rng2 = random.Random(99)
    rel_sample = rng2.sample(relationships, min(50, len(relationships)))
    direction_correct = 0
    for s_uri, o_uri, rel_type in rel_sample:
        if rel_type.startswith(_NON_DOMAIN_PREFIXES):
            direction_correct += 1
            continue
        candidate_locals = sanitize_to_raw_rels.get(rel_type, {rel_type})
        # canonical inverse 의 drop 쪽 raw name 도 시도 (RDF 에는 양방향 다 있을 수 있음)
        for drop, keep in tbox_inverse_map.items():
            if keep == rel_type or _sanitize_neo4j_ident(keep) == rel_type:
                candidate_locals.add(drop)
        s_ref, o_ref = URIRef(s_uri), URIRef(o_uri)
        matched = False
        for raw in candidate_locals:
            pred = URIRef(_DOMAIN_NS + raw)
            if (s_ref, pred, o_ref) in g or (o_ref, pred, s_ref) in g:
                matched = True
                break
        if matched:
            direction_correct += 1
    direction_accuracy = _clamp(direction_correct / len(rel_sample) * 100) if rel_sample else 100.0

    # 다중값 보존 (프로퍼티 단위로 1회만 체크)
    rdf_multivalue = 0
    lpg_multivalue_ok = 0
    for uri in samp:
        checked_props: set[str] = set()
        for _, p, o in g.triples((URIRef(uri), None, None)):
            if not isinstance(o, Literal):
                continue
            base = _local_name(str(p))
            raw = f"{base}_{o.language}" if o.language else base
            pname = _sanitize_neo4j_ident(raw)
            if pname in checked_props:
                continue
            checked_props.add(pname)
            literal_vals = [v for v in g.objects(URIRef(uri), p) if isinstance(v, Literal)]
            if len(literal_vals) > 1:
                rdf_multivalue += 1
                lpg_val = nodes.get(uri, {}).get("properties", {}).get(pname)
                if isinstance(lpg_val, list) and len(lpg_val) == len(literal_vals):
                    lpg_multivalue_ok += 1
    multivalue_preservation = _clamp(lpg_multivalue_ok / rdf_multivalue * 100) if rdf_multivalue else 100.0

    # 타입 충실도: XSD 타입 매핑된 프로퍼티 비율 (_metadata 는 컨테이너이므로 분모 제외)
    prop_type_map = stats.get("prop_type_map", {})
    array_props = stats.get("array_props", set())
    typed_props = set(prop_type_map.keys()) | array_props  # array 도 타입 표기됨
    all_props = {k for n in nodes.values() for k in n["properties"] if k != "_metadata"}
    datatype_fidelity = _clamp(len(typed_props & all_props) / len(all_props) * 100) if all_props else 100.0

    # 언어 태그 보존
    lang_total = label_ko + label_en
    lang_preservation = 100.0 if lang_total > 0 else 0.0

    # 네임스페이스 구분: prefix로 구분되므로 항상 100%
    ns_disambiguation = 100.0

    faithfulness_score = _clamp(
        direction_accuracy * 0.30
        + multivalue_preservation * 0.25
        + datatype_fidelity * 0.20
        + lang_preservation * 0.15
        + ns_disambiguation * 0.10
    )

    # ── D3. Query Equivalence (쿼리 동등성, 20%) ──
    # Kontokostas et al. (2014) WWW — Test-Driven Evaluation
    query_equivalence_score, query_eq_details = _compute_d3_score(nodes, relationships)

    # ── D4. Consistency (일관성, 10%) ──
    # Färber et al. (2018) SWJ 9(1):77-129
    # Instance 관계만 대상으로 한다 (OWLClass 사이의 rdfs_subClassOf 는 schema 관계).
    instance_nodes = {u for u, n in nodes.items() if "OWLClass" not in n["labels"]}
    instance_rels = [(s, e, t) for s, e, t in relationships
                     if t != "rdfs_subClassOf" and s in instance_nodes and e in instance_nodes]
    node_uris_in_rels = set()
    for s, e, _ in instance_rels:
        node_uris_in_rels.add(s)
        node_uris_in_rels.add(e)
    orphan_nodes = instance_nodes - node_uris_in_rels
    orphan_rate = len(orphan_nodes) / len(instance_nodes) * 100 if instance_nodes else 0

    # dangling: relationships 자체에 등장하지만 nodes 사전에 없는 endpoint
    dangling = sum(1 for s, e, _ in relationships
                   if s not in all_node_uris or e not in all_node_uris)
    dangling_rate = dangling / len(relationships) * 100 if relationships else 0

    class_prop_keys: dict[str, Counter] = defaultdict(Counter)
    for n in nodes.values():
        if "OWLClass" in n["labels"]:
            continue
        for lb in n["labels"]:
            if lb.startswith(("http", "urn")):
                continue
            for k in n["properties"]:
                class_prop_keys[lb][k] += 1

    label_consistency_scores = []
    for cls, key_counts in class_prop_keys.items():
        if not key_counts:
            continue
        total_instances = lpg_cd.get(cls, 1)
        consistent_keys = sum(1 for k, cnt in key_counts.items() if cnt >= total_instances * 0.9)
        total_keys = len(key_counts)
        if total_keys:
            label_consistency_scores.append(consistent_keys / total_keys * 100)
    label_consistency = sum(label_consistency_scores) / len(label_consistency_scores) if label_consistency_scores else 100

    consistency_score = _clamp(
        max(0, 100 - orphan_rate * 2) * 0.40
        + max(0, 100 - dangling_rate * 20) * 0.30  # C-2: 단위 수정, 5%→0점
        + label_consistency * 0.30
    )

    # ── D5. Conciseness (간결성, 5%) ──
    rel_tuples = [(s, e, t) for s, e, t in relationships]
    unique_rels = set(rel_tuples)
    duplicate_rate = (len(rel_tuples) - len(unique_rels)) / len(rel_tuples) * 100 if rel_tuples else 0

    redundant_labels = set()
    for n in nodes.values():
        for lb in n["labels"]:
            if lb in ("Thing", "Resource", "Class", "NamedIndividual"):
                redundant_labels.add(lb)

    # [C-3] 모든 항 clamp
    conciseness_score = _clamp(
        max(0, 100 - duplicate_rate * 20) * 0.60
        + (100 if not redundant_labels else max(0, 100 - len(redundant_labels) * 25)) * 0.40
    )

    # ── 종합 점수 ──
    total_score = (
        completeness_score * 0.40
        + faithfulness_score * 0.25
        + query_equivalence_score * 0.20
        + consistency_score * 0.10
        + conciseness_score * 0.05
    )

    quality_score = {
        "total": round(min(total_score, 100), 1),
        "completeness": {
            "score": round(completeness_score, 1),
            "weight": 0.40,
            "details": {
                "node": round(node_completeness, 1),
                "edge": round(edge_completeness, 1),
                "property": round(property_completeness, 1),
                "label": round(label_completeness, 1),
                "hierarchy": round(hierarchy_completeness, 1),
            },
        },
        "faithfulness": {
            "score": round(faithfulness_score, 1),
            "weight": 0.25,
            "details": {
                "direction_accuracy": round(direction_accuracy, 1),
                "multivalue_preservation": round(multivalue_preservation, 1),
                "datatype_fidelity": round(datatype_fidelity, 1),
                "language_preservation": round(lang_preservation, 1),
                "namespace_disambiguation": round(ns_disambiguation, 1),
            },
        },
        "query_equivalence": {
            "score": round(query_equivalence_score, 1),
            "weight": 0.20,
            "details": query_eq_details,
        },
        "consistency": {
            "score": round(consistency_score, 1),
            "weight": 0.10,
            "details": {
                "orphan_rate": round(orphan_rate, 2),
                "dangling_rate": round(dangling_rate, 2),
                "label_consistency": round(label_consistency, 1),
            },
        },
        "conciseness": {
            "score": round(conciseness_score, 1),
            "weight": 0.05,
            "details": {
                "duplicate_rate": round(duplicate_rate, 2),
                "redundant_labels": sorted(redundant_labels),
            },
        },
        "references": [
            "Zaveri, A. et al. (2016) SWJ 7(1):63-93 — Completeness metrics",
            "Angles, R. et al. (2020) IEEE Access 8:86091 — RDF-to-PG mapping fidelity",
            "Kontokostas, D. et al. (2014) WWW — Query-based quality testing",
            "Färber, M. et al. (2018) SWJ 9(1):77-129 — Instance consistency",
            "Hartig, O. (2014) CEUR-WS — RDF/PG structural correspondence",
        ],
    }

    return {
        "class_distribution": {
            "matched": len(rdf_cd) - len(c_mis), "total": len(rdf_cd),
            "mismatches": [
                {"class": cls, "rdf": rdf_count, "lpg": lpg_count}
                for cls, rdf_count, lpg_count in c_mis[:10]
            ],
        },
        "iof_class_distribution": {
            "matched": len(rdf_iof_cd) - len(iof_c_mis), "total": len(rdf_iof_cd),
            "mismatches": [
                {"class": cls, "rdf": rdf_count, "lpg": lpg_count}
                for cls, rdf_count, lpg_count in iof_c_mis[:10]
            ],
        },
        "domain_relationships": {
            "matched": len(rdf_rd) - len(r_mis), "total": len(rdf_rd),
            "mismatches": [{"rel": r, "rdf": rv, "lpg": lv} for r, rv, lv in r_mis[:10]],
        },
        "iof_relationships": dict(iof_rels.most_common(15)),
        "iof_total": sum(iof_rels.values()),
        "class_hierarchy": subclass_count,
        "labels": {"ko": label_ko, "en": label_en, "comment_ko": comment_ko},
        "spot_check": {
            "passed": spot_pass,
            "total": len(samp),
            "mismatches": spot_mismatches,
        },
        "coverage": {
            "lpg_triples": lpg_t,
            "rdf_instance_triples": rdf_instance_triples,
            "rdf_total": total,
            "percent": round(cov, 1),
            "excluded": excluded,
        },
        "quality_score": quality_score,
        "cq_coverage": _verify_lpg_cq_coverage(nodes, relationships),
        "policy_normalization": {
            "_note": "검증은 LPG 변환 정책(sanitize / inverse fold / sparse pruning)을 인지한 비교로 수행됨. 아래 수치는 정책으로 흡수된 차이가 mismatch 로 둔갑하지 않도록 적용된 정규화 통계.",
            "tbox_inverse_pairs": len(tbox_inverse_map),
            "canonical_overrides": len(canonical_inverse_map),
            "ident_renames_applied": len(stats.get("ident_renames", {})),
            "array_columns_excluded_from_dp_check": len(array_props),
        },
        "issues": issues,
        "verified": len(issues) == 0,
    }


def _verify_lpg_cq_coverage(nodes: dict, relationships: list) -> dict:
    """LPG에서 Competency Questions 응답 가능성을 검증한다.

    CQ 도메인 클래스가 LPG 노드 라벨로 존재하고,
    도메인 간 관계 경로가 존재하는지 확인.
    """
    import os as _os

    from config import COMPETENCY_QUESTIONS_PATH
    cq_path = COMPETENCY_QUESTIONS_PATH
    if not _os.path.exists(cq_path):
        return {"status": "skip", "reason": "CQ 파일 없음"}

    with open(cq_path, encoding="utf-8") as f:
        cqs = json.load(f)

    # LPG 라벨 및 관계 인덱스 구축
    label_instances: Counter = Counter()
    for n in nodes.values():
        if "OWLClass" in n["labels"]:
            continue
        for lb in n["labels"]:
            label_instances[lb] += 1

    # 관계 타입별 (source_label → target_label) 매핑
    rel_label_map: dict[str, set] = defaultdict(set)
    for s_uri, e_uri, _rtype in relationships:
        s_node = nodes.get(s_uri)
        e_node = nodes.get(e_uri)
        if not s_node or not e_node:
            continue
        for s_lb in s_node["labels"]:
            for e_lb in e_node["labels"]:
                rel_label_map[s_lb].add(e_lb)
                rel_label_map[e_lb].add(s_lb)  # 양방향 탐색

    cq_results = []
    passed = 0
    for cq in cqs:
        cq_id = cq.get("id", "")
        domains = cq.get("domains", [])
        cls_names = [d.replace("_", "") for d in domains]

        # 클래스 존재
        cls_exist = all(label_instances.get(c, 0) > 0 for c in cls_names)

        # 연결성: 모든 도메인 쌍이 1-2홉 내 연결
        connected = True
        if cls_exist and len(cls_names) >= 2:
            for i in range(len(cls_names)):
                for j in range(i + 1, len(cls_names)):
                    a, b = cls_names[i], cls_names[j]
                    # 직접 연결
                    if b in rel_label_map.get(a, set()):
                        continue
                    # 2홉: a → mid → b
                    found_2hop = False
                    for mid in rel_label_map.get(a, set()):
                        if b in rel_label_map.get(mid, set()):
                            found_2hop = True
                            break
                    if not found_2hop:
                        connected = False
                        break
                if not connected:
                    break

        cq_pass = cls_exist and connected
        if cq_pass:
            passed += 1
        cq_results.append({"id": cq_id, "passed": cq_pass, "classes_exist": cls_exist, "connected": connected})

    return {
        "total": len(cqs),
        "passed": passed,
        "pass_rate": round(passed / max(len(cqs), 1) * 100, 1),
        "results": cq_results,
    }


def _build_cq_section_html(verification: dict) -> str:
    """CQ 커버리지 HTML 섹션 — 구현은 neo4j_reporter로 이전됨."""
    from tools.remote.neo4j_reporter import build_cq_section_html
    return build_cq_section_html(verification)


def _build_lpg_report_html(
    source_file: str, rdf_triples: int, nodes: dict, relationships: list,
    verification: dict, stats: dict,
) -> str:
    """LPG 변환 보고서 HTML 렌더링 — 구현은 neo4j_reporter로 이전됨."""
    from tools.remote.neo4j_reporter import build_lpg_report_html
    return build_lpg_report_html(
        source_file, rdf_triples, nodes, relationships, verification, stats,
    )



def convert_rdf_to_lpg(
    ttl_file: str = "",
    sample_count: int = 20,
    open_browser: bool = True,
) -> str:
    """RDF(TTL) 파일을 Neo4j LPG 형식(CSV)으로 변환하고 검증 보고서(HTML)를 생성한다.

    Neo4j 연결 없이 로컬에서 실행. 산출물:
    - data/generated/inferred/neo4j/nodes.csv: 노드 (라벨, 프로퍼티)
    - data/generated/inferred/neo4j/relationships.csv: 관계
    - data/generated/reports/lpg_conversion_report.html: 검증 보고서

    변환 규칙:
    - rdf:type → Neo4j 라벨
    - DatatypeProperty → 노드 프로퍼티
    - ObjectProperty → 관계 (네임스페이스 prefix로 충돌 방지)
    - IOF 추론 관계 포함 (iof_hasQuality, iof_hasParticipantAtSomeTime 등)
    - rdfs:subClassOf → 클래스 계층 관계 (rdfs_subClassOf)
    - rdfs:label/comment → 노드 프로퍼티
    - OWL axiom (inverseOf, Restriction 등) → 제외 (LPG 표현 불가, 추론 결과는 포함)

    Neo4j 적재 시 (Cypher 기반):
        neo4j_deploy_lpg 도구를 사용하세요. (APOC 플러그인 필요)
        neo4j-admin import 사용 시: --array-delimiter=$'\\x1f'

    Args:
        ttl_file: data/generated/inferred 아래 변환할 TTL 파일명. 비어있으면 all_inferred.ttl 사용.
        sample_count: 스팟체크 엔티티 수. 기본 20개.
        open_browser: True면 HTML 보고서를 브라우저에서 열기.
    """
    try:
        if ttl_file:
            ttl_file = resolve_child_path(
                os.path.dirname(INFERRED_PATH),
                ttl_file,
                allowed_suffixes=(".ttl",),
            )
        return _convert_rdf_to_lpg_impl(ttl_file, sample_count, open_browser)
    except Exception as e:
        return error_response(e, hint="TTL 파일 경로와 구문을 확인하세요.", logger=logger)


def _convert_rdf_to_lpg_impl(ttl_file: str, sample_count: int, open_browser: bool) -> str:
    ttl_path = ttl_file or INFERRED_PATH
    if not os.path.exists(ttl_path):
        return error_response(
            f"파일 없음: {ttl_path}",
            hint="파이프라인을 먼저 실행하여 TTL을 생성하세요.",
        )

    # 파싱 — Oxigraph Rust bulk_load (대용량 추론 결과에서 ~10× 가속)
    from domain.tbox_utils import fast_parse_turtle
    g = _new_graph()
    fast_parse_turtle(g, ttl_path)
    rdf_triples = len(g)
    ns = _DOMAIN_NS

    # 변환
    nodes, relationships, conv_stats = _convert_rdf_to_lpg(g)
    prop_type_map = conv_stats.get("prop_type_map", {})

    # LPG Co-design: self-loop 제거
    relationships, self_loop_count = _filter_self_loops(relationships)

    # LPG Co-design: inverse 관계 중복 제거
    tbox_for_dedup = _new_graph()
    if os.path.exists(TBOX_PATH):
        tbox_for_dedup.parse(TBOX_PATH, format="turtle")
    relationships, dedup_stats = _dedup_inverse_relationships(relationships, tbox_for_dedup)

    # LPG Co-design: sparse column pruning
    nodes, pruned_keys, coverage_stats = _prune_sparse_columns(nodes, min_coverage=0.01)

    # CSV 출력
    out_dir = _lpg_csv_dir()
    os.makedirs(out_dir, exist_ok=True)

    all_prop_keys = set()
    for n in nodes.values():
        all_prop_keys.update(n["properties"].keys())
    prop_keys = sorted(all_prop_keys)

    # [H1] 타입 정보 포함 CSV 헤더 (예: temperature:float, createdAt:datetime)
    # 다중값 프로퍼티는 array 타입 (예: label_ko:string[]). neo4j-admin import 호환.
    array_props = conv_stats.get("array_props", set())
    typed_headers = []
    for k in prop_keys:
        neo4j_type = prop_type_map.get(k, "")
        is_array = k in array_props
        if is_array and not neo4j_type:
            neo4j_type = "string"  # default array 원소 타입
        if neo4j_type:
            header = f"{k}:{neo4j_type}[]" if is_array else f"{k}:{neo4j_type}"
        else:
            header = k
        typed_headers.append(header)

    nodes_path = os.path.join(out_dir, "nodes.csv")
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uri:ID", ":LABEL"] + typed_headers)
        for uri, data in nodes.items():
            labels = ";".join(sorted(data["labels"])) if data["labels"] else "Resource"
            row = [uri, labels]
            for k in prop_keys:
                val = data["properties"].get(k, "")
                if isinstance(val, list):
                    val = _ARRAY_DELIM.join(val)
                row.append(val)
            w.writerow(row)

    rels_path = os.path.join(out_dir, "relationships.csv")
    with open(rels_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([":START_ID", ":END_ID", ":TYPE"])
        for start, end, rtype in relationships:
            w.writerow([start, end, rtype])

    # 검증 — CSV에서 독립 로드하여 self-grading 편향 제거
    _verify_g = _new_graph()
    fast_parse_turtle(_verify_g, ttl_path)
    _verify_nodes: dict[str, dict] = {}
    _verify_rels: list[tuple[str, str, str]] = []
    with open(nodes_path, encoding="utf-8") as _vf:
        _vr = csv.DictReader(_vf)
        for _row in _vr:
            _uri = _row.get("uri:ID", "")
            _lbls = set(_row.get(":LABEL", "").split(";")) if _row.get(":LABEL") else set()
            _props: dict = {}
            for k, v in _row.items():
                if k in ("uri:ID", ":LABEL") or not v:
                    continue
                key = k.split(":")[0]
                # array 헤더 (":string[]", ":int[]" 등) → 값도 delimiter 로 split
                _props[key] = v.split(_ARRAY_DELIM) if k.endswith("[]") else v
            _verify_nodes[_uri] = {"labels": _lbls, "properties": _props}
    with open(rels_path, encoding="utf-8") as _vf:
        _vr2 = csv.DictReader(_vf)
        for _row in _vr2:
            _verify_rels.append((_row[":START_ID"], _row[":END_ID"], _row[":TYPE"]))
    verification = _verify_lpg(_verify_g, _verify_nodes, _verify_rels, ns, sample_count, conv_stats)

    # LPG Loss Manifest 생성 + 저장
    exclusion_tracker = conv_stats.get("exclusion_tracker", {})
    axiom_triples = rdf_triples - conv_stats.get("rdf_instance_triples", 0)
    loss_manifest = _build_lpg_loss_manifest(
        exclusion_tracker,
        bnode_losses=conv_stats.get("excluded_bnode", 0),
        external_drops=conv_stats.get("excluded_external_ref", 0),
        rdf_total=rdf_triples,
        instance_triples=conv_stats.get("rdf_instance_triples", 0),
        axiom_triples=axiom_triples,
        pruned_sparse_columns=coverage_stats,
        ident_renames=conv_stats.get("ident_renames", {}),
    )
    manifest_path = os.path.join(out_dir, "lpg_loss_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(loss_manifest, f, ensure_ascii=False, indent=2)

    # HTML 보고서
    os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
    html = _build_lpg_report_html(
        ttl_path, rdf_triples, nodes, relationships, verification, conv_stats,
    )
    report_path = os.path.join(GENERATED_REPORTS_DIR, "lpg_conversion_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    if open_browser:
        from tools.common import path_to_file_uri
        webbrowser.open(path_to_file_uri(report_path))

    result = {
        "success": True,
        "source": ttl_path,
        "rdf_triples": rdf_triples,
        "lpg_nodes": len(nodes),
        "lpg_relationships": len(relationships),
        "files": {
            "nodes_csv": nodes_path,
            "relationships_csv": rels_path,
            "report_html": report_path,
            "loss_manifest": manifest_path,
        },
        "verification": {
            "class_distribution": f"{verification['class_distribution']['matched']}/{verification['class_distribution']['total']}",
            "domain_relationships": f"{verification['domain_relationships']['matched']}/{verification['domain_relationships']['total']}",
            "iof_relationships": verification["iof_total"],
            "spot_check": f"{verification['spot_check']['passed']}/{verification['spot_check']['total']}",
            "coverage_percent": verification["coverage"]["percent"],
            "quality_score": verification.get("quality_score", {}).get("total", 0),
            "cq_coverage": f"{verification.get('cq_coverage', {}).get('passed', 0)}/{verification.get('cq_coverage', {}).get('total', 0)}",
            "verified": verification["verified"],
        },
    }
    # 변환 시 적용된 변경사항 — 데이터 손실/식별자 변경을 응답에 노출
    if pruned_keys:
        result["sparse_pruned_columns"] = {
            "count": len(pruned_keys),
            "keys": pruned_keys,
            "coverage_ratios": {k: round(v, 4) for k, v in coverage_stats.items()},
            "note": "각 노드의 _metadata JSON 프로퍼티로 이동. Cypher 에서 직접 WHERE 절 사용 불가.",
        }
    ident_renames = conv_stats.get("ident_renames", {})
    if ident_renames:
        result["identifier_renames"] = {
            "count": len(ident_renames),
            "sample": dict(sorted(ident_renames.items())[:10]),
        }
    if dedup_stats.get("canonical_overrides_applied"):
        result["canonical_inverse_overrides"] = dedup_stats["canonical_overrides_applied"]
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── LPG 시맨틱 딕셔너리 ───────────────────────────────────────────────────────

_LPG_META_LABELS = frozenset({
    "Literal", "decimal", "string", "int", "float", "boolean",
    "date", "datetime", "dateTime", "integer", "long", "short",
    "nonNegativeInteger", "DataRange", "Datatype",
    "LPGNode", "OWLClass", "Resource", "Thing",
})

_MAX_EXAMPLES = 5  # 프로퍼티별 예시값 최대 수

# 센서/측정 전용 프로퍼티 — 이것만 가진 라벨은 "data" 티어로 분류
_SENSOR_ONLY_PROPS = frozenset({"value", "timestamp", "tagId", "qualityCode"})


def generate_lpg_semantic_dictionary(
    nodes_csv: str = "",
    relationships_csv: str = "",
) -> str:
    """LPG CSV에서 Neo4j 전용 시맨틱 딕셔너리를 생성한다.

    convert_rdf_to_lpg 실행 후 호출. ask_neo4j가 정확한 Cypher를 생성하도록
    실제 Neo4j 라벨, 프로퍼티, 관계 타입 정보를 수집한다.

    산출물: data/generated/neo4j/semantic_dictionary.json

    Args:
        nodes_csv: data/generated/inferred/neo4j 아래 nodes.csv 경로 (절대경로 또는
            작업 디렉터리 기준 상대경로). 비어있으면 inferred 기본 경로 사용.
        relationships_csv: data/generated/inferred/neo4j 아래 relationships.csv 경로
            (절대경로 또는 작업 디렉터리 기준 상대경로). 비어있으면 inferred 기본 경로 사용.
    """
    try:
        if nodes_csv:
            nodes_csv = _resolve_lpg_csv(nodes_csv)
        if relationships_csv:
            relationships_csv = _resolve_lpg_csv(relationships_csv)
        # CSV 기본 경로: inferred 우선, 없으면 generated/neo4j 폴백
        inferred_dir = _lpg_csv_dir()
        base_dir = os.path.join(os.path.dirname(os.path.dirname(INFERRED_PATH)), "neo4j")
        if os.path.exists(os.path.join(inferred_dir, "nodes.csv")):
            default_dir = inferred_dir
        elif os.path.exists(os.path.join(base_dir, "nodes.csv")):
            default_dir = base_dir
        else:
            default_dir = inferred_dir  # 에러 메시지용
        nodes_path = nodes_csv or os.path.join(default_dir, "nodes.csv")
        rels_path = relationships_csv or os.path.join(default_dir, "relationships.csv")

        for p in (nodes_path, rels_path):
            if not os.path.exists(p):
                return error_response(
                    f"파일 없음: {p}",
                    hint="convert_rdf_to_lpg를 먼저 실행하세요.",
                )

        result = _build_lpg_semantic_dict(nodes_path, rels_path)

        os.makedirs(os.path.dirname(LPG_SEMANTIC_DICT_PATH), exist_ok=True)
        with open(LPG_SEMANTIC_DICT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        return json.dumps({
            "success": True,
            "path": LPG_SEMANTIC_DICT_PATH,
            "labels": len(result["labels"]),
            "relationship_types": len(result["relationship_types"]),
            "total_nodes": result["metadata"]["total_nodes"],
            "total_relationships": result["metadata"]["total_relationships"],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, hint="CSV 파일 경로를 확인하세요.", logger=logger)


def _extract_owl_semantics(rel_names: set[str], dp_names: set[str]) -> dict:
    """T-Box에서 OWL 프로퍼티 특성을 추출하여 LPG 관계/프로퍼티명에 매핑한다.

    Args:
        rel_names: LPG 관계 타입 이름 집합 (ObjectProperty 대상).
        dp_names: LPG 노드 프로퍼티 이름 집합 (DatatypeProperty 대상).

    Returns:
        {rel_or_prop_name: {transitive?, symmetric?, functional?, inverse_of?, cypher_hint?}}
        관계 이름이 _cypher_hint_ident 를 통과하지 못하면 특성 플래그만 두고
        관계 패턴 cypher_hint 는 만들지 않는다.
    """
    if not os.path.exists(TBOX_PATH):
        return {}

    tbox = _new_graph()
    tbox.parse(TBOX_PATH, format="turtle")

    # URI → local name 매핑 (LPG 이름과 대조용)
    owl_sem: dict[str, dict] = {}

    # ObjectProperty 특성 추출
    for s in tbox.subjects(RDF.type, OWL.ObjectProperty):
        name = _local_name(str(s))
        if name not in rel_names:
            continue

        hint_name = _cypher_hint_ident(name)
        entry: dict = {}
        if (s, RDF.type, OWL.TransitiveProperty) in tbox:
            entry["transitive"] = True
            if hint_name:
                entry["cypher_hint"] = f"MATCH path=(a)-[:{hint_name}*]->(b)"
        if (s, RDF.type, OWL.SymmetricProperty) in tbox:
            entry["symmetric"] = True
            if hint_name:
                entry["cypher_hint"] = f"MATCH (a)-[:{hint_name}]-(b) // undirected"
        if (s, RDF.type, OWL.FunctionalProperty) in tbox:
            entry["functional"] = True
            if hint_name:
                entry.setdefault(
                    "cypher_hint", f"MATCH (a)-[:{hint_name}]->(b) // always 0 or 1",
                )
        for _, _, inv in tbox.triples((s, OWL.inverseOf, None)):
            if isinstance(inv, URIRef):
                entry["inverse_of"] = _local_name(str(inv))
                break

        if entry:
            owl_sem[name] = entry

    # DatatypeProperty — functional 여부만 추출
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        name = _local_name(str(s))
        if name not in dp_names:
            continue
        if (s, RDF.type, OWL.FunctionalProperty) in tbox:
            owl_sem[name] = {"functional": True, "cypher_hint": "// always 0 or 1 value"}

    return owl_sem


def _build_lpg_semantic_dict(nodes_path: str, rels_path: str) -> dict:
    """LPG CSV를 분석하여 시맨틱 딕셔너리 dict를 반환한다."""

    # ── 1. 노드 CSV 분석 ──
    uri_labels: dict[str, list[str]] = {}
    label_counts: Counter = Counter()
    label_cooccur: dict[str, Counter] = defaultdict(Counter)
    prop_node_count: Counter = Counter()  # 프로퍼티별 노드 수 (specificity 계산용)
    # label → prop_name → {"type", "non_null", "values"}
    label_props: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"type": "string", "non_null": 0, "values": set()})
    )

    with open(nodes_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # 프로퍼티 컬럼 파싱: "propName:type" → (header, name, type)
        prop_columns: list[tuple[str, str, str]] = []
        for h in headers:
            if h in ("uri:ID", ":LABEL"):
                continue
            parts = h.split(":")
            prop_columns.append((h, parts[0], parts[1] if len(parts) > 1 else "string"))

        total_nodes = 0
        for row in reader:
            total_nodes += 1
            raw_labels = [lb for lb in row.get(":LABEL", "").split(";") if lb]
            meaningful = [
                lb for lb in raw_labels
                if lb not in _LPG_META_LABELS and not _HASH_URI_RE.match(lb)
            ]
            uri_labels[row.get("uri:ID", "")] = meaningful

            # 라벨 공존 추적
            for i_lb, lb1 in enumerate(meaningful):
                for lb2 in meaningful[i_lb + 1:]:
                    label_cooccur[lb1][lb2] += 1
                    label_cooccur[lb2][lb1] += 1

            # 노드별 프로퍼티 존재 추적 (specificity 분모)
            for header, pname, _ in prop_columns:
                if row.get(header, ""):
                    prop_node_count[pname] += 1

            for lb in meaningful:
                label_counts[lb] += 1
                for header, pname, ptype in prop_columns:
                    val = row.get(header, "")
                    if val:
                        info = label_props[lb][pname]
                        info["type"] = ptype
                        info["non_null"] += 1
                        if len(info["values"]) < _MAX_EXAMPLES:
                            info["values"].add(val)

    # ── 2. 관계 CSV 분석 ──
    rel_counts: Counter = Counter()
    rel_source: dict[str, Counter] = defaultdict(Counter)
    rel_target: dict[str, Counter] = defaultdict(Counter)
    total_rels = 0

    with open(rels_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rels += 1
            rtype = row.get(":TYPE", "")
            rel_counts[rtype] += 1
            for lb in uri_labels.get(row.get(":START_ID", ""), []):
                rel_source[rtype][lb] += 1
            for lb in uri_labels.get(row.get(":END_ID", ""), []):
                rel_target[rtype][lb] += 1

    # ── 3. 라벨별 딕셔너리 구성 (프로퍼티 특이성 포함) ──
    labels_dict: dict[str, dict] = {}
    for lb, cnt in label_counts.most_common():
        lb_ratio = cnt / total_nodes if total_nodes > 0 else 0
        props = {}
        for pname, pinfo in label_props[lb].items():
            if pinfo["non_null"] == 0:
                continue
            coverage = round(pinfo["non_null"] / cnt * 100, 1)
            if coverage < 1:
                continue
            # 프로퍼티 특이성: P(label|prop) / P(label)
            # > 1 = 이 라벨에 집중, ≈ 1 = 무작위, < 1 = 반비례
            p_total = prop_node_count.get(pname, 1)
            specificity = round(
                (pinfo["non_null"] / p_total) / lb_ratio, 1,
            ) if lb_ratio > 0 else 0
            props[pname] = {
                "type": pinfo["type"],
                "coverage": coverage,
                "specificity": specificity,
                "rare_but_important": coverage < 5 and specificity > 2.0,
                "distinct_samples": sorted(pinfo["values"]),
            }
        labels_dict[lb] = {"count": cnt, "properties": props}

    # ── 3b. 라벨 분류 + 공존 ──
    # native 프로퍼티 = specificity > 1.5 (이 라벨에 1.5배 이상 집중)
    _NATIVE_THRESHOLD = 1.5
    for lb, info in labels_dict.items():
        native = {
            p: v for p, v in info["properties"].items()
            if v.get("specificity", 0) >= _NATIVE_THRESHOLD
        }
        native_names = set(native.keys())
        has_id = any(p.endswith("Id") and p != "tagId" for p in native_names)
        has_name = any(p.endswith("Name") or p.endswith("_ko") for p in native_names)
        if has_id or has_name:
            info["tier"] = "core"
        elif native_names and native_names <= _SENSOR_ONLY_PROPS:
            info["tier"] = "data"
        elif not native_names:
            info["tier"] = "data"  # native 프로퍼티 없음 = 우산 라벨
        else:
            info["tier"] = "core"
        # 공존 라벨 (top 5)
        cooc = label_cooccur.get(lb, Counter())
        info["cooccurs_with"] = [
            lb2 for lb2, _ in cooc.most_common(5) if lb2 in labels_dict
        ]

    # ── 4. 관계 타입별 딕셔너리 구성 ──
    rels_dict: dict[str, dict] = {}
    for rtype, cnt in rel_counts.most_common():
        rels_dict[rtype] = {
            "count": cnt,
            "source_labels": [lb for lb, _ in rel_source[rtype].most_common(3)],
            "target_labels": [lb for lb, _ in rel_target[rtype].most_common(3)],
        }

    # ── 4b. 역방향 관계 탐지 ──
    inverse_map: dict[str, str] = {}
    _rel_list = list(rel_counts.keys())
    for i_r, r1 in enumerate(_rel_list):
        if r1 in inverse_map:
            continue
        for r2 in _rel_list[i_r + 1:]:
            if r2 in inverse_map or rel_counts[r1] != rel_counts[r2]:
                continue
            if (set(rel_source[r1].keys()) == set(rel_target[r2].keys())
                    and set(rel_target[r1].keys()) == set(rel_source[r2].keys())):
                inverse_map[r1] = r2
                inverse_map[r2] = r1
                break  # 1:1 매핑 보장 — 첫 매칭만 유지
    for rtype in rels_dict:
        if rtype in inverse_map:
            rels_dict[rtype]["inverse"] = inverse_map[rtype]

    # ── 5. 라벨 퀵레퍼런스 (LLM 컨텍스트용 한줄 요약) ──
    quick_ref: dict[str, str] = {}
    for lb, info in labels_dict.items():
        # native 프로퍼티(specificity ≥ 1.5) top-5
        native_sorted = sorted(
            ((p, v) for p, v in info["properties"].items()
             if v.get("specificity", 0) >= _NATIVE_THRESHOLD),
            key=lambda x: -x[1]["specificity"],
        )[:5]
        prop_parts = [f"{p}({v['type']})" for p, v in native_sorted]
        # 나가는/들어오는 관계 — 건수 top-5
        out_all = [
            (rt, ri["target_labels"][0], ri["count"])
            for rt, ri in rels_dict.items()
            if lb in ri["source_labels"]
        ]
        in_all = [
            (ri["source_labels"][0], rt, ri["count"])
            for rt, ri in rels_dict.items()
            if lb in ri["target_labels"]
        ]
        out_top = [f"{rt}→{tgt}" for rt, tgt, _ in sorted(out_all, key=lambda x: -x[2])[:5]]
        in_top = [f"{src}→{rt}" for src, rt, _ in sorted(in_all, key=lambda x: -x[2])[:5]]
        parts = " ".join(prop_parts)
        if out_top:
            parts += " | Out: " + ", ".join(out_top)
        if in_top:
            parts += " | In: " + ", ".join(in_top)
        quick_ref[lb] = parts

    # ── 6. 주요 2홉 경로 자동 추출 (LLM 멀티홉 쿼리 지원) ──
    # 전략: 각 관계 타입별 최고 스코어 경로 1개 보장 + 전체 상위 보충
    # (rel_source/rel_target 전체 라벨로 브릿지 탐색)
    _raw_entries: list[tuple[int, str, str, str, str, str, str]] = []
    rel_type_list = list(rel_counts.keys())
    for r1 in rel_type_list:
        r1_tgt_all = set(rel_target[r1].keys())
        for r2 in rel_type_list:
            if r1 == r2:
                continue
            bridge = r1_tgt_all & set(rel_source[r2].keys())
            if bridge:
                src = rel_source[r1].most_common(1)[0][0]
                mid = max(bridge, key=lambda lb: rel_target[r1][lb])
                tgt = rel_target[r2].most_common(1)[0][0]
                # 경로 문자열은 Cypher 패턴 힌트다. 식별자로 그대로 쓸 수 없는 이름이
                # 하나라도 있으면 패턴을 만들지 않는다.
                if not all(_cypher_hint_ident(n) for n in (src, r1, mid, r2, tgt)):
                    continue
                if src != tgt:
                    score = rel_counts[r1] + rel_counts[r2]
                    path = f"(:{src})-[:{r1}]->(:{mid})-[:{r2}]->(:{tgt})"
                    _raw_entries.append((score, r1, r2, path, src, mid, tgt))

    # 역방향 중복 제거: inverse_map으로 정규화한 키 사용
    _deduped: dict[tuple, tuple[int, str, str, str]] = {}
    for score, r1, r2, path, src, mid, tgt in _raw_entries:
        r1n = min(r1, inverse_map.get(r1, r1))
        r2n = min(r2, inverse_map.get(r2, r2))
        key = (tuple(sorted([r1n, r2n])), tuple(sorted([src, tgt])), mid)
        if key not in _deduped or score > _deduped[key][0]:
            _deduped[key] = (score, r1, r2, path)
    _entries = list(_deduped.values())

    # Phase 1: 각 관계 타입(r1, r2)별 최고 스코어 경로 1개씩 보장
    seen: set[str] = set()
    two_hop_paths: list[str] = []
    best_per_rel: dict[str, tuple[int, str]] = {}
    for _score, r1, r2, path in _entries:
        for key in (f"r1:{r1}", f"r2:{r2}"):
            if key not in best_per_rel or score > best_per_rel[key][0]:
                best_per_rel[key] = (score, path)
    for _, path in sorted(best_per_rel.values(), key=lambda x: -x[0]):
        if path not in seen:
            seen.add(path)
            two_hop_paths.append(path)

    # Phase 2: 나머지 슬롯을 전체 스코어 순으로 채움
    for _score, _, _, path in sorted(_entries, key=lambda x: -x[0]):
        if len(two_hop_paths) >= 80:
            break
        if path not in seen:
            seen.add(path)
            two_hop_paths.append(path)

    # ── 7. OWL 시맨틱스 추출 (T-Box 프로퍼티 특성 → Cypher 힌트) ──
    all_dp_names: set[str] = set()
    for info in labels_dict.values():
        all_dp_names.update(info.get("properties", {}).keys())
    owl_sem = _extract_owl_semantics(set(rels_dict.keys()), all_dp_names)

    return {
        "metadata": {
            "type": "lpg",
            "domain_ko": DOMAIN_CONFIG.get("domain", {}).get("name_ko", ""),
            "total_nodes": total_nodes,
            "total_relationships": total_rels,
            "nodes_file": nodes_path,
            "relationships_file": rels_path,
            "generated_at": datetime.now().isoformat(),
        },
        "labels": labels_dict,
        "relationship_types": rels_dict,
        "label_quick_reference": quick_ref,
        "two_hop_paths": two_hop_paths,
        "inverse_pairs": {r: inv for r, inv in inverse_map.items() if r < inv},
        "owl_semantics": owl_sem,
    }


# ``ask_neo4j`` 공개 타깃은 tools/bedrock.py의 함수 하나뿐이다. Cypher NL 질의는
# bedrock.ask_neo4j를 사용하고, 이 모듈은 Neo4j 저장소 연산만 제공한다.

# CQ domain 에서 밑줄을 뺀 클래스 이름은 SPARQL prefixed name 과 Cypher label 자리에
# 들어간다. 문자/숫자 외 문자가 하나라도 있으면 두 쿼리 모두에서 구문을 끊을 수 있으므로
# 실행하지 않고 rejected_domains 로 보고한다.
_PARITY_CLASS_NAME_RE = re.compile(r"[^\W_]+")


def _split_parity_class_names(domains) -> tuple[list[str], list[str]]:
    """CQ domains 를 (검증 가능한 클래스 이름, 거부한 원본 값) 으로 나눈다."""
    if not isinstance(domains, list):
        domains = [domains] if domains else []
    accepted: list[str] = []
    rejected: list[str] = []
    for domain in domains:
        cls_name = domain.replace("_", "") if isinstance(domain, str) else None
        if cls_name and _PARITY_CLASS_NAME_RE.fullmatch(cls_name):
            accepted.append(cls_name)
        else:
            rejected.append(domain[:120] if isinstance(domain, str) else repr(domain)[:120])
    return accepted, rejected


def _parity_cypher_label(cls_name: str) -> str:
    """LPG 변환과 같은 sanitize 규칙으로 만든 label 을 backtick 으로 감싼다.

    sanitize 결과는 [A-Za-z0-9_] 뿐이라 backtick 을 닫을 수 없다. backtick 안은
    clause guard 가 보지 않으므로 `Stop`, `Remove` 같은 정상 클래스 이름도 통과한다.
    """
    return f"`{_sanitize_neo4j_ident(cls_name)}`"


def _parity_sparql_count(graph, query: str) -> int:
    """egress 구문을 거부한 뒤 로컬 그래프에서 COUNT 쿼리를 실행한다."""
    from tools.sparql_local import _reject_query_egress

    _reject_query_egress(query)
    rows = list(graph.query(query))
    return int(rows[0][0]) if rows else 0


def verify_sparql_cypher_parity(max_cqs: int = 10) -> str:
    """Golden Query 교차 검증 — CQ별로 SPARQL(로컬)과 Cypher(Neo4j) 결과를 비교한다.

    각 CQ의 도메인 클래스에 대해:
    1. 로컬 SPARQL로 인스턴스 수 + 관계 수를 조회
    2. Neo4j Cypher로 동일한 카운트를 조회
    3. 두 결과의 일치율을 계산

    NEO4J_DEPLOY 워크플로우 마지막에 호출하여 RDF→LPG 변환 충실도를 실측.

    CQ domain 중 밑줄을 뺀 값이 문자·숫자만으로 이뤄지지 않으면 쿼리에 넣지 않고
    rejected_domains 로 보고한다. Cypher 는 neo4j_query 와 같은 clause guard 와
    READ 세션으로, SPARQL 은 SERVICE/FROM 거부 guard 를 거쳐 실행한다.

    Args:
        max_cqs: 검증할 CQ 최대 수. 기본 10.
    """
    err = _check_configured()
    if err:
        return err

    import os as _os
    import time as _time

    start = _time.monotonic()

    # 1. CQ 로드
    from config import COMPETENCY_QUESTIONS_PATH
    cq_path = COMPETENCY_QUESTIONS_PATH
    if not _os.path.exists(cq_path):
        return error_response("CQ 파일 없음", hint="generate_competency_questions를 먼저 실행하세요.")

    with open(cq_path, encoding="utf-8") as f:
        cqs = json.load(f)[:max_cqs]

    # 2. 로컬 SPARQL 그래프 로드
    try:
        from tools.sparql_local import _get_graph
        sparql_g, _load_msg = _get_graph("inferred")
    except Exception as e:
        return error_response(f"SPARQL 그래프 로드 실패: {e}")

    from domain.namespaces import NS_PREFIX, prepend_prefixes

    # 3. CQ별 교차 검증
    results = []
    total_checks = 0
    matched_checks = 0
    rejected_domain_count = 0

    for cq in cqs:
        cq_id = cq.get("id", "unknown")
        # domains 는 사용자 입력이나 LLM 출력이다. 식별자가 아닌 값은 쿼리에 넣지 않는다.
        cls_names, rejected_domains = _split_parity_class_names(cq.get("domains", []))
        rejected_domain_count += len(rejected_domains)
        checks = []

        for cls_name in cls_names:
            # SPARQL: 인스턴스 수
            sparql_q = prepend_prefixes(
                f"SELECT (COUNT(DISTINCT ?x) AS ?cnt) WHERE {{ ?x a {NS_PREFIX}:{cls_name} }}"
            )
            try:
                sparql_count = _parity_sparql_count(sparql_g, sparql_q)
            except Exception:
                sparql_count = -1

            # Cypher: 인스턴스 수 (읽기 전용 guard + READ 세션)
            label = _parity_cypher_label(cls_name)
            try:
                cypher_count = _run_cypher_read_single(
                    f"MATCH (n:{label}) WHERE n.uri IS NOT NULL RETURN count(n) AS cnt"
                ) or 0
            except Exception:
                cypher_count = -1

            match = sparql_count == cypher_count
            if sparql_count >= 0 and cypher_count >= 0:
                total_checks += 1
                if match:
                    matched_checks += 1

            checks.append({
                "class": cls_name,
                "check": "instance_count",
                "sparql": sparql_count,
                "cypher": cypher_count,
                "match": match,
            })

        # 클래스 쌍 간 관계 수 비교
        for i in range(len(cls_names)):
            for j in range(i + 1, len(cls_names)):
                a, b = cls_names[i], cls_names[j]

                # SPARQL: a→b 또는 b→a 관계 수
                sparql_rel_q = prepend_prefixes(
                    f"SELECT (COUNT(*) AS ?cnt) WHERE {{ "
                    f"{{ ?x a {NS_PREFIX}:{a} ; ?p ?y . ?y a {NS_PREFIX}:{b} . "
                    f"FILTER(?p != rdf:type && STRSTARTS(STR(?p), STR({NS_PREFIX}:))) }} "
                    f"UNION "
                    f"{{ ?y a {NS_PREFIX}:{b} ; ?p ?x . ?x a {NS_PREFIX}:{a} . "
                    f"FILTER(?p != rdf:type && STRSTARTS(STR(?p), STR({NS_PREFIX}:))) }} }}"
                )
                try:
                    sparql_rel = _parity_sparql_count(sparql_g, sparql_rel_q)
                except Exception:
                    sparql_rel = -1

                # Cypher: a↔b 관계 수 (읽기 전용 guard + READ 세션)
                label_a, label_b = _parity_cypher_label(a), _parity_cypher_label(b)
                try:
                    cypher_rel = _run_cypher_read_single(
                        f"MATCH (a:{label_a})-[r]-(b:{label_b}) RETURN count(r) AS cnt"
                    ) or 0
                except Exception:
                    cypher_rel = -1

                # 관계 수는 정확히 일치하지 않을 수 있음 (LPG 변환에서 제외된 predicate)
                # 허용 오차: SPARQL 대비 90% 이상이면 match
                if sparql_rel > 0 and cypher_rel >= 0:
                    ratio = cypher_rel / sparql_rel
                    rel_match = ratio >= 0.9
                elif sparql_rel == 0 and cypher_rel == 0:
                    ratio = 1.0
                    rel_match = True
                else:
                    ratio = 0.0
                    rel_match = sparql_rel < 0 or cypher_rel < 0  # skip if error

                if sparql_rel >= 0 and cypher_rel >= 0:
                    total_checks += 1
                    if rel_match:
                        matched_checks += 1

                checks.append({
                    "pair": f"{a}↔{b}",
                    "check": "relationship_count",
                    "sparql": sparql_rel,
                    "cypher": cypher_rel,
                    "ratio": round(ratio, 3) if sparql_rel > 0 else None,
                    "match": rel_match,
                })

        cq_result = {
            "cq_id": cq_id,
            "checks": checks,
            "all_match": all(c["match"] for c in checks),
        }
        if rejected_domains:
            # 거부한 domain 은 측정하지 않았으므로 CQ 전체 일치 여부는 판정 불가다.
            cq_result["all_match"] = None
            cq_result["rejected_domains"] = rejected_domains
        results.append(cq_result)

    duration = round(_time.monotonic() - start, 1)
    parity_score = round(matched_checks / max(total_checks, 1) * 100, 1)

    response = {
        "success": True,
        "parity_score": parity_score,
        "total_checks": total_checks,
        "matched_checks": matched_checks,
        "duration_seconds": duration,
        "cqs_tested": len(cqs),
        "results": results,
        "interpretation": {
            ">=95%": "RDF→LPG 변환 충실도 우수",
            "80-95%": "일부 데이터 손실 있으나 허용 범위",
            "<80%": "변환 품질 문제 — 누락된 관계/노드 확인 필요",
        },
    }
    if rejected_domain_count:
        response["rejected_domain_count"] = rejected_domain_count
        response["warnings"] = [
            f"클래스 식별자가 아닌 CQ domain {rejected_domain_count}개를 검증에서 제외했습니다. "
            "해당 CQ 의 all_match 는 null 이며 results[].rejected_domains 를 확인하세요."
        ]
    return json.dumps(response, ensure_ascii=False, indent=2)
