"""Step 12g — 한 DP 가 여러 CSV 컬럼을 주장할 때 하나만 남긴다.

``dcterms:source`` 는 "이 DP 가 어느 CSV 컬럼에서 왔는가" 를 단일하게 지목해야
한다 (원칙 4-1: 컬럼 1개 = DP 1개). 여러 컬럼을 주장하면 A-Box 생성기가 각 컬럼을
순회하며 **같은 속성에 값을 합쳐** 넣는다.

**실측 (2026-07-27)**: ``orderStdNoBOld`` 가 ``STD_NO_B_OLD`` 와
``STD_NO_B_NEW`` 을 함께 주장했다. 두 컬럼은 7,272행에서 모두 채워지지만
**값이 같은 행이 0건** 인 서로 다른 항목이다 (구 체계 ``GRADE002`` vs 신 체계
``GRADE001``). 결과:

  - 값 트리플 14,544 > 인스턴스 약 1.2만 (초과 2,865)
  - 이 속성으로 필터하면 **두 체계의 표준번호가 섞여** 나온다
  - COUNT 집계가 중복 계수된다

형제 컬럼들은 규칙대로 분리돼 있었다 (``STD_NO_A_OLD`` → ``orderStdNoAOld`` /
``STD_NO_A_NEW`` → ``orderHotRollingStdNoANew``). 이 DP 만 예외였다.

**동작**: 여러 컬럼을 주장하는 DP 에서, **다른 DP 가 이미 주장하고 있는 컬럼** 표기를
제거한다. 그 컬럼은 전용 DP 가 담당하므로 이쪽에 남길 이유가 없다. 남는 컬럼이
하나가 되면 모호성이 해소된다.

전용 DP 가 없는 컬럼은 **건드리지 않는다** — 지우면 그 컬럼이 어느 DP 로도 적재되지
않아 데이터가 유실된다. 이 경우는 Step 12e 가 경고로 남기고, 담당자가
``rules/domain/tbox_manual_additions.ttl`` 에 전용 DP 를 선언해야 한다 (실제 조치 예: [E] 블록).

멱등: 이미 단일 컬럼이면 no-op.
"""
from __future__ import annotations

import collections
import logging

from rdflib import OWL, RDF, Graph, Namespace, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS = Namespace("http://purl.org/dc/terms/")


def _local(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """Drop redundant ``dcterms:source`` claims from multi-column DPs."""
    before = len(g)
    ns = ctx.domain_ns

    # column (UPPER) -> {dp local names claiming it}
    owners: dict[str, set[str]] = collections.defaultdict(set)
    claims: dict[str, list[tuple[str, object]]] = collections.defaultdict(list)
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(ns)):
            continue
        name = _local(str(dp))
        for obj in g.objects(dp, _DCTERMS.source):
            col = str(obj).strip().upper()
            if not col:
                continue
            owners[col].add(name)
            claims[name].append((col, obj))

    removed: list[dict] = []
    kept_ambiguous: list[dict] = []
    for name, entries in claims.items():
        cols = {c for c, _ in entries}
        if len(cols) < 2:
            continue
        # 다른 DP 가 전담하는 컬럼의 표기를 이 DP 에서 뺀다.
        droppable = [
            (c, obj) for c, obj in entries
            if len(owners[c]) > 1
        ]
        # 전부 빼면 출처가 사라지므로, 최소 하나는 남긴다.
        if len(droppable) >= len(entries):
            droppable = droppable[:-1]
        if not droppable:
            kept_ambiguous.append({"dp": name, "columns": sorted(cols)})
            continue
        dp_uri = URIRef(ns + name)
        for col, obj in droppable:
            g.remove((dp_uri, _DCTERMS.source, obj))
            removed.append({"dp": name, "column": col,
                            "handled_by": sorted(owners[col] - {name})})
        still = {c for c, _ in entries} - {c for c, _ in droppable}
        if len(still) > 1:
            kept_ambiguous.append({"dp": name, "columns": sorted(still)})

    if removed:
        logger.info(
            "Step 12g: 중복 출처 표기 %d건 제거 — 해당 컬럼은 전용 DP 가 담당 (예: %s)",
            len(removed), removed[:2],
        )
    if kept_ambiguous:
        logger.warning(
            "Step 12g: 전용 DP 가 없어 모호성이 남은 DP %d건 — 두 컬럼 값이 한 속성에 "
            "합쳐진다. rules/domain/tbox_manual_additions.ttl 에 전용 DP 를 선언할 것: %s",
            len(kept_ambiguous), kept_ambiguous[:3],
        )

    return StepResult(
        name="step_12g_dp_source_split",
        stats={
            "source_claims_removed": len(removed),
            "removed_sample": removed[:20],
            "ambiguous_remaining": len(kept_ambiguous),
            "ambiguous_sample": kept_ambiguous[:10],
        },
        triples_delta=len(g) - before,
        step_number="12g",
        step_label="dp_source_split",
    )
