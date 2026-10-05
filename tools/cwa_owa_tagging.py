"""CWA/OWA 명시 태깅 — #19.

RDF는 기본적으로 Open World Assumption(OWA)이지만, 이 프로젝트에서는
3가지 데이터 유래가 섞여 있다:
- **CSV direct** (Closed World): 실제 마스터/트랜잭션 데이터. 누락은 데이터 이슈.
- **Tacit SME** (Open World, authoritative): 현장 전문가가 추가한 암묵지.
- **OWL RL inferred** (Derived): 추론으로만 존재. 명시적 주장 아님.

이 모듈은 각 트리플의 유래를 **reified provenance sidecar**에 기록하여
SPARQL로 필터 가능하게 한다. 기존 `tools/triple_confidence.py`의 CONFIDENCE
reification을 재사용.

기여:
- `tag_graph_origin(g, origin)` — 그래프 전체에 단일 origin 태그 일괄 부여
- `summarize_origin_distribution(sidecar)` — 원본별 트리플 수 요약
"""
from __future__ import annotations

import logging
from typing import Literal as _L

from rdflib import Graph, URIRef

from tools.common import error_response, success_response
from tools.triple_confidence import (
    annotate_triple,
    summarize_sidecar,
)

logger = logging.getLogger(__name__)


ALLOWED_ORIGINS = {"csv_direct", "tacit_sme", "owl_rl_1hop",
                   "owl_rl_chain", "bedrock_llm", "manual"}


def tag_graph_origin(
    src: Graph,
    origin: _L["csv_direct", "tacit_sme", "owl_rl_1hop",
              "owl_rl_chain", "bedrock_llm", "manual"],
    *,
    sidecar: Graph | None = None,
) -> Graph:
    """소스 그래프의 모든 트리플을 reified provenance로 태깅.

    Args:
        src: 원본 그래프 (A-Box / inferred_delta / tacit).
        origin: 유래 태그. DEFAULT_CONFIDENCE 키와 동일.
        sidecar: 누적 대상 reification 그래프. None이면 새로 생성.

    Returns:
        reified provenance가 추가된 sidecar 그래프.
    """
    if origin not in ALLOWED_ORIGINS:
        raise ValueError(
            f"Invalid origin {origin!r}. Allowed: {sorted(ALLOWED_ORIGINS)}",
        )
    if sidecar is None:
        from domain.tbox_utils import _new_graph
        sidecar = _new_graph()

    for s, p, o in src:
        if not isinstance(s, URIRef):
            continue  # BNode subject는 skip (reification 의미 약함)
        annotate_triple(sidecar, s, p, o, provenance=origin)
    return sidecar


def summarize_origin_distribution(sidecar_path: str) -> dict:
    """sidecar TTL을 파싱해 origin별 트리플 수 + CWA/OWA 카테고리 분포."""
    from domain.tbox_utils import _new_graph
    g = _new_graph()
    g.parse(sidecar_path, format="turtle")
    s = summarize_sidecar(g)

    # CWA/OWA 카테고리 roll-up
    cwa_origins = {"csv_direct", "manual"}
    owa_origins = {"tacit_sme", "bedrock_llm"}
    inferred_origins = {"owl_rl_1hop", "owl_rl_chain"}

    by_origin = s.get("by_provenance", {})
    cwa_count = sum(cnt for o, cnt in by_origin.items() if o in cwa_origins)
    owa_count = sum(cnt for o, cnt in by_origin.items() if o in owa_origins)
    inf_count = sum(cnt for o, cnt in by_origin.items() if o in inferred_origins)

    return {
        **s,
        "cwa_count": cwa_count,
        "owa_count": owa_count,
        "inferred_count": inf_count,
        "total_classified": cwa_count + owa_count + inf_count,
    }


def summarize_origin_sidecar(sidecar_path: str) -> str:
    """reified origin sidecar의 CWA/OWA/inferred 분포 요약 (#19).

    triple_confidence.annotate_triple로 태깅된 TTL에서 각 provenance 유형을
    closed-world (CSV), open-world (tacit), inferred로 rollup 분류해 반환.
    """
    try:
        r = summarize_origin_distribution(sidecar_path)
        return success_response(r)
    except Exception as e:
        return error_response(e, logger=logger)
