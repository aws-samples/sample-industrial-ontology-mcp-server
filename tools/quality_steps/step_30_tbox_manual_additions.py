"""Step 30 — T-Box 수동 추가분 자동 병합 (2026-06-25).

rules/domain/tbox_manual_additions.ttl 의 [A]/[C] 블록(고립 클래스 연결 OP + 차원
클래스/OP/DP)을 T-Box 그래프에 머지한다. S2(generate_tbox) 를 다시 돌려도 이
후처리 스텝이 수동 추가분을 결정적으로 복원하므로, 손으로 t_box.ttl 을 편집하던
일회성 패치가 파이프라인에 통합돼 일정한 품질을 유지한다.

패치 파일은 주석(#)이 섞인 Turtle 이므로, 주석을 제거하고 표준 prefix 를 앞에
붙여 별도 그래프로 파싱한 뒤 본 T-Box 그래프에 union 한다. 이미 동일 트리플이
있으면 rdflib set semantics 로 자연 dedup 된다.

파일이 없으면 no-op (도메인에 수동 추가분이 없으면 스킵). 파싱 실패는 warn 으로
처리해 파이프라인을 막지 않는다 (RuntimeError 아님).
"""
from __future__ import annotations

import logging
import os

from rdflib import OWL, RDF, Graph

from domain.rules_paths import rules_path
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_PATCH_FILENAME = "tbox_manual_additions.ttl"


def _patch_path() -> str:
    return rules_path(_PATCH_FILENAME)


# 패치 파일이 쓸 수 있는데 SPARQL_PREFIXES 에는 없는 어휘. 특히
# ``dcterms:source`` 는 04-property-rules.md 의 "DatatypeProperty declaration" 절이
# DP 마다 요구하므로 수동 추가분도 사용한다. 미선언이면 패치 전체가 파싱에 실패하고
# step_30 이 조용히 skip 되어 수동 추가분이 통째로 유실된다.
_EXTRA_PREFIXES: dict[str, str] = {
    "dcterms": "http://purl.org/dc/terms/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
}


def _turtle_prefix_header() -> str:
    """패치 파일 파싱용 @prefix 헤더 — domain.namespaces 의 SPARQL_PREFIXES 재사용.

    SPARQL_PREFIXES 는 ``PREFIX x: <uri>`` 형식 → Turtle ``@prefix x: <uri> .`` 로 변환.
    거기에 없는 표준 어휘는 ``_EXTRA_PREFIXES`` 로 보강한다.
    """
    from domain.namespaces import SPARQL_PREFIXES

    lines = []
    declared: set[str] = set()
    for ln in SPARQL_PREFIXES.splitlines():
        ln = ln.strip()
        if ln.startswith("PREFIX "):
            # "PREFIX steel: <uri>" → "@prefix steel: <uri> ."
            body = ln[len("PREFIX "):]
            lines.append("@prefix " + body + " .")
            declared.add(body.split(":", 1)[0].strip())
    for prefix, uri in _EXTRA_PREFIXES.items():
        if prefix not in declared:
            lines.append(f"@prefix {prefix}: <{uri}> .")
    return "\n".join(lines) + "\n"


def _conflicting_patch_dps(g: Graph, patch: Graph) -> set:
    """패치 DP 중 이미 같은 ``(domain, dcterms:source)`` 를 주장하는 것.

    A-Box 생성기는 두 DP 가 한 (클래스, 컬럼) 을 주장하면 **양쪽 모두 버린다**
    (``abox_generation`` 의 ``source_conflicts`` — 어느 쪽이 정본인지 판별 불가).
    그래서 패치가 S2 산출물과 겹치면 보완이 아니라 **파괴**가 된다.

    실측 2026-08-29: S2 가 ``steelmakingProductId`` (source=Product_ID) 를 스스로
    만든 실행에서 패치의 ``steelmakingProductIdRef`` 가 같은 컬럼을 주장해 양쪽이
    사라지고 FK 리터럴이 0건이 됐다. S2 산출물은 실행마다 달라지므로 패치 파일을
    손으로 맞추는 것은 재발한다 — 병합 시점에 양보하는 것이 정본이다.
    """
    from rdflib import RDFS, URIRef

    source_pred = URIRef(_EXTRA_PREFIXES["dcterms"] + "source")

    def _claims(graph: Graph, prop) -> set[tuple[str, str]]:
        cols = {str(o).strip().upper() for o in graph.objects(prop, source_pred)
                if str(o).strip()}
        doms = {str(d) for d in graph.objects(prop, RDFS.domain)}
        return {(d, c) for d in doms for c in cols}

    existing: set[tuple[str, str]] = set()
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        existing |= _claims(g, prop)

    conflicting = set()
    for prop in patch.subjects(RDF.type, OWL.DatatypeProperty):
        if _claims(patch, prop) & existing:
            conflicting.add(prop)
    return conflicting


def apply(g: Graph, ctx: StepContext) -> StepResult:
    path = _patch_path()
    if not os.path.exists(path):
        return StepResult(
            name="step_30_tbox_manual_additions",
            stats={"patch_present": False},
            step_number=30,
            step_label="tbox_manual_additions",
        )

    before = len(g)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        # 주석(#) 줄 제거 → 순수 Turtle 본문만
        body = "\n".join(
            ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")
        )
        patch = Graph()
        patch.parse(data=_turtle_prefix_header() + body, format="turtle")
    except Exception as exc:  # noqa: BLE001 — warn 정책, 파이프라인 비차단
        logger.warning("step_30: 패치 파일 파싱 실패 (skip): %s", exc)
        return StepResult(
            name="step_30_tbox_manual_additions",
            stats={"patch_present": True, "parse_error": str(exc)},
            error=str(exc),
            step_number=30,
            step_label="tbox_manual_additions",
        )

    # 같은 (domain, dcterms:source) 를 이미 주장하는 DP 가 그래프에 있으면 패치 DP 를
    # 건너뛴다. 두 DP 가 한 컬럼을 주장하면 A-Box 생성기가 **양쪽 모두 버리고**
    # transliteration 폴백으로 떨어진다 (abox_generation 의 source_conflicts) —
    # 실측 2026-08-29: S2 가 `steelmakingProductId` 를 스스로 만든 실행에서 패치의
    # `steelmakingProductIdRef` 와 충돌해 **양쪽이 사라지고 FK 리터럴이 0건**이 됐다.
    # 패치는 "S2 가 안 만들 때의 보완" 이므로 S2 산출물이 있으면 양보한다.
    skipped_dp_conflict = _conflicting_patch_dps(g, patch)
    if skipped_dp_conflict:
        logger.info(
            "step_30: 이미 같은 컬럼을 주장하는 DP 가 있어 패치 DP %d개 skip: %s",
            len(skipped_dp_conflict), sorted(skipped_dp_conflict)[:5],
        )

    # 새로 추가되는 트리플만 카운트 (이미 있으면 dedup)
    added_classes, added_ops, added_dps = 0, 0, 0
    new_triples = 0
    for s, p, o in patch:
        if s in skipped_dp_conflict:
            continue
        if (s, p, o) not in g:
            new_triples += 1
            if p == RDF.type:
                if o == OWL.Class:
                    added_classes += 1
                elif o == OWL.ObjectProperty:
                    added_ops += 1
                elif o == OWL.DatatypeProperty:
                    added_dps += 1
        g.add((s, p, o))

    stats = {
        "patch_present": True,
        "patch_triples": len(patch),
        "new_triples": new_triples,
        "added_classes": added_classes,
        "added_object_properties": added_ops,
        "added_datatype_properties": added_dps,
    }
    logger.info(
        "step_30: 수동 추가분 병합 — 신규 트리플 %d (클래스 +%d, OP +%d, DP +%d)",
        new_triples, added_classes, added_ops, added_dps,
    )
    return StepResult(
        name="step_30_tbox_manual_additions",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=30,
        step_label="tbox_manual_additions",
    )
