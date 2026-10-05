"""Step 12d-2 — ``dcterms:source`` 값에 새어든 Turtle 표기 정리.

LLM 은 이 필드에 직렬화 표기를 그대로 담아 보낸다. 실측 (2026-08-10 배포 T-Box):
``dcterms:source "\\"Event_ID\\"^^xsd:string"^^xsd:string`` 형태가 **34건**.

왜 치명적인가: A-Box 는 이 값을 ``str(src).strip().upper()`` 로 **CSV 헤더와 직접
비교**한다 (``abox_generation`` 의 출처 인덱스). ``'"EVENT_ID"^^XSD:STRING'`` 은
어떤 헤더와도 일치하지 않으므로 그 컬럼은 조용히 transliteration 폴백으로
떨어진다 — ``dcterms:source`` 를 쓰는 이유(이름 추측 제거) 자체가 무력화된다.

왜 게이트가 못 잡았나: Step 12e 는 **존재 여부만** 센다. 오염된 값도 "표기됨" 으로
집계되므로 커버리지는 정상으로 보였다.

생성 지점(DSL / jury_fixes)은 각각 정규화되었지만, 이 스텝은 **이미 오염된 산출물**
과 앞으로 유입될 외부 TTL 을 위한 그물이다. 값이 이미 깨끗하면 no-op 이다.
"""
from __future__ import annotations

import logging

from rdflib import Graph, Literal, Namespace, URIRef

from domain.graph_utils import clean_source_column
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    cleaned = 0
    dropped = 0
    samples: list[str] = []

    for subject, _, obj in list(g.triples((None, _DCTERMS_NS.source, None))):
        if not isinstance(subject, URIRef):
            continue
        raw = str(obj)
        # object 가 **IRI 노드** 인 경우: 컬럼명 자리에 IRI 가 들어간 것이다.
        # 실측: LLM 이 `dcterms:source` 값으로 `xsd:string` 을 보내 그것이 IRI 로
        # 승격돼 `steel:string` 이 됐다 (5건). CSV 헤더가 IRI 일 수는 없으므로
        # **컬럼 표기로 쓸 수 없는 값** 이고, 남겨두면 커버리지만 부풀린다.
        if isinstance(obj, URIRef):
            g.remove((subject, _DCTERMS_NS.source, obj))
            dropped += 1
            if len(samples) < 5:
                local = str(subject).split("#")[-1].split("/")[-1]
                samples.append(f"{local}: IRI {raw!r} 제거 (컬럼명이 아니다)")
            continue
        fixed = clean_source_column(raw)
        if fixed == raw:
            continue
        g.remove((subject, _DCTERMS_NS.source, obj))
        if fixed:
            g.add((subject, _DCTERMS_NS.source, Literal(fixed)))
            cleaned += 1
            if len(samples) < 5:
                local = str(subject).split("#")[-1].split("/")[-1]
                samples.append(f"{local}: {raw!r} → {fixed!r}")
        else:
            # 정리 후 빈 값 — 표기가 없는 것과 같으므로 남기지 않는다. A-Box 는
            # 빈 문자열을 컬럼으로 착각하지 않고 폴백으로 간다.
            dropped += 1

    if cleaned or dropped:
        logger.warning(
            "Step 12d-2: dcterms:source 값 정리 %d건 / 빈 값 제거 %d건. "
            "이 값들은 CSV 헤더와 비교되므로 Turtle 표기가 섞이면 매칭이 "
            "영구히 실패한다 (예: %s)",
            cleaned, dropped, samples,
        )

    return StepResult(
        name="step_12d2_source_literal_cleanup",
        stats={
            "source_literal_cleaned": cleaned,
            "source_literal_dropped": dropped,
            "source_literal_samples": samples,
        },
        triples_delta=len(g) - before,
        step_number="12d-2",
        step_label="source_literal_cleanup",
    )
