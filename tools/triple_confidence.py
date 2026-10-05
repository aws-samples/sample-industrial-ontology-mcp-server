"""Per-triple Confidence Annotation (task code Q2; see docs/reference/task-glossary.md).

트리플 단위 신뢰도 + 출처 분류를 RDF reification으로 기록한다.
(rdflib 7.6이 RDF-star turtle 구문을 파싱 못해 reification 사용; 표준 RDF 1.1).

Schema per annotated triple:
    _:stmt_sha rdf:type rdf:Statement ;
               rdf:subject <s> ;
               rdf:predicate <p> ;
               rdf:object <o> ;
               steel-q:confidence "0.9"^^xsd:decimal ;
               steel-q:provenance "csv_direct" ;
               steel-q:source "Equipment.csv:row_42" .

출처별 기본 confidence:
    csv_direct:             1.0
    fk_verified:            0.95
    fk_unverified_promoted: 0.7
    owl_rl_1hop:            0.85
    owl_rl_chain:           max(1.0 / depth, 0.3)
    bedrock_llm:            0.6
    tacit_sme:              0.9

사용: A-Box/Inference 모듈이 confidence annotation을 별도 sidecar TTL에
함께 저장하면, downstream은 SPARQL로 `FILTER(?conf >= X)` 가능.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from typing import Any

from rdflib import XSD, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from config import GENERATED_DIR
from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.common import error_response, resolve_path_within

logger = logging.getLogger(__name__)

# 품질 annotation 전용 네임스페이스
QUALITY_NS = Namespace(str(DOMAIN_NS).rstrip("#") + "-quality#")
CONFIDENCE = URIRef(str(QUALITY_NS) + "confidence")
PROVENANCE = URIRef(str(QUALITY_NS) + "provenance")
SOURCE = URIRef(str(QUALITY_NS) + "source")
# R3 — Wikidata 스타일 확장
RANK = URIRef(str(QUALITY_NS) + "rank")          # preferred | normal | deprecated
REFERENCE = URIRef(str(QUALITY_NS) + "reference")  # URL/DOI/CSV row 식별자

DEFAULT_CONFIDENCE: dict[str, float] = {
    "csv_direct": 1.0,
    "fk_verified": 0.95,
    "fk_unverified_promoted": 0.7,
    "owl_rl_1hop": 0.85,
    "bedrock_llm": 0.6,
    "tacit_sme": 0.9,
    "manual": 1.0,
    "unknown": 0.5,
}

# R3 — Wikidata rank 가중치. 동일 subject-predicate의 여러 값 중 preferred
# 가 우선하고 deprecated는 제외. 쿼리 필터 hint로 사용.
RANK_WEIGHTS: dict[str, float] = {
    "preferred": 1.0,
    "normal": 0.8,
    "deprecated": 0.0,
}
VALID_RANKS = set(RANK_WEIGHTS.keys())


def confidence_for(provenance: str, *, chain_depth: int = 0) -> float:
    """출처 문자열 → 기본 confidence 계산."""
    if provenance == "owl_rl_chain" and chain_depth > 0:
        return max(round(1.0 / chain_depth, 3), 0.3)
    return DEFAULT_CONFIDENCE.get(provenance, 0.5)


def _stmt_uri(s, p, o) -> URIRef:
    """(s,p,o) 해시 기반 statement URI."""
    key = f"{s}|{p}|{o}".encode()
    h = hashlib.sha256(key).hexdigest()[:16]
    return URIRef(str(QUALITY_NS) + f"stmt_{h}")


def annotate_triple(
    g: Graph,
    s: Any,
    p: Any,
    o: Any,
    *,
    provenance: str,
    confidence: float | None = None,
    source: str | None = None,
    rank: str | None = None,
    reference: str | list[str] | None = None,
) -> URIRef:
    """그래프에 reified statement + confidence 추가. stmt URI 반환.

    R3: Wikidata 스타일 확장
    - rank: "preferred" | "normal" | "deprecated" (기본 "normal" 미설정)
    - reference: 출처 URL/DOI/CSV row id. 문자열 또는 문자열 리스트.

    Raises:
        ValueError: rank가 VALID_RANKS 밖일 때.
    """
    stmt = _stmt_uri(s, p, o)
    g.add((stmt, RDF.type, RDF.Statement))
    g.add((stmt, RDF.subject, s if isinstance(s, URIRef) else URIRef(str(s))))
    g.add((stmt, RDF.predicate, p if isinstance(p, URIRef) else URIRef(str(p))))
    g.add((stmt, RDF.object, o))
    conf = confidence if confidence is not None else confidence_for(provenance)
    g.add((stmt, CONFIDENCE, Literal(conf, datatype=XSD.decimal)))
    g.add((stmt, PROVENANCE, Literal(provenance)))
    if source:
        g.add((stmt, SOURCE, Literal(source)))
    if rank is not None:
        if rank not in VALID_RANKS:
            raise ValueError(
                f"Invalid rank {rank!r}. Expected one of {sorted(VALID_RANKS)}",
            )
        g.add((stmt, RANK, Literal(rank)))
    if reference is not None:
        refs = [reference] if isinstance(reference, str) else list(reference)
        for r in refs:
            # URL이면 URIRef, 그 외는 Literal (DOI:xxx, CSV row식별자 등)
            if isinstance(r, str) and (r.startswith("http://") or r.startswith("https://")):
                g.add((stmt, REFERENCE, URIRef(r)))
            else:
                g.add((stmt, REFERENCE, Literal(r)))
    return stmt


def filter_by_rank(
    sidecar_graph: Graph,
    allowed_ranks: set[str] | None = None,
) -> list[tuple[Any, Any, Any, str | None]]:
    """rank 필터로 (s,p,o,rank) 반환. deprecated 기본 제외.

    Args:
        allowed_ranks: 허용할 rank 집합. None이면 {"preferred","normal"}.
    """
    allowed = allowed_ranks or {"preferred", "normal"}
    results: list[tuple[Any, Any, Any, str | None]] = []
    for stmt in sidecar_graph.subjects(RDF.type, RDF.Statement):
        ranks = list(sidecar_graph.objects(stmt, RANK))
        rank_val = str(ranks[0]) if ranks else None
        # rank가 선언된 경우 필터 적용, 없으면 기본 normal로 간주
        effective_rank = rank_val if rank_val else "normal"
        if effective_rank not in allowed:
            continue
        subjs = list(sidecar_graph.objects(stmt, RDF.subject))
        preds = list(sidecar_graph.objects(stmt, RDF.predicate))
        objs = list(sidecar_graph.objects(stmt, RDF.object))
        if subjs and preds and objs:
            results.append((subjs[0], preds[0], objs[0], rank_val))
    return results


def get_references(sidecar_graph: Graph, stmt_uri: URIRef) -> list[str]:
    """특정 reified statement의 reference 목록."""
    return [str(r) for r in sidecar_graph.objects(stmt_uri, REFERENCE)]


def build_confidence_sidecar(
    annotations: list[dict],
) -> Graph:
    """annotation dict 리스트를 reified Graph로 변환.

    annotation: {"s", "p", "o", "provenance",
                 optional: "confidence", "source", "rank", "reference"}
    R3: rank/reference 필드 추가 지원 (Wikidata 스타일).
    """
    g = _new_graph()
    g.bind("steel-q", QUALITY_NS)
    for a in annotations:
        annotate_triple(
            g, a["s"], a["p"], a["o"],
            provenance=a["provenance"],
            confidence=a.get("confidence"),
            source=a.get("source"),
            rank=a.get("rank"),
            reference=a.get("reference"),
        )
    return g


def write_confidence_sidecar(annotations: list[dict], output_path: str) -> int:
    g = build_confidence_sidecar(annotations)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    g.serialize(destination=output_path, format="turtle")
    return len(g)


def filter_by_confidence(
    sidecar_graph: Graph,
    min_confidence: float,
) -> list[tuple[Any, Any, Any, float]]:
    """sidecar에서 confidence >= threshold인 reified 트리플을 (s,p,o,conf)로 추출."""
    results: list[tuple[Any, Any, Any, float]] = []
    for stmt in sidecar_graph.subjects(RDF.type, RDF.Statement):
        confs = list(sidecar_graph.objects(stmt, CONFIDENCE))
        if not confs:
            continue
        try:
            conf = float(confs[0].toPython())
        except Exception:  # noqa: BLE001 — 숫자가 아닌 confidence 값은 필터 대상에서 제외한다
            continue
        if conf < min_confidence:
            continue
        subjs = list(sidecar_graph.objects(stmt, RDF.subject))
        preds = list(sidecar_graph.objects(stmt, RDF.predicate))
        objs = list(sidecar_graph.objects(stmt, RDF.object))
        if subjs and preds and objs:
            results.append((subjs[0], preds[0], objs[0], conf))
    return results


def summarize_sidecar(sidecar_graph: Graph) -> dict:
    """sidecar 파일의 confidence/provenance 분포 요약."""
    total = 0
    by_prov: dict[str, int] = {}
    conf_vals: list[float] = []
    by_rank: dict[str, int] = {}
    with_reference = 0
    for stmt in sidecar_graph.subjects(RDF.type, RDF.Statement):
        total += 1
        confs = list(sidecar_graph.objects(stmt, CONFIDENCE))
        if confs:
            with contextlib.suppress(Exception):
                conf_vals.append(float(confs[0].toPython()))
        provs = list(sidecar_graph.objects(stmt, PROVENANCE))
        if provs:
            key = str(provs[0])
            by_prov[key] = by_prov.get(key, 0) + 1
        # R3: rank / reference 집계
        ranks = list(sidecar_graph.objects(stmt, RANK))
        if ranks:
            r = str(ranks[0])
            by_rank[r] = by_rank.get(r, 0) + 1
        refs = list(sidecar_graph.objects(stmt, REFERENCE))
        if refs:
            with_reference += 1
    conf_vals.sort()
    n = len(conf_vals)
    def pct(p):
        return (
            conf_vals[min(n - 1, max(0, int(n * p / 100)))] if n else 0
        )
    return {
        "total_statements": total,
        "by_provenance": by_prov,
        "by_rank": by_rank,
        "with_reference_count": with_reference,
        "reference_coverage_pct": round(
            with_reference / max(total, 1) * 100, 1,
        ),
        "confidence": {
            "mean": round(sum(conf_vals) / n, 3) if n else 0,
            "p50": round(pct(50), 3) if n else 0,
            "p10": round(pct(10), 3) if n else 0,
            "min": round(conf_vals[0], 3) if n else 0,
            "max": round(conf_vals[-1], 3) if n else 0,
        },
    }


def summarize_confidence_sidecar(sidecar_path: str) -> str:
    """confidence sidecar TTL의 분포 요약 (Q2).

    Args:
        sidecar_path: reified statement가 담긴 data/generated 아래 TTL 파일 경로
            (절대경로 또는 작업 디렉터리 기준 상대경로). symlink 해석 후에도
            data/generated 안이어야 한다.
    """
    try:
        sidecar_path = resolve_path_within(
            GENERATED_DIR,
            sidecar_path,
            allowed_suffixes=(".ttl",),
        )
    except Exception as e:
        return error_response(e, logger=logger)
    if not os.path.exists(sidecar_path):
        return error_response(
            f"sidecar not found: {sidecar_path}",
            logger=logger,
        )
    try:
        g = _new_graph()
        g.parse(sidecar_path, format="turtle")
        summary = summarize_sidecar(g)
        return json.dumps({"success": True, "path": sidecar_path, **summary},
                          ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(f"parse failed: {e}", logger=logger)
