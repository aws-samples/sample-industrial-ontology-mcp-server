"""Step 12k — 한 CSV 컬럼을 여러 DP 가 주장할 때 하나만 남긴다.

``step_12g`` 는 **반대 방향** 이다 (한 DP 가 여러 컬럼을 주장). 이쪽은 여러 DP 가 한
컬럼을 주장하는 경우다.

## 왜 (2026-09-05 실측)

A-Box 생성기는 충돌을 감지하면 **양쪽 모두 버린다** (``abox_generation`` 의
``source_conflicts`` → ``dp_by_source.pop(key)``). 즉 T-Box 가 선언한 authoritative
매핑을 못 쓰고 **이름 추측 폴백** (transliteration / semantic suffix) 에 맡긴다.
2026-08-29 에는 그 폴백이 실패해 FK 리터럴 3개가 0건이 됐다.

배포 T-Box 실측 4쌍 — 전부 domain·range(xsd:string)·source 가 동일한, 의미상 같은 DP::

    EquipmentStatus.Status    equipmentStatusValue(6000) / equipmentStatusStatusText(0)
    ProductionPlan.Status     productionPlanStatus(157)  / productionPlanStatusText(0)
    PurchaseOrder.Status      purchaseOrderStatus(109)   / purchaseOrderStatusText(0)
    WasteManagement.Waste_Type wasteManagementWasteTypeText(150) / wasteManagementWasteType(0)

8개 DP 전부 **S2 출력에 이미 있었다** — 생성 지점 가드로는 잡히지 않는 부류다
(``step_13d``/``step_13e`` 와 같은 형태).

## 왜 승자를 이름으로 정해도 안전한가

주장을 하나로 만들면 ``_col_to_prop`` 의 **step 0 (authoritative source)** 이 복원되어
그 DP 가 값을 받는다. 즉 tie-break 는 "데이터가 실리는가" 를 바꾸지 않고 **어느 이름에
실리는가** 만 바꾼다 (딕셔너리는 뒤이어 재생성된다). 그래서 실측 근거가 없어도
결정론적 규칙으로 정할 수 있다 — 이 스텝이 하지 **않는** 것은 A-Box 사용량을 근거로
쓰는 것이다 (S3 는 S7 앞에 도므로 그 시점 사용량은 이전 세대다).

tie-break 순서:
  1. **canonical Path B 형** ``{classCamelLower}{ColumnCamel}`` 이 후보에 있으면 그것
     (문서화된 작명 규약이므로 사람이 예측할 수 있다). 실측 4쌍 중 3쌍이 여기서 갈린다.
  2. 없으면 **짧은 이름** (``…StatusText`` 같은 군더더기 접미를 배제한다).
  3. 길이도 같으면 사전순 (재현성).

## 무엇을 하지 않는가

* 패자 DP 를 **지우지 않는다.** ``dcterms:source`` 주장만 뗀다 — 유령 DP 정리는
  ``step_12f``/``step_21b`` 의 축이고, 여기서 겹치면 두 판정이 섞인다.
* 컬럼을 주장하는 DP 가 하나뿐이면 no-op (멱등).
* 도메인이 다른 DP 가 같은 컬럼명을 주장하는 것은 충돌이 아니다 (다른 테이블의 같은
  이름 컬럼) — 키에 domain 을 포함해 구분한다.
"""
from __future__ import annotations

import collections
import logging
import os

from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS = Namespace("http://purl.org/dc/terms/")

#: 환경변수 — ``resolve``(기본) / ``warn``(측정만) / ``off``.
_ENV_MODE = "TBOX_DUP_SOURCE_CLAIM"


def _local(uri: str) -> str:
    return str(uri).split("#")[-1].split("/")[-1]


def _canonical_name(class_local: str, column: str) -> str:
    """``{classCamelLower}{ColumnCamel}`` — 문서화된 Path B 작명 형."""
    cls_camel = class_local[:1].lower() + class_local[1:] if class_local else ""
    col_camel = "".join(p[:1].upper() + p[1:].lower() for p in column.split("_") if p)
    return f"{cls_camel}{col_camel}"


def _pick_winner(candidates: list[str], class_local: str, column: str) -> str:
    """결정론적 tie-break — canonical 형 → 짧은 이름 → 사전순."""
    canonical = _canonical_name(class_local, column)
    for name in sorted(candidates):
        if name.lower() == canonical.lower():
            return name
    return sorted(candidates, key=lambda n: (len(n), n))[0]


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    mode = os.getenv(_ENV_MODE, "resolve").strip().lower()
    stats: dict = {"dup_source_mode": mode}
    error: str | None = None

    if mode == "off":
        return StepResult(
            name="step_12k_duplicate_source_claim_resolve",
            stats={**stats, "dup_source_columns": 0},
            triples_delta=0,
            error=None,
            step_number=12,
            step_label="duplicate_source_claim_resolve",
        )

    try:
        ns = ctx.domain_ns
        # (domain IRI, UPPER 컬럼) → {DP local name: 원본 source 리터럴}
        claims: dict[tuple[str, str], dict[str, object]] = collections.defaultdict(dict)
        for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
            if not (isinstance(prop, URIRef) and str(prop).startswith(ns)):
                continue
            domains = [d for d in g.objects(prop, RDFS.domain)
                       if isinstance(d, URIRef) and str(d).startswith(ns)]
            for src in g.objects(prop, _DCTERMS.source):
                text = str(src).strip()
                if not text:
                    continue
                for dom in domains:
                    claims[(str(dom), text.upper())][_local(prop)] = src

        conflicts = {k: v for k, v in claims.items() if len(v) > 1}
        resolved: list[dict] = []
        removed = 0
        for (dom, column_upper), by_name in sorted(conflicts.items()):
            class_local = _local(dom)
            names = sorted(by_name)
            winner = _pick_winner(names, class_local, column_upper)
            losers = [n for n in names if n != winner]
            resolved.append({
                "class": class_local,
                "column": column_upper,
                "winner": winner,
                "dropped_claim_from": losers,
                "reason": (
                    "canonical" if winner.lower()
                    == _canonical_name(class_local, column_upper).lower()
                    else "shortest"
                ),
            })
            if mode != "resolve":
                continue
            for loser in losers:
                g.remove((URIRef(ns + loser), _DCTERMS.source, by_name[loser]))
                removed += 1

        if conflicts:
            logger.warning(
                "Step 12k: 한 컬럼을 여러 DP 가 주장 %d건 (mode=%s, 주장 제거 %d) — "
                "충돌이 남으면 A-Box 생성기가 authoritative 매핑을 버리고 이름 추측 "
                "폴백에 맡긴다: %s",
                len(conflicts), mode, removed,
                [f'{r["class"]}.{r["column"]}→{r["winner"]}' for r in resolved],
            )
        stats.update({
            "dup_source_columns": len(conflicts),
            "dup_source_claims_removed": removed,
            "dup_source_resolved": resolved[:10],
        })
    except Exception as e:                     # pragma: no cover — 스텝 격리
        logger.warning("Step 12k: 중복 source 주장 해소 실패: %s", e)
        error = str(e)

    return StepResult(
        name="step_12k_duplicate_source_claim_resolve",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=12,
        step_label="duplicate_source_claim_resolve",
    )
