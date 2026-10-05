"""Step 22d — 중복 ObjectProperty 게이트 (read-only 측정).

T-Box 생성 프롬프트 (``prompts/tbox-prompt-modules/04-property-rules.md`` 원칙 6)
는 같은 ``(rdfs:domain, rdfs:range)`` 쌍에 관계를 중복 생성하지 말도록 요구한다.
프롬프트는 LLM 에 대한 요청이므로 지켜지지 않을 수 있고, 실제로 지켜지지 않았다.

**실측 (2026-07-26)**: ``domain=MaterialA, range=ProcessStepA`` 인 OP 가 **7개** 생성됐다.
주석을 읽으면 넷 이상이 같은 사실을 관점만 달리해 서술한다 (중간재 실적 /
1차 공정 실적 / 중간 소재 생산 / 1차 공정 생산). CSV 에는 FK 컬럼(``KEY_COL_4``)이 하나뿐
이므로 A-Box 는 그중 **하나만** 채우고 나머지는 값 0건으로 남는다.

왜 조용히 위험한가:

1. **빈 관계로 질의하면 0건이 정답처럼 반환된다** — 오류가 아니라 빈 결과다.
2. **재생성마다 어느 이름이 채워질지 바뀐다** — Step 22 가 canonical 을 A-Box
   사용량으로 고르고, A-Box 는 T-Box 직렬화 순서에 좌우됐다 (실측: ``isSlabOf``
   13,222건 → 0건, ``isSlabProducedByBofHeat`` 0 → 13,222건). 저장해 둔 SPARQL·
   골든 쿼리가 한꺼번에 깨진다. 생성기 측은 이름 오름차순으로 고정해 수정했다
   (``_candidate_sort_key``) 지만, **중복 자체는 T-Box 생성 단계에서만 없앨 수
   있다.**

이 단계는 그래프를 수정하지 않고 (Step 22 가 이미 subPropertyOf 로 묶는다) 다음을
측정해 프롬프트 회귀를 드러낸다:

  - ``duplicate_groups``: 2개 이상 OP 가 공유하는 (domain, range) 쌍 수
  - ``redundant_ops``: 그 그룹들의 초과 OP 수 (그룹당 개수 - 1 의 합)
  - ``worst_groups``: 가장 심한 그룹 표본 (프롬프트 개선 근거)
  - ``opposing_groups``: 방향 대립(origin↔destination)으로 **정당하게** 여럿인 그룹.
    중복 집계에서 제외한다.

## 이 스텝은 22e **뒤** 에 있어야 한다 (2026-08-30 규명)

이전에는 ``step_22e_duplicate_op_prune`` **앞** 이었다. 22e 는 중복 OP 를
**삭제**하므로 (``.env`` 의 ``TBOX_DUP_OP_PRUNE=on``), 이 게이트가 세던 것은
**배포되지 않는 중간 상태** 였다. 08-19 산출물 실측:

======================= ============ ==============
시점                     중복 그룹      중복 OP
======================= ============ ==============
22d (측정, 22e 앞)        31           **35**
22e 후 (실제 배포본)       0            **0**
======================= ============ ==============

``fail`` 모드였다면 **한 줄 뒤에 지워질 중복** 때문에 S3 가 ``RuntimeError`` 로
중단됐다. ``step_22f`` 에서 같은 결함을 고쳤는데 판정 로직을 그 모듈에 **사본**으로
둔 탓에 이 스텝의 같은 문제를 놓쳤다 — 그래서 판정기를 ``_base.is_registered_after``
로 공용화했다. ``duplicate_op_measured_after_prune`` 을 stats 에 남겨, 뒤에 OP 를
지우는 스텝이 또 들어오면 드러나게 한다.

(이번 세대 T-Box 는 22d/22e 모두 0건이라 현재 실손실은 없다. 위치 자체가 틀렸으므로
고친 것이고, 다음 S2 가 중복을 다시 만들면 재발한다.)

환경변수:
  - ``TBOX_DUP_OP_GATE``: ``warn`` (default) | ``fail``
  - ``TBOX_DUP_OP_MAX_REDUNDANT``: 허용 초과 OP 수 (default 0 — 즉 중복 없어야 함)

``fail`` 모드에서 초과 시 ``RuntimeError`` 를 전파한다. 기본이 ``warn`` 인 이유는
기존 T-Box 가 이미 중복을 갖고 있어 즉시 ``fail`` 로 두면 파이프라인이 막히기
때문이다 — 중복을 정리한 뒤 ``fail`` 로 올려 회귀를 봉쇄하는 것을 권장한다.
"""
from __future__ import annotations

import collections
import logging
import os

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import (
    OP_REMOVING_STEPS,
    StepContext,
    StepResult,
    is_registered_after,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_REDUNDANT = 0


def _local(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _measure(g: Graph, domain_ns: str) -> dict:
    from tools.ontology_quality import (
        _directional_tokens_in_labels,
        _has_opposing_directions,
    )

    buckets: dict[tuple[str, str], list[URIRef]] = collections.defaultdict(list)
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(domain_ns)):
            continue
        domains = sorted(
            _local(str(d)) for d in g.objects(op, RDFS.domain)
            if isinstance(d, URIRef) and str(d).startswith(domain_ns)
        )
        ranges = sorted(
            _local(str(r)) for r in g.objects(op, RDFS.range)
            if isinstance(r, URIRef) and str(r).startswith(domain_ns)
        )
        if len(domains) != 1 or len(ranges) != 1:
            # 다중/누락 domain·range 는 별도 스텝의 관심사 (step_03/09 계열).
            continue
        buckets[(domains[0], ranges[0])].append(op)

    duplicate_groups = 0
    redundant_ops = 0
    opposing_groups = 0
    detail: list[dict] = []
    for (dom, rng), ops in buckets.items():
        if len(ops) < 2:
            continue
        token_sets = [_directional_tokens_in_labels(g, op) for op in ops]
        if _has_opposing_directions(token_sets):
            # origin ↔ destination 처럼 의미가 실제로 갈리는 경우는 중복이 아니다.
            opposing_groups += 1
            continue
        duplicate_groups += 1
        redundant_ops += len(ops) - 1
        detail.append({
            "domain": dom,
            "range": rng,
            "count": len(ops),
            "properties": sorted(_local(str(o)) for o in ops),
        })

    detail.sort(key=lambda d: (-d["count"], d["domain"], d["range"]))
    return {
        "op_pairs_examined": len(buckets),
        "duplicate_groups": duplicate_groups,
        "redundant_ops": redundant_ops,
        "opposing_groups": opposing_groups,
        "worst_groups": detail[:10],
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """Measure duplicate (domain, range) OP groups; warn or fail per policy."""
    before = len(g)
    mode = (os.getenv("TBOX_DUP_OP_GATE") or "warn").strip().lower()
    try:
        max_redundant = int(
            os.getenv("TBOX_DUP_OP_MAX_REDUNDANT") or _DEFAULT_MAX_REDUNDANT,
        )
    except ValueError:
        max_redundant = _DEFAULT_MAX_REDUNDANT

    stats: dict = {}
    error: str | None = None
    try:
        stats = _measure(g, ctx.domain_ns)
    except Exception as exc:  # noqa: BLE001 — 측정 실패가 S3 를 막아선 안 된다
        logger.warning("Step 22d 중복 OP 측정 실패 (무시): %s", exc)
        error = str(exc)
        stats = {"error": str(exc)}

    # 이 게이트가 **배포되는 그래프** 를 재고 있는가. 측정 성공 여부와 무관한
    # **배선** 사실이므로 오류 경로에도 넣는다 — 한쪽에만 있으면 소비자가 키 부재를
    # "정상" 으로 오독한다 (22f 에서 같은 함정을 겪었다: 필드 부재 ≠ 값 0).
    measured_after_prune = is_registered_after(
        "step_22d_duplicate_op_gate", *OP_REMOVING_STEPS,
    )
    stats["duplicate_op_measured_after_prune"] = measured_after_prune
    if not measured_after_prune:
        logger.warning(
            "Step 22d: 이 게이트 **뒤** 에 OP 를 삭제하는 스텝이 있다 — 지금 세는 "
            "중복 수치는 배포되는 T-Box 가 아니라 중간 상태다. 2026-08-30 실측: "
            "22e 앞에서 그룹 31/중복 35 를 보고했으나 배포본은 0/0 이었고, "
            "TBOX_DUP_OP_GATE=fail 이면 한 줄 뒤에 지워질 중복 때문에 S3 가 중단된다. "
            "등록 순서를 22e 뒤로 되돌려라 (tools/quality_steps/__init__.py).",
        )

    if not error:
        stats["gate_mode"] = mode
        stats["gate_max_redundant"] = max_redundant
        passed = stats["redundant_ops"] <= max_redundant
        stats["gate_passed"] = passed
        if not passed:
            worst = stats["worst_groups"][0] if stats["worst_groups"] else {}
            message = (
                f"같은 (domain, range) 쌍에 중복 ObjectProperty {stats['redundant_ops']}개 "
                f"(허용 {max_redundant}) — 그룹 {stats['duplicate_groups']}개. "
                f"최다: ({worst.get('domain')}, {worst.get('range')}) "
                f"{worst.get('count')}개 {worst.get('properties')}. "
                "CSV FK 컬럼은 하나뿐이라 A-Box 는 그중 하나만 채우고 나머지는 "
                "값 0건으로 남는다 — 빈 관계로 질의하면 0건이 정답처럼 반환된다. "
                "prompts/tbox-prompt-modules/04-property-rules.md 원칙 6 참조."
            )
            if mode == "fail":
                logger.error("Step 22d duplicate OP gate FAIL: %s", message)
                raise RuntimeError(message)
            logger.warning("Step 22d duplicate OP gate WARN: %s", message)

    return StepResult(
        name="step_22d_duplicate_op_gate",
        stats=stats,
        triples_delta=len(g) - before,   # 항상 0 — read-only
        step_number=22,
        step_label="duplicate_op_gate",
    )
