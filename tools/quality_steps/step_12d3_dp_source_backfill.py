"""Step 12d-3 — 커버리지 판정을 ``dcterms:source`` 로 **역기록**.

## 왜 필요한가

A-Box 는 ``dcterms:source`` 값을 CSV 헤더와 직접 비교해 "이 DP 는 이 컬럼에서
온다" 를 확정한다 (``abox_generation`` 출처 인덱스, 다른 모든 매칭 경로보다 **먼저**
짧은 회로로 확정).

실측 (2026-08-11 배포 T-Box): DP 254개 중 **147개가 미표기** (커버리지 40.6%).
147개 **전부 S2 초안 출처** 이고 S3 기여는 0이다. ``tools/tbox_generation.py`` 에는
``dcterms:source`` 를 보강하는 단계가 **아예 없어** LLM 이 TTL 에 쓴 것만 살아남는다.
프롬프트는 "source 필수" 라고 명시하지만(``multi_agent_prompts.py:248``) 준수율이
42% 다.

## 이득의 크기를 정직하게 적는다 — "컬럼 유실 방지" 가 주효과가 **아니다**

이 스텝의 첫 근거는 "미표기면 컬럼이 조용히 유실된다" 였는데, 검증에서 **과대평가로
반박됐다**. S7 은 기본값 ``use_dict_contract=True`` 로 돌기 때문에 (``abox_generation``
기본 인자), 출처 표기가 없어도 **딕셔너리 계약(S6.5)** 이 1단계에서 대부분을 해결한다.
실측: 미표기 컬럼 300쌍 스윕에서 계약을 적용하면 ``same=298``, 표기 추가로 새로
적재되는 컬럼은 **2개** (``MaintenanceHistory.Type`` / ``.Description``) 뿐이다.
``Molten_Steel_Temp_C`` → ``steelmakingMoltenSteelTempC`` 류 17건은 이미 계약이 잡는다.

그래서 이 스텝의 실제 가치는 **적재량 증가가 아니라 계약의 명시화** 다:

- **추측 경로 제거** — 계약·transliteration 폴백에 의존하던 113건이 선언으로 바뀐다.
  딕셔너리가 낡거나(S6.5 미실행) 클래스명이 바뀌면 폴백은 깨지지만 표기는 남는다.
- **SME 가독성·감사성** — ``dcterms:source`` 는 사람이 CSV 와 대조하는 계약이다.
- **게이트 정합** — Step 12e 가 요구하는 표기를 S3 가 스스로 충족한다 (40.6 → 86.6%).

적재 관점의 순증이 2개라는 사실을 숨기지 않는다. 표기를 "유실 방지" 로 정당화하면
다음 사람이 계약 경로를 지워도 안전하다고 오해한다.

## 추측이 아니다 — 이미 내린 판정의 전사(轉寫)다

``resolve_column_owner`` (= 커버리지 게이트가 쓰는 사다리) 가 이미 **어느 DP 가 어느
컬럼을 담당하는지** 판정한다. Step 12c 게이트는 그 판정으로 "이 컬럼은 커버됐다" 를
보고한다. 즉 시스템은 답을 알고 **기록만 안 하고** 있었다. 이 스텝은 그 답을
``dcterms:source`` 로 적어 A-Box 가 같은 추정을 처음부터 다시 하지 않게 한다.

## 기록 조건 (네 개 모두 만족해야 한다)

1. **대상 DP 가 미표기** — 이미 표기가 있으면 절대 손대지 않는다 (멱등성도 여기서 온다).
2. **가장 먼저 성립한 단계만** 인정 — 게이트의 짧은 회로 순서를 그대로 따라야
   기록자와 게이트가 서로 다른 답을 내지 않는다.
3. **단계 상한 5** — stage 6(``contains``)은 거부한다. 실측상 오늘 손실은 0이고,
   stage 0 소유자가 사라지는 순간 생기는 다중 후보 충돌을 미리 막는다.
4. **(class, column) 스코프 양방향 유일** — 한 쌍에 후보 DP 가 정확히 1개이고, 그 DP
   가 그 클래스에서 정확히 1개 컬럼만 주장할 때만 기록한다. **컬럼 단독으로 경쟁을
   판정하면 안 된다** — ``Timestamp`` 는 여러 테이블에 있어 정당한 기록 34건이
   잘못 버려진다 (실측).

모호하면 **기록하지 않고** stats 로 보고한다. 틀린 확신은 폴백보다 나쁘다 — A-Box
출처 인덱스는 다른 모든 매칭 경로보다 **먼저** 짧은 회로로 확정되므로, 잘못된
컬럼을 적으면 올바른 폴백조차 타지 못한다.
"""
from __future__ import annotations

import logging
import os

from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")

#: stage 6(contains)은 과매칭 위험이 커 기록에서 배제한다. 판정(게이트)은 6까지 쓴다.
_MAX_WRITE_STAGE = 5


def _local(node) -> str:
    return str(node).split("#")[-1].split("/")[-1]


def apply(g: Graph, ctx: StepContext) -> StepResult:
    if (os.getenv("TBOX_DP_SOURCE_BACKFILL") or "true").strip().lower() in (
        "false", "0", "off", "no",
    ):
        return StepResult(
            name="step_12d3_dp_source_backfill",
            stats={"backfill_disabled": True},
            step_number="12d-3", step_label="dp_source_backfill",
        )

    before = len(g)
    from tools.ontology_quality import (
        _collect_expected_columns_per_class,
        resolve_column_owner,
    )

    steel_str = ctx.domain_ns
    recorded = 0
    ambiguous = 0
    rejected_loose = 0
    samples: list[str] = []

    try:
        expected = _collect_expected_columns_per_class()
    except Exception as exc:  # noqa: BLE001 — CSV 없으면 아무것도 하지 않는다
        logger.warning("Step 12d-3: CSV 스캔 실패, 역기록 skip: %s", exc)
        return StepResult(
            name="step_12d3_dp_source_backfill",
            stats={"backfill_error": str(exc)[:200]},
            step_number="12d-3", step_label="dp_source_backfill",
        )

    # 클래스별: 선언된 DP 전체 / 미표기 DP / 이미 표기된 컬럼
    dps_by_class: dict[str, dict[str, URIRef]] = {}
    sourced_cols: dict[str, set[str]] = {}
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(dp).startswith(steel_str):
            continue
        existing = [str(o).strip() for o in g.objects(dp, _DCTERMS_NS.source)]
        existing = [e for e in existing if e]
        for dom in g.objects(dp, RDFS.domain):
            if not (isinstance(dom, URIRef) and str(dom).startswith(steel_str)):
                continue
            cls = _local(dom)
            dps_by_class.setdefault(cls, {})[_local(dp).lower()] = dp
            if existing:
                sourced_cols.setdefault(cls, set()).update(e.upper() for e in existing)

    for cls, meta in expected.items():
        declared = dps_by_class.get(cls)
        if not declared:
            continue
        class_camel = cls[0].lower() + cls[1:] if cls else cls
        already = sourced_cols.get(cls, set())
        # ``expected_columns`` 는 소문자화돼 있다. ``dcterms:source`` 는 사람이 읽고
        # SME 가 CSV 와 대조하는 계약이므로 **원본 헤더 케이스** 로 적는다
        # (``Event_ID``, not ``event_id``). A-Box 는 ``upper()`` 로 비교하니 매칭에는
        # 영향이 없지만, LLM 이 쓴 기존 107건이 원본 케이스라 섞이면 읽기 어렵다.
        original_case = {
            col.lower(): col for col in (meta.get("all_columns") or ())
        }

        # pass 1 — 제안만 모은다 (기록하지 않는다).
        proposals: dict[str, set[str]] = {}   # dp_lower -> {column}
        claims: dict[str, set[str]] = {}      # column   -> {dp_lower}
        for column in meta.get("expected_columns") or ():
            owner = resolve_column_owner(
                column.lower(), class_camel, set(declared),
                already, max_stage=_MAX_WRITE_STAGE,
            )
            if owner is None:
                continue
            dp_lower, stage, candidates = owner
            if stage == 0:
                continue                      # 이미 표기됨
            if not dp_lower:
                ambiguous += 1                # 후보 2개 이상
                continue
            if list(g.objects(declared[dp_lower], _DCTERMS_NS.source)):
                continue                      # 이 DP 는 이미 표기됨
            proposals.setdefault(dp_lower, set()).add(column)
            claims.setdefault(column.upper(), set()).add(dp_lower)

        # pass 2 — 양방향 유일할 때만 기록한다.
        for dp_lower, columns in proposals.items():
            if len(columns) != 1:
                ambiguous += 1                # 한 DP 가 여러 컬럼을 주장
                continue
            column = next(iter(columns))
            if len(claims.get(column.upper(), ())) != 1:
                ambiguous += 1                # 한 컬럼을 여러 DP 가 주장
                continue
            header = original_case.get(column.lower(), column)
            g.add((declared[dp_lower], _DCTERMS_NS.source, Literal(header)))
            recorded += 1
            if len(samples) < 6:
                samples.append(f"{cls}.{dp_lower} ← {header}")

    if recorded or ambiguous:
        logger.info(
            "Step 12d-3: dcterms:source 역기록 %d건 / 모호해서 보류 %d건. "
            "A-Box 는 이 값을 CSV 헤더와 직접 비교하므로 표기가 없으면 해당 "
            "컬럼이 이름 추측 폴백으로 넘어간다 (예: %s)",
            recorded, ambiguous, samples,
        )

    return StepResult(
        name="step_12d3_dp_source_backfill",
        stats={
            "dp_source_backfilled": recorded,
            "dp_source_backfill_ambiguous": ambiguous,
            "dp_source_backfill_rejected_loose_stage": rejected_loose,
            "dp_source_backfill_samples": samples,
        },
        triples_delta=len(g) - before,
        step_number="12d-3",
        step_label="dp_source_backfill",
    )
