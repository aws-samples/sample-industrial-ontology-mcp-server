"""RDF Canonical Forms — D (근거: Hogan 2017 [22]).

RDF 그래프의 canonical labeling을 통해 **구조적 동등성**을 수학적으로
비교한다. rdflib의 to_canonical_graph (URDNA2015 구현)를 사용:
Hogan 2017의 canonical form과 목적이 동일하다(blank node 의존성 제거 +
트리플 정렬).

활용:
- 배포 전/후 KG가 의도한 변경만 포함하는가 (회귀 동등성 검증)
- 두 버전의 T-Box가 의미적으로 같은가
- blank node 이름 차이에 영향 없이 비교

도구:
- canonicalize_graph(path): 정규 TTL + SHA256 해시 반환
- compare_canonical(path_a, path_b): 두 그래프 동등/차이 분류

두 MCP 도구의 입력 경로는 symlink 해석 후 data/generated 안의 .ttl 파일이어야 한다.
"""
from __future__ import annotations

import hashlib
import logging
import os

from rdflib import Graph
from rdflib.compare import graph_diff, isomorphic, to_canonical_graph

from config import GENERATED_DIR, GENERATED_REPORTS_DIR, INFERRED_PATH, TBOX_PATH
from domain.tbox_utils import _new_graph
from tools.common import (
    error_response,
    is_deployed_input,
    resolve_path_within,
    success_response,
    write_deployed_sidecar,
)

logger = logging.getLogger(__name__)


def _load(path: str) -> Graph:
    g = _new_graph()
    g.parse(path, format="turtle")
    return g


def _resolve_graph_path(path: str) -> str:
    """MCP 입력 그래프 경로를 data/generated 안의 .ttl 파일로 제한한다."""
    return resolve_path_within(GENERATED_DIR, path, allowed_suffixes=(".ttl",))


def canonical_nt_lines(g: Graph) -> list[str]:
    """그래프를 canonical N-Triples로 직렬화하고 정렬된 라인 리스트 반환."""
    cg = to_canonical_graph(g)
    nt = cg.serialize(format="nt")
    lines = [line for line in nt.splitlines() if line.strip()]
    lines.sort()
    return lines


def canonical_hash(g: Graph) -> str:
    """정규 N-Triples의 SHA256."""
    lines = canonical_nt_lines(g)
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def canonicalize_file(path: str) -> dict:
    if not os.path.exists(path):
        return {"error": f"not found: {path}"}
    g = _load(path)
    lines = canonical_nt_lines(g)
    digest = hashlib.sha256()
    for ln in lines:
        digest.update(ln.encode())
        digest.update(b"\n")
    return {
        "path": path,
        "triple_count": len(g),
        "canonical_line_count": len(lines),
        "sha256": digest.hexdigest(),
    }


def _summarize_diff(g_only: Graph, limit: int = 20) -> list[str]:
    out = []
    for s, p, o in list(g_only)[:limit]:
        out.append(f"{s} {p} {o}")
    return out


def compare_graphs(path_a: str, path_b: str) -> dict:
    if not os.path.exists(path_a):
        return {"error": f"A not found: {path_a}"}
    if not os.path.exists(path_b):
        return {"error": f"B not found: {path_b}"}
    ga = _load(path_a)
    gb = _load(path_b)

    iso = isomorphic(ga, gb)
    ha = canonical_hash(ga)
    hb = canonical_hash(gb)

    result = {
        "path_a": path_a,
        "path_b": path_b,
        "triples_a": len(ga),
        "triples_b": len(gb),
        "canonical_hash_a": ha,
        "canonical_hash_b": hb,
        "isomorphic": bool(iso),
        "hash_equal": ha == hb,
    }

    if not iso:
        in_both, only_a, only_b = graph_diff(ga, gb)
        result["only_in_a_count"] = len(only_a)
        result["only_in_b_count"] = len(only_b)
        result["only_in_a_sample"] = _summarize_diff(only_a)
        result["only_in_b_sample"] = _summarize_diff(only_b)
    return result


def canonicalize_graph(path: str) -> str:
    """RDF 그래프를 canonical form으로 정규화하고 SHA256 지문 반환 (D, [22]).

    blank node 이름 차이 · 트리플 순서를 제거한 동등성 비교 기준점을
    제공. KG 버전 관리/회귀 검증에 사용.

    Args:
        path: data/generated 아래 TTL 파일 경로 (절대경로 또는 작업 디렉터리 기준 상대경로).
    """
    try:
        r = canonicalize_file(_resolve_graph_path(path))
        return success_response(r)
    except Exception as e:
        return error_response(f"canonicalize 실패: {e}", logger=logger)


def compare_canonical(path_a: str, path_b: str) -> str:
    """두 RDF 그래프의 canonical 동등성 비교 (D, [22]).

    isomorphic(수학적 동등) + SHA256 해시 일치 + 차이 트리플 샘플을 반환.
    배포 전/후, 추론 전/후 구조 변화 회귀 검증에 사용.

    Args:
        path_a: data/generated 아래 기준 TTL 파일 경로 (절대경로 또는 작업 디렉터리 기준 상대경로).
        path_b: data/generated 아래 비교 TTL 파일 경로 (절대경로 또는 작업 디렉터리 기준 상대경로).
    """
    try:
        path_a = _resolve_graph_path(path_a)
        path_b = _resolve_graph_path(path_b)
        r = compare_graphs(path_a, path_b)
        # 입력은 data/generated 안의 임의의 두 경로지만 사이드카 출력 경로는 고정이다.
        # 애드혹 비교가 배포 감사 증거를 덮어쓰지 않도록 path_a 가 배포 산출물일 때만 쓴다.
        deployed = is_deployed_input(path_a, TBOX_PATH) or is_deployed_input(
            path_a, INFERRED_PATH,
        )
        write = write_deployed_sidecar(
            os.path.join(GENERATED_REPORTS_DIR, "canonical_compare.json"),
            r,
            sources={"graph_a": path_a, "graph_b": path_b},
            inputs_are_deployed=deployed,
            logger=logger,
        )
        return success_response({**r, "report_written": write})
    except Exception as e:
        return error_response(f"compare 실패: {e}", logger=logger)
