"""GraphDB Free Edition MCP wrapper.

Why: reasonable.PyReasoner 의 Turtle 파서가 큰 입력 (>=100MB) 에서 비선형
비용을 보여 52M triples OWL 2 RL closure 를 1.5시간+ 에 완료하지 못함.
GraphDB Free 는 Lucene 인덱스 + RocksDB disk-backed forward-chaining 으로
같은 작업을 수십 분에 처리. macOS 32GB 환경에서 검증 가능한 대안.

How: GraphDB 11 REST API (RDF4J 호환) 를 thin requests 래퍼로 노출.
- repository init (owl2-rl-optimized ruleset)
- TTL bulk import via /statements
- SPARQL select / construct / update
- 추론 결과 export (NT/TTL)

Endpoint convention (GraphDB 11.3 default):
- Workbench UI : http://localhost:7200/
- REST root    : http://localhost:7200/rest/...
- Repository   : http://localhost:7200/repositories/<id>          (RDF4J)
- Statements   : http://localhost:7200/repositories/<id>/statements
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re

import requests
from rdflib import Literal

from config import (
    DATA_DIR,
    GRAPHDB_BASE_URL,
    GRAPHDB_REPOSITORY,
    GRAPHDB_RULESET,
    GRAPHDB_TIMEOUT,
)
from domain.namespaces import prepend_prefixes as _prepend_prefixes
from tools.common import JobRegistry, error_response, resolve_path_within

logger = logging.getLogger(__name__)


class _GraphDBInputError(ValueError):
    """GraphDB 요청을 보내기 전에 거부한 입력값."""


def _gdb_error(e: Exception, operation: str) -> str:
    if isinstance(e, _GraphDBInputError):
        return error_response(
            e, hint=f"GraphDB {operation} 요청을 보내지 않았습니다. 입력값을 확인하세요.",
        )
    if isinstance(e, requests.exceptions.ConnectionError):
        return error_response(
            e,
            hint=(
                f"GraphDB 연결 실패. {GRAPHDB_BASE_URL} Workbench 가 실행 중인지 "
                "확인 (open -a 'GraphDB Desktop')."
            ),
            logger=logger,
        )
    if isinstance(e, requests.exceptions.HTTPError):
        body = ""
        with contextlib.suppress(Exception):
            body = e.response.text[:400]  # type: ignore[union-attr]
        return error_response(
            e,
            hint=f"GraphDB HTTP 에러 ({operation}). 본문: {body}",
            logger=logger,
        )
    return error_response(
        e, hint=f"GraphDB {operation} 중 예기치 않은 오류.", logger=logger,
    )


# GraphDB repository ID 는 영문자·숫자·'-'·'_' 만 허용한다. ID 는 REST 경로와 저장소
# 설정 Turtle 에 들어가므로 이 규칙 밖의 값은 요청 전에 거부한다. 그래야 '/', '?', '#'
# 로 다른 엔드포인트를 가리키거나 질의 파라미터를 덧붙이지 못한다.
_REPO_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _repo_id(repo: str = "") -> str:
    """repository ID 를 확정한다. 비어 있으면 GRAPHDB_REPOSITORY 다.

    Raises:
        _GraphDBInputError: GraphDB repository ID 규칙에 맞지 않을 때.
    """
    rid = repo or GRAPHDB_REPOSITORY
    if not isinstance(rid, str) or not _REPO_ID_RE.fullmatch(rid):
        raise _GraphDBInputError(
            "GraphDB repository ID 는 영문자, 숫자, '-', '_' 만 쓸 수 있습니다."
        )
    return rid


def _repo_url(repo: str = "") -> str:
    return f"{GRAPHDB_BASE_URL}/repositories/{_repo_id(repo)}"


# ─────────────────────────────────────────────────────────
# Connectivity
# ─────────────────────────────────────────────────────────


def graphdb_health() -> str:
    """GraphDB Workbench 가 실행 중인지 확인하고 버전·repository 목록을 반환.

    macOS 에서 GraphDB Desktop.app 미실행 시 ConnectionError 와 함께 실행
    힌트를 돌려준다.
    """
    try:
        version_resp = requests.get(
            f"{GRAPHDB_BASE_URL}/rest/info/version", timeout=5,
        )
        version_resp.raise_for_status()
        version_info = version_resp.json()

        repos_resp = requests.get(
            f"{GRAPHDB_BASE_URL}/rest/repositories", timeout=5,
        )
        repos_resp.raise_for_status()
        repos = repos_resp.json()

        license_status = "unknown"
        try:
            lic_resp = requests.get(
                f"{GRAPHDB_BASE_URL}/rest/graphdb-settings/license", timeout=5,
            )
            if lic_resp.status_code == 200:
                lic_info = lic_resp.json()
                lic_valid = lic_info.get("valid")
                if lic_valid is True:
                    license_status = (
                        f"valid ({lic_info.get('product', 'free')} → "
                        f"expires {lic_info.get('expiryDate', '?')})"
                    )
                elif lic_valid is False:
                    license_status = "INVALID/EXPIRED"
                else:
                    license_status = "NOT SET — Workbench → Setup → License 에서 등록"
            else:
                license_status = f"check failed (HTTP {lic_resp.status_code})"
        except Exception:  # noqa: BLE001 — 선택적 라이선스 조회 실패는 health 본체를 막지 않는다
            pass

        return (
            f"GraphDB OK — version={version_info.get('productVersion', '?')} "
            f"license={license_status} "
            f"repositories={len(repos)} "
            f"({', '.join(r.get('id', '?') for r in repos) or 'none'})"
        )
    except Exception as e:
        return _gdb_error(e, "health")


# ─────────────────────────────────────────────────────────
# Repository management
# ─────────────────────────────────────────────────────────


# 자리표시자에는 Literal(...).n3() 로 만든 Turtle 리터럴을 넣는다. 값 안의 따옴표나
# 줄바꿈이 리터럴을 닫고 설정 트리플(graphdb:imports 등)을 덧붙이지 못한다.
_DEFAULT_REPO_TTL = """\
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rep: <http://www.openrdf.org/config/repository#> .
@prefix sr: <http://www.openrdf.org/config/repository/sail#> .
@prefix sail: <http://www.openrdf.org/config/sail#> .
@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .

[] a rep:Repository ;
    rep:repositoryID {repo_id} ;
    rdfs:label {label} ;
    rep:repositoryImpl [
        rep:repositoryType "graphdb:SailRepository" ;
        sr:sailImpl [
            sail:sailType "graphdb:Sail" ;
            graphdb:base-URL "http://example.org/graphdb#" ;
            graphdb:defaultNS "" ;
            graphdb:entity-index-size "10000000" ;
            graphdb:entity-id-size "32" ;
            graphdb:imports "" ;
            graphdb:repository-type "file-repository" ;
            graphdb:ruleset {ruleset} ;
            graphdb:storage-folder "storage" ;
            graphdb:enable-context-index "false" ;
            graphdb:enablePredicateList "true" ;
            graphdb:in-memory-literal-properties "true" ;
            graphdb:enable-literal-index "true" ;
            graphdb:check-for-inconsistencies "false" ;
            graphdb:disable-sameAs "true" ;
            graphdb:query-timeout "0" ;
            graphdb:query-limit-results "0" ;
            graphdb:throw-QueryEvaluationException-on-timeout "false" ;
            graphdb:read-only "false" ;
        ]
    ] .
"""


def graphdb_create_repository(
    repo_id: str = "",
    ruleset: str = "",
    label: str = "",
    overwrite: bool = False,
) -> str:
    """GraphDB repository 를 생성한다 (없으면).

    Args:
        repo_id: repository ID (영문자·숫자·'-'·'_'). 기본 GRAPHDB_REPOSITORY env,
                 미지정 시 domain_config.json 의 namespace.prefix → '{prefix}-kg' 로 유도.
        ruleset: 추론 룰셋. owl2-rl-optimized (기본) / owl-horst-optimized
                 / rdfs-optimized / empty 등.
        label: rdfs:label 표시명. 기본 repo_id 와 동일.
        overwrite: True 면 같은 ID 의 기존 repository 와 그 데이터를 삭제한 뒤 재생성한다.
                   기본 False 는 기존 repository 를 바꾸지 않고 "이미 존재" 를 반환한다.
    """
    rs = ruleset or GRAPHDB_RULESET
    try:
        rid = _repo_id(repo_id)
        lbl = label or rid
        # Existing?
        existing = requests.get(
            f"{GRAPHDB_BASE_URL}/rest/repositories", timeout=10,
        )
        existing.raise_for_status()
        ids = {r.get("id") for r in existing.json()}
        if rid in ids:
            if not overwrite:
                return f"이미 존재: repository={rid} (overwrite=False)"
            del_resp = requests.delete(
                f"{GRAPHDB_BASE_URL}/rest/repositories/{rid}", timeout=30,
            )
            del_resp.raise_for_status()
            logger.info("기존 repository 삭제됨: %s", rid)

        # Create via multipart upload (config.ttl)
        ttl_body = _DEFAULT_REPO_TTL.format(
            repo_id=Literal(rid).n3(),
            label=Literal(lbl).n3(),
            ruleset=Literal(rs).n3(),
        )
        files = {
            "config": (
                "config.ttl",
                ttl_body.encode("utf-8"),
                "application/x-turtle",
            ),
        }
        resp = requests.post(
            f"{GRAPHDB_BASE_URL}/rest/repositories",
            files=files,
            timeout=30,
        )
        resp.raise_for_status()
        return f"repository 생성됨: {rid} (ruleset={rs})"
    except Exception as e:
        return _gdb_error(e, "create_repository")


# ─────────────────────────────────────────────────────────
# Bulk import
# ─────────────────────────────────────────────────────────


_CONTENT_TYPES = {
    ".ttl": "text/turtle",
    ".nt": "application/n-triples",
    ".nq": "application/n-quads",
    ".rdf": "application/rdf+xml",
    ".jsonld": "application/ld+json",
}


def _content_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _CONTENT_TYPES[ext]


def graphdb_import_file(
    file_path: str,
    repo: str = "",
    graph_uri: str = "",
    replace: bool = False,
    timeout_override: int = 0,
) -> str:
    """로컬 TTL/NT 파일을 GraphDB repository 로 stream upload (server-side parse).

    Args:
        file_path: DATA_DIR 하위의 절대 경로. symlink 해석 후 DATA_DIR 안에
                   있어야 하며 .ttl / .nt / .nq / .rdf / .jsonld 만 허용.
        repo: repository ID. 기본 GRAPHDB_REPOSITORY.
        graph_uri: named graph URI. "" 이면 default graph.
        replace: True 면 기존 데이터 삭제 후 import (PUT). False 면 append (POST).
        timeout_override: 0 이면 GRAPHDB_TIMEOUT (default 600). 큰 파일에서
                          read timeout 발생 시 더 큰 값 (예: 7200) 지정.

    Note:
        파일이 클 경우 (수 GB) requests.post 가 streaming 으로 전송한다.
        타임아웃은 GRAPHDB_TIMEOUT (default 600s). 더 큰 파일은 timeout_override
        또는 환경변수로 늘려야 함. timeout 으로 client 가 끊겨도 서버는 transaction
        을 commit 하므로 데이터는 들어가나 client 입장에서 후속 청크는 skip 또는
        idempotent 처리 필요.
    """
    try:
        resolved_file_path = resolve_path_within(
            DATA_DIR,
            file_path,
            allowed_suffixes=tuple(_CONTENT_TYPES),
        )
    except ValueError as e:
        # containment 거부에는 입력·해석 경로를 되돌려 주지 않아 경로 노출을 막는다.
        return f"파일 import 거부: {e}"

    if not os.path.exists(resolved_file_path):
        return f"파일 없음: {file_path}"

    try:
        rid = _repo_id(repo)
    except _GraphDBInputError as e:
        return _gdb_error(e, "import_file")
    ct = _content_type_for(resolved_file_path)
    size_mb = os.path.getsize(resolved_file_path) / 1024 / 1024
    url = f"{_repo_url(rid)}/statements"
    params: dict[str, str] = {}
    if graph_uri:
        params["context"] = f"<{graph_uri}>"

    timeout = timeout_override if timeout_override > 0 else GRAPHDB_TIMEOUT
    logger.info(
        "GraphDB import 시작 — %s (%.0f MB) → %s%s timeout=%ds",
        os.path.basename(file_path), size_mb, rid,
        f" graph={graph_uri}" if graph_uri else "", timeout,
    )
    try:
        with open(resolved_file_path, "rb") as fh:
            method = "PUT" if replace else "POST"
            resp = requests.request(
                method,
                url,
                data=fh,
                params=params,
                headers={"Content-Type": ct},
                timeout=timeout,
            )
            resp.raise_for_status()
        return (
            f"import 완료 — {os.path.basename(file_path)} ({size_mb:.0f} MB) "
            f"→ {rid}{' [replaced]' if replace else ''}"
        )
    except Exception as e:
        return _gdb_error(e, "import_file")


# ─────────────────────────────────────────────────────────
# SPARQL (RDF4J endpoint)
# ─────────────────────────────────────────────────────────


def graphdb_sparql_select(
    query: str, repo: str = "", include_inferred: bool = True,
) -> str:
    """GraphDB 에서 SPARQL SELECT 실행. 결과 JSON.

    Args:
        query: SPARQL SELECT. PREFIX 생략 시 표준 PREFIX 자동 추가.
        repo: repository ID.
        include_inferred: True (기본) 면 추론 결과 포함, False 면 explicit only.
    """
    try:
        full_query = _prepend_prefixes(query)
        resp = requests.post(
            _repo_url(repo),
            data={"query": full_query, "infer": str(include_inferred).lower()},
            headers={"Accept": "application/sparql-results+json"},
            timeout=GRAPHDB_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        return _gdb_error(e, "sparql_select")


def graphdb_sparql_construct(
    query: str, repo: str = "", include_inferred: bool = True,
) -> str:
    """GraphDB 에서 SPARQL CONSTRUCT 실행. 결과 Turtle."""
    try:
        full_query = _prepend_prefixes(query)
        resp = requests.post(
            _repo_url(repo),
            data={"query": full_query, "infer": str(include_inferred).lower()},
            headers={"Accept": "text/turtle"},
            timeout=GRAPHDB_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        return _gdb_error(e, "sparql_construct")


def graphdb_sparql_update(query: str, repo: str = "") -> str:
    """GraphDB 에서 SPARQL UPDATE (INSERT/DELETE) 실행."""
    try:
        full_query = _prepend_prefixes(query)
        resp = requests.post(
            f"{_repo_url(repo)}/statements",
            data={"update": full_query},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=GRAPHDB_TIMEOUT,
        )
        resp.raise_for_status()
        return f"update 성공 (status {resp.status_code})"
    except Exception as e:
        return _gdb_error(e, "sparql_update")


def graphdb_count_triples(
    repo: str = "", include_inferred: bool = True,
) -> str:
    """repository 의 trip 수를 explicit / inferred 분리해서 보고."""
    try:
        rid = _repo_id(repo)
        out: dict[str, int] = {}
        for label, infer in (("explicit", "false"), ("with_inferred", "true")):
            resp = requests.post(
                _repo_url(rid),
                data={
                    "query": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
                    "infer": infer,
                },
                headers={"Accept": "application/sparql-results+json"},
                timeout=60,
            )
            resp.raise_for_status()
            j = resp.json()
            n = int(j["results"]["bindings"][0]["n"]["value"])
            out[label] = n
        delta = out["with_inferred"] - out["explicit"]
        return (
            f"repo={rid}  explicit={out['explicit']:,}  "
            f"with_inferred={out['with_inferred']:,}  Δ={delta:,}"
        )
    except Exception as e:
        return _gdb_error(e, "count_triples")


# ─────────────────────────────────────────────────────────
# Export inferred closure
# ─────────────────────────────────────────────────────────


_EXPORT_FORMATS = {
    "turtle": "text/turtle",
    "ttl": "text/turtle",
    "nt": "application/n-triples",
    "n-triples": "application/n-triples",
    "ntriples": "application/n-triples",
}
_EXPORT_SUFFIXES = (".ttl", ".nt", ".ntriples")


def _resolve_export_path(output_path: str) -> str:
    """export 대상 경로를 기본 INFERRED_PATH 와 같은 디렉터리 안의 RDF 파일로 제한한다.

    비어 있으면 INFERRED_PATH 다. 지정 경로는 symlink 해석 후에도
    data/generated/inferred 안이어야 하며 .ttl / .nt / .ntriples 만 허용한다.
    """
    from config import INFERRED_PATH

    if not output_path:
        return INFERRED_PATH
    return resolve_path_within(
        os.path.dirname(INFERRED_PATH),
        output_path,
        allowed_suffixes=_EXPORT_SUFFIXES,
    )


def graphdb_export_inferred(
    output_path: str = "",
    repo: str = "",
    only_inferred: bool = False,
    format: str = "turtle",
) -> str:
    """추론 후 전체 그래프 (또는 inferred-only) 를 파일로 export.

    Args:
        output_path: data/generated/inferred 아래 .ttl / .nt / .ntriples 경로 (절대경로
                     또는 작업 디렉터리 기준 상대경로). symlink 해석 후에도 그 디렉터리
                     안이어야 한다. 기본 INFERRED_PATH
                     (data/generated/inferred/all_inferred.ttl).
        repo: repository ID.
        only_inferred: True 면 (with_inferred − explicit) 차집합만 — SPARQL CONSTRUCT
                       두 번 (infer=true, infer=false) 후 set diff. 단순 비교 안전.
                       False (기본) 면 통합 그래프 전체 export.
        format: "turtle" (기본, INFERRED_PATH 와 일관) 또는 "nt". 파이프라인
                다운스트림 (sparql_local / validate_kg / semantic_dictionary) 은
                INFERRED_PATH 를 turtle 로 파싱하므로 turtle 권장.
    """
    try:
        rid = _repo_id(repo)
    except _GraphDBInputError as e:
        return _gdb_error(e, "export_inferred")
    accept = _EXPORT_FORMATS.get(format.lower())
    if accept is None:
        return f"지원하지 않는 format: {format} (turtle / nt)"

    try:
        out_path = _resolve_export_path(output_path)
    except ValueError as e:
        # containment 거부에는 입력·해석 경로를 되돌려 주지 않아 경로 노출을 막는다.
        return f"파일 export 거부: {e}"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    try:
        if only_inferred:
            # 차집합 export 는 set 비교 비용이 크므로 SPARQL 로 "infer=true 결과"
            # 만 받고 caller 가 explicit 와 비교하도록 안내.
            return (
                "only_inferred=True 는 미구현 — explicit/inferred 모두 포함된 전체 "
                "export 를 받은 뒤 diff 계산을 별도로 수행하세요. only_inferred=False 권장."
            )

        # Bandit B113은 산술식 timeout을 인식하지 못하지만 호출에는 명시돼 있다.
        resp = requests.get(  # nosec B113
            f"{_repo_url(rid)}/statements",
            params={"infer": "true"},
            headers={"Accept": accept},
            stream=True,
            timeout=GRAPHDB_TIMEOUT * 4,  # full export 는 길어질 수 있음
        )
        resp.raise_for_status()
        total = 0
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
                    total += len(chunk)
        size_mb = total / 1024 / 1024
        return f"export 완료 — {out_path} ({size_mb:.0f} MB, {format})"
    except Exception as e:
        return _gdb_error(e, "export_inferred")


def _resolve_inference_targets(
    tbox_path: str, abox_path: str, output_path: str, repo: str,
) -> tuple[str, str, str, str]:
    """추론 잡의 repository ID 와 입력·출력 경로를 GraphDB 요청 전에 확정한다.

    tbox_path·abox_path 는 graphdb_import_file 과 같은 규칙(DATA_DIR 안, RDF 확장자)으로,
    output_path 는 _resolve_export_path 규칙으로 해석한다. 비어 있는 입력은 기본
    TBOX_PATH·ABOX_PATH·INFERRED_PATH 다. 존재 확인·크기 조회·repository 생성보다 먼저
    호출하므로 경계 밖 경로의 메타데이터를 읽지 않고 원격 저장소도 바꾸지 않는다.

    Returns:
        (repository ID, T-Box 경로, A-Box 경로, export 경로)

    Raises:
        ValueError: 거부 사유. 메시지에는 입력·해석 경로를 넣지 않는다.
    """
    from config import ABOX_PATH, TBOX_PATH

    rid = _repo_id(repo)
    try:
        out_path = _resolve_export_path(output_path)
    except ValueError as e:
        raise ValueError(f"export 경로 거부: {e}") from e
    inputs: list[str] = []
    for name, raw, default in (
        ("tbox_path", tbox_path, TBOX_PATH),
        ("abox_path", abox_path, ABOX_PATH),
    ):
        if not raw:
            inputs.append(default)
            continue
        try:
            inputs.append(resolve_path_within(
                DATA_DIR, raw, allowed_suffixes=tuple(_CONTENT_TYPES),
            ))
        except ValueError as e:
            raise ValueError(f"{name} 경로 거부: {e}") from e
    return rid, inputs[0], inputs[1], out_path


def _graphdb_run_inference_sync(
    tbox_path: str = "",
    abox_path: str = "",
    repo: str = "",
    output_path: str = "",
    ruleset: str = "",
    overwrite_repo: bool = False,
    import_timeout: int = 7200,
) -> str:
    """대용량 입력에서 OWL 2 RL 추론을 GraphDB Free 로 실행 (동기 본문).

    최대 ~4시간 걸리는 블로킹 작업이다. 이 함수를 부르는 곳은 MCP 진입점
    graphdb_run_inference 의 잡 워커뿐이고, 진입점은 job_id 만 즉시 반환한다.
    run_owl_rl_inference 는 이 함수를 호출하지 않으며 입력을 GraphDB 로 보내지 않는다.

    Why: reasonable.PyReasoner 가 ~12M triples 를 넘어가면 비선형 비용 폭발
    (32GB RAM 노트북에서 미완료). GraphDB Free 는 Lucene+RocksDB 인덱스로
    52M+ triples 도 안정적으로 처리. 본 함수는 import → reinfer → export
    파이프라인을 한 번에 실행한다. 로컬 상한을 넘는 입력은 사용자가 이 도구를
    명시적으로 실행할 때만 GraphDB 로 처리된다.

    전제조건:
      - GraphDB Desktop.app 이 실행 중이고 http://localhost:7200 응답.
      - 라이선스 등록됨 (GRAPHDB_LITE 빌트인 또는 별도 신청).
      - 디스크 여유 공간 ~30 GB (storage + export 합산).

    동작:
      0. repository ID 와 tbox_path / abox_path / output_path 를 확정한다. 거부되면
         GraphDB 요청 없이 중단한다.
      1. graphdb_health 로 가동 확인 → 안 되면 명확한 에러 + 중단.
      2. repository 생성 (ruleset=empty). 같은 ID 가 이미 있으면 overwrite_repo=True
         일 때만 삭제 후 재생성하고, False 면 기존 repository 를 바꾸지 않고 중단한다.
      3. T-Box / A-Box / tacit/*.ttl 차례 import (timeout=import_timeout).
      4. ruleset 을 owl2-rl-optimized 로 전환 (addRuleset + defaultRuleset).
      5. SPARQL `INSERT DATA { [] sys:reinfer [] }` 트리거 — fixpoint 도달까지 대기.
      6. 최종 graph 를 N-Triples 로 export.

    Args:
        tbox_path: T-Box 경로. DATA_DIR 안의 .ttl / .nt / .nq / .rdf / .jsonld 만
                   허용하며 symlink 해석 후 검사한다. 기본 TBOX_PATH.
        abox_path: A-Box 경로. 규칙은 tbox_path 와 같다. 기본 ABOX_PATH.
        repo: GraphDB repository ID (영문자·숫자·'-'·'_'). 기본 GRAPHDB_REPOSITORY env,
              미지정 시 domain_config.json 의 namespace.prefix → '{prefix}-kg' 로 유도.
        output_path: 추론 결과 export 경로. data/generated/inferred 아래
                     .ttl / .nt / .ntriples 만 허용하며 import 전에 검사한다.
                     기본 INFERRED_PATH (turtle, 다운스트림 sparql_local /
                     validate_kg / semantic_dictionary 와 일관).
        ruleset: 추론 룰셋. 기본 GRAPHDB_RULESET (owl2-rl-optimized).
        overwrite_repo: True 면 같은 ID 의 기존 repository 와 그 데이터를 삭제한 뒤
                        재생성한다 (clean state). 기본 False.
        import_timeout: 청크당 client read timeout (초). 기본 7200 (2시간).
                        큰 단일 파일 import 시 600s 기본값으로는 부족함.

    Returns:
        JSON: success / engine="graphdb" / counts / output_path / 단계별 시간.
    """
    import json as _json
    import time as _time

    from config import SOURCE_TACIT_DIR

    rs = ruleset or GRAPHDB_RULESET
    # repository ID 와 입출력 경로는 GraphDB 요청과 수 시간 걸리는 import·reinfer 전에 확정한다.
    try:
        rid, tbox, abox, out_path = _resolve_inference_targets(
            tbox_path, abox_path, output_path, repo,
        )
    except ValueError as e:
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": str(e),
        }, ensure_ascii=False, indent=2)
    # 다운스트림이 INFERRED_PATH 를 turtle 로 파싱하므로, 확장자에 맞춰 export
    # 포맷을 결정. .nt / .ntriples 를 명시한 caller 만 N-Triples 로 받음.
    out_ext = os.path.splitext(out_path)[1].lower()
    export_format = "nt" if out_ext in (".nt", ".ntriples") else "turtle"
    stages: dict[str, float] = {}
    t_total = _time.monotonic()

    # 1. health check ────────────────────────────────────────
    t0 = _time.monotonic()
    health = graphdb_health()
    stages["health_check"] = round(_time.monotonic() - t0, 2)
    if "GraphDB OK" not in health:
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": "GraphDB 가 응답하지 않음",
            "hint": (
                f"{GRAPHDB_BASE_URL} 에 GraphDB Desktop.app 실행 후 재시도. "
                "Workbench UI 가 뜨고 라이선스가 등록되었는지 확인."
            ),
            "health_response": health,
        }, ensure_ascii=False, indent=2)

    # 2. repository 생성 (ruleset=empty) ─────────────────────
    t0 = _time.monotonic()
    create_resp = graphdb_create_repository(
        repo_id=rid, ruleset="empty", overwrite=overwrite_repo,
    )
    stages["create_repository"] = round(_time.monotonic() - t0, 2)
    if create_resp.startswith("이미 존재"):
        # 기존 repository 에 import·ruleset 전환·reinfer 를 덧씌우면 이전 데이터가
        # 결과에 섞이고 사용자의 저장소가 바뀐다. 삭제는 overwrite_repo=True 로만 한다.
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": f"repository 가 이미 존재해 변경하지 않았습니다: {rid}",
            "hint": (
                "기존 repository 와 그 데이터를 삭제하고 다시 만들려면 overwrite_repo=True, "
                "보존하려면 다른 repo ID 를 지정하세요."
            ),
            "stages": stages,
        }, ensure_ascii=False, indent=2)
    if "생성됨" not in create_resp:
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": f"repository 생성 실패: {create_resp}",
            "stages": stages,
        }, ensure_ascii=False, indent=2)

    # 3. import T-Box + A-Box + tacit ────────────────────────
    t0 = _time.monotonic()
    files: list[str] = []
    if os.path.exists(tbox):
        files.append(tbox)
    if os.path.exists(abox):
        files.append(abox)
    if os.path.isdir(SOURCE_TACIT_DIR):
        import glob
        files.extend(sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl"))))

    import_results: list[dict] = []
    for idx, path in enumerate(files, start=1):
        size_mb = os.path.getsize(path) / 1024 / 1024
        t_imp = _time.monotonic()
        logger.info(
            "graphdb_run_inference: import [%d/%d] %s (%.0f MB)",
            idx, len(files), os.path.basename(path), size_mb,
        )
        result = graphdb_import_file(
            path, repo=rid, timeout_override=import_timeout,
        )
        elapsed = round(_time.monotonic() - t_imp, 2)
        ok = "import 완료" in result
        import_results.append({
            "file": os.path.basename(path),
            "size_mb": round(size_mb, 1),
            "seconds": elapsed,
            "ok": ok,
            "result": result if not ok else "ok",
        })
        if not ok:
            return _json.dumps({
                "success": False,
                "engine": "graphdb",
                "error": f"import 실패 — {os.path.basename(path)}: {result}",
                "stages": stages,
                "import_progress": import_results,
            }, ensure_ascii=False, indent=2)

    stages["import_all"] = round(_time.monotonic() - t0, 2)
    stages["files_imported"] = len(files)

    # 4. ruleset 전환 (empty → owl2-rl-optimized) ────────────
    t0 = _time.monotonic()
    # ruleset 은 SPARQL 문자열 리터럴로 직렬화해 넣는다. 따옴표가 리터럴을 닫고
    # 같은 요청에 다른 update 연산을 덧붙이지 못한다.
    ruleset_literal = Literal(rs).n3()
    try:
        for sys_pred, _label in (
            ("addRuleset", "addRuleset"),
            ("defaultRuleset", "defaultRuleset"),
        ):
            resp = requests.post(
                f"{_repo_url(rid)}/statements",
                data={
                    "update": (
                        f"INSERT DATA {{ _:b "
                        f"<http://www.ontotext.com/owlim/system#{sys_pred}> "
                        f"{ruleset_literal} }}"
                    ),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=60,
            )
            resp.raise_for_status()
        stages["ruleset_switch"] = round(_time.monotonic() - t0, 2)
    except Exception as e:
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": f"ruleset 전환 실패: {e}",
            "stages": stages,
        }, ensure_ascii=False, indent=2)

    # 5. reinfer (fixpoint forward-chaining) ─────────────────
    t0 = _time.monotonic()
    try:
        # reinfer 는 매우 오래 걸릴 수 있으므로 큰 timeout 사용 (기본 import_timeout 의 2배).
        reinfer_timeout = max(import_timeout * 2, 14400)
        resp = requests.post(
            f"{_repo_url(rid)}/statements",
            data={
                "update": (
                    "INSERT DATA { [] "
                    "<http://www.ontotext.com/owlim/system#reinfer> [] }"
                ),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=reinfer_timeout,
        )
        resp.raise_for_status()
        stages["reinfer"] = round(_time.monotonic() - t0, 2)
    except Exception as e:
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": f"reinfer 실패: {e}",
            "stages": stages,
            "hint": (
                "reinfer 가 timeout 으로 끊겼어도 GraphDB 서버는 transaction "
                "을 commit 했을 가능성. graphdb_count_triples 로 확인."
            ),
        }, ensure_ascii=False, indent=2)

    # 6. counts 측정 ──────────────────────────────────────────
    counts = graphdb_count_triples(repo=rid)

    # 7. export ──────────────────────────────────────────────
    t0 = _time.monotonic()
    export_result = graphdb_export_inferred(
        output_path=out_path, repo=rid, only_inferred=False,
        format=export_format,
    )
    stages["export"] = round(_time.monotonic() - t0, 2)
    if "export 완료" not in export_result:
        return _json.dumps({
            "success": False,
            "engine": "graphdb",
            "error": f"export 실패: {export_result}",
            "stages": stages,
        }, ensure_ascii=False, indent=2)

    stages["total"] = round(_time.monotonic() - t_total, 2)

    return _json.dumps({
        "success": True,
        "engine": "graphdb",
        "repository": rid,
        "ruleset": rs,
        "counts": counts,
        "output_path": out_path,
        "output_size_bytes": os.path.getsize(out_path),
        "stages": stages,
        "imports": import_results,
    }, ensure_ascii=False, indent=2)


# GraphDB 추론 잡 레지스트리. reinfer 단계가 최대 ~4시간이라 동기 응답하면 MCP stdio
# 가 끊기므로 graphdb_run_inference 는 이 레지스트리로 워커를 띄운다. 로컬 추론
# (run_owl_rl_inference) 은 이 레지스트리도 _graphdb_run_inference_sync 도 쓰지 않는다.
_GRAPHDB_JOBS = JobRegistry(
    name="gdbinfer", poll_with="get_inference_status", logger=logger,
)


def graphdb_run_inference(
    tbox_path: str = "",
    abox_path: str = "",
    repo: str = "",
    output_path: str = "",
    ruleset: str = "",
    overwrite_repo: bool = False,
    import_timeout: int = 7200,
) -> str:
    """대용량 입력에서 OWL 2 RL 추론을 GraphDB Free 로 **백그라운드 실행**한다.

    ⚠️ 비동기 잡 패턴: daemon 워커를 띄우고 즉시 job_id 를 반환한다(수십 ms).
    실제 추론(import→reinfer→export)은 최대 ~4시간 걸리므로 동기 응답하면 MCP
    stdio 타임아웃으로 서버가 끊긴다. ``get_inference_status(job_id)`` 로
    폴링하라.

    run_owl_rl_inference 는 이 도구를 호출하지 않고 대용량 입력을 GraphDB 로 보내지도
    않는다. 로컬 상한을 넘는 입력은 사용자가 이 도구를 명시적으로 실행할 때만 GraphDB
    로 처리된다.

    주의: overwrite_repo=True 면 repo 와 같은 ID 의 기존 GraphDB repository 와 그 안의
    데이터를 삭제한 뒤 다시 만든다. 기본값 False 에서 그 repository 가 이미 있으면
    아무것도 바꾸지 않고 실패한다. 없으면 새로 만든다.

    Args:
        tbox_path: T-Box 경로. DATA_DIR 안의 .ttl / .nt / .nq / .rdf / .jsonld 만
                   허용하며 symlink 해석 후 검사한다. 기본 TBOX_PATH.
        abox_path: A-Box 경로. 규칙은 tbox_path 와 같다. 기본 ABOX_PATH.
        repo: GraphDB repository ID (영문자·숫자·'-'·'_'). 기본 GRAPHDB_REPOSITORY.
        output_path: 추론 결과 export 경로. data/generated/inferred 아래
                     .ttl / .nt / .ntriples 만 허용한다. 기본 INFERRED_PATH.
        ruleset: 추론 룰셋. 기본 GRAPHDB_RULESET (owl2-rl-optimized).
        overwrite_repo: True 일 때만 같은 ID 의 기존 repository 를 삭제하고
                        재생성한다. 기본 False.
        import_timeout: 파일당 client read timeout (초). 기본 7200 (2시간).

    repository ID 와 경로 인자는 잡을 띄우기 전에 검사하며, 거부되면 잡 없이
    {"started": false, "success": false, "engine": "graphdb", "error": str} 를 반환한다.

    Returns:
        {"started": bool, "job_id": str, "status": "running"|"reused",
         "poll_with": "get_inference_status", "message": str}
    """
    try:
        _resolve_inference_targets(tbox_path, abox_path, output_path, repo)
    except ValueError as e:
        return json.dumps({
            "started": False,
            "success": False,
            "engine": "graphdb",
            "error": str(e),
        }, ensure_ascii=False, indent=2)
    key = f"{tbox_path}|{abox_path}|{repo}|{ruleset}"
    return _GRAPHDB_JOBS.dispatch(
        key=key,
        worker=lambda: _graphdb_run_inference_sync(
            tbox_path, abox_path, repo, output_path, ruleset,
            overwrite_repo, import_timeout),
    )
