"""Step 7 — 온톨로지 메타데이터 보강.

본문 ontology_quality.py 의 Step 7 블록을 그대로 모듈로 옮김.

- owl:Ontology 타입 / owl:imports / owl:versionIRI / owl:versionInfo
- dcterms:creator/license/description/subject (FAIR F4 키워드)
- FAIR F1 persistent IRI (있으면 owl:sameAs)
- rdfs:comment (en/ko)
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    # DOMAIN_CONFIG 는 **원천에서 직접** 가져온다. tools.ontology_quality 를 경유해
    # re-import 하면 그 모듈에서 심볼이 사라지는 순간(예: 미사용 import 정리) 여기서
    # ImportError 가 나는데, 정적 분석은 이 간접 의존을 보지 못한다.
    from domain.namespaces import DOMAIN_CONFIG, IOF_ONTOLOGY_IRIS
    from tools.ontology_quality import (
        DC,
        ONT,
        ONTOLOGY_URI,
        _add_if_missing,
    )

    before = len(g)
    meta_added = 0

    meta_added += _add_if_missing(g, ONT, RDF.type, OWL.Ontology)

    # ``owl:imports`` 는 **온톨로지 IRI** 를 가리켜야 한다 — 용어 네임스페이스
    # (``IOF_CORE`` = ``…/ontology/construct/``) 를 넣으면 아무 파일의
    # ``owl:Ontology`` 선언과도 대응되지 않아 추론기가 네트워크 다운로드로 빠지고,
    # 그 IRI 는 HTML 리다이렉트라 **HermiT 이 전체 검증을 포기한다** (2026-08-18
    # 실측: "Cannot download …/construct/" 로 validate_owl_consistency 가 실패,
    # construct imports 만 제거하면 consistent=true + unsat 4건이 드러났다).
    #
    # IOF 는 **용어는 하나의 construct 네임스페이스, 온톨로지 IRI 는 모듈별** 로
    # 둔다. imports 는 후자를 쓴다 (동봉 Core.rdf / Maintenance.rdf /
    # SupplyChain.rdf 의 owl:Ontology 값과 정확히 일치).
    for imp in IOF_ONTOLOGY_IRIS:
        meta_added += _add_if_missing(g, ONT, OWL.imports, URIRef(imp))

    _version = DOMAIN_CONFIG["metadata"]["version"]
    meta_added += _add_if_missing(
        g, ONT, OWL.versionIRI,
        URIRef(f"{ONTOLOGY_URI}/{_version}"),
    )

    for s, p, o in list(g.triples((ONT, OWL.versionInfo, None))):
        g.remove((s, p, o))
    g.add((ONT, OWL.versionInfo, Literal(_version)))

    dc_items = [
        (DC.creator, Literal(DOMAIN_CONFIG["metadata"]["creator"])),
        (DC.license, URIRef(DOMAIN_CONFIG["metadata"]["license"])),
        (DC.description, Literal(
            DOMAIN_CONFIG["domain"]["description_en"],
            lang="en",
        )),
        (DC.description, Literal(
            DOMAIN_CONFIG["domain"]["description_ko"],
            lang="ko",
        )),
    ]
    _keywords = DOMAIN_CONFIG.get("metadata", {}).get(
        "keywords",
        ["steel manufacturing", "industrial ontology", "IOF",
         "knowledge graph", "철강", "제조"],
    )
    for kw in _keywords:
        dc_items.append((DC.subject, Literal(kw)))
    _persistent_iri = DOMAIN_CONFIG.get("metadata", {}).get("persistent_iri")
    if _persistent_iri:
        dc_items.append((OWL.sameAs, URIRef(_persistent_iri)))
    for pred, obj in dc_items:
        meta_added += _add_if_missing(g, ONT, pred, obj)

    for s, p, o in list(g.triples((ONT, RDFS.comment, None))):
        g.remove((s, p, o))
    g.add((ONT, RDFS.comment, Literal(
        f"IOF-based Steel Manufacturing T-Box Ontology "
        f"(v{_version} -- quality-improved)",
        lang="en",
    )))
    g.add((ONT, RDFS.comment, Literal(
        f"{DOMAIN_CONFIG['domain']['description_ko']} "
        f"(v{_version} -- 품질 개선)",
        lang="ko",
    )))

    return StepResult(
        name="step_07_metadata",
        stats={"metadata_triples_added": meta_added},
        triples_delta=len(g) - before,
        step_number=7,
        step_label="metadata_enrichment",
    )
