"""Step 9c — DatatypeProperty range 정규화 (XSD 타입 **정확히 하나**).

HermiT 는 ``rdfs:range`` 가 반드시 XSD 타입이어야 한다. LLM 이 ``steel:Supplier``
같은 클래스를 DP 의 range 로 넣으면 추론기가 크래시한다. 비표준 range 를 일괄
``xsd:string`` 으로 정규화한다.

## 2026-08-19 실측 — 두 결함이 이 스텝을 통과했다

``equipmentStatusMaintenanceFlag`` 가 **두 번의 S2 실행에서 연속** SHACL 위반으로
남았고, S3 를 돌려도 고쳐지지 않았다 (S2 원본에서 S3 전체 재실행으로 확인):

======================= ========================================== =========
실행                     range 값                                    형태
======================= ========================================== =========
S2 (08-19 00:34)        ``xsd:boolean`` + 리터럴 ``"xsd:string"``    리터럴
S2 (08-19 06:46)        ``xsd:boolean`` + ``xsd:string``            다중 IRI
======================= ========================================== =========

원인은 예전 구현의 두 사각지대였다:

1. **리터럴을 아예 보지 않았다.** ``isinstance(range_val, URIRef)`` 안에서만
   교정했으므로 ``rdfs:range "xsd:string"`` (문자열 리터럴) 은 그대로 남았다.
   OWL 2 DL 위반이고 ``validate_tbox_shacl`` 이 잡지만, S3 가 못 고치니 사람이
   매번 손으로 지워야 했다.
2. **다중 선언을 정리하지 않았다.** 안전 prefix 값이 여러 개면 전부 ``continue``
   로 통과한다. SHACL shape 은 ``sh:maxCount 1`` 을 요구한다
   (``rules/policy/tbox_shapes.ttl``: "must have exactly one rdfs:range").

## 다중 range 에서 무엇을 남기는가

값을 임의로 고르지 않는다. ``xsd:string`` 은 이 스텝이 "모르겠으면 쓰는" 폴백
타입이다. 다른 XSD 타입과 함께 있으면 그 다른 타입이 프롬프트/LLM 이 **의도적으로**
고른 값이므로 그쪽을 남긴다 — 실측: ``Maintenance_Flag`` 의 CSV 값은 ``0``/``1`` 이고
프롬프트 ``04-property-rules.md`` 가 ``*flag → xsd:boolean`` 을 지시한다. boolean 이
정답이고 string 이 잉여였다.

폴백이 아닌 타입이 둘 이상이면 판정 근거가 없다. 그때는 **경고를 남기고 건드리지
않는다** — 조용히 하나를 고르면 데이터 타입이 뒤바뀌어 A-Box 적재가 깨지고, 그
원인을 아무도 추적할 수 없다.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: 이 스텝이 "모르겠을 때" 쓰는 폴백 range. 다중 선언에서 다른 XSD 타입과 함께
#: 있으면 그 다른 타입이 의도된 값이므로 폴백을 버린다.
_FALLBACK_RANGE = XSD.string

_SAFE_RANGE_PREFIXES = (
    str(XSD),
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
)

#: **OWL 2 datatype map 밖** 인 XSD 타입 → 대체할 타입.
#:
#: OWL 2 는 지원 데이터타입을 열거하고 (W3C OWL2 syntax §4 Datatype Maps) 그 밖의
#: 타입을 쓰면 HermiT 이 **온톨로지를 아예 열지 못한다**::
#:
#:     UnsupportedDatatypeException: The datatype 'xsd:date' is not part of the
#:     OWL 2 datatype map ... therefore, HermiT cannot handle this datatype.
#:
#: 그러면 S4·S9.1 의 일관성 검증이 **통째로 사라진다** — 이 리포에 같은 실패가
#: 기록돼 있다 ("검증기 실패 = 검증 없음": owl:imports 한 줄이 unsat 4건을 덮었다).
#:
#: ## 왜 ``step_08`` 이 이미 있는데도 필요한가 — 이 스텝이 되살렸다
#:
#: ``step_08_xsd_date_to_datetime`` 이 ``rdfs:range xsd:date`` → ``xsd:dateTime`` 을
#: 전역 치환한다. 다중 range 도 처리한다 (``date``+``dateTime`` → ``dateTime``, 집합
#: 의미론). 그런데 08 은 파이프라인 index 11, 이 스텝은 index 16 이고, 위
#: ``_recover_from_literal`` 이 리터럴 ``"xsd:date"`` 를 URIRef ``XSD.date`` 로
#: **복원한다**. 즉 08 이 지나간 **뒤에** 이 스텝이 비지원 타입을 만들어 내고,
#: 그것을 치울 스텝은 더 이상 없다. 2026-08-30 재현::
#:
#:     start          : rdfs:range "xsd:date"  (리터럴 — LLM 이 쓴 형태)
#:     after step_08  : "xsd:date"             (08 은 URIRef 만 매칭한다)
#:     after step_09c : xsd:date               ← 이 스텝이 되살렸다
#:
#: 그 결과 S2 산출 DP 4개(``purchaseOrderOrderDate`` 등)가 ``date`` 를 갖고 S4 에
#: 도달해 HermiT 이 온톨로지를 열지 못했다. 다중 range 정리가 "폴백 아닌 타입이 둘이라
#: 판정 근거가 없다" 며 ``date``+``dateTime`` 을 **둘 다 남긴** 것도 겹쳤다.
#:
#: 그래서 정보 복원(1)과 다중 range 판정(2) **사이** 에 비지원 타입을 대체한다.
#: 값은 잃지 않는다: ``xsd:date`` 값 ``"2025-09-01"`` 은 ``xsd:dateTime`` 으로 승격
#: 가능하고 (``_convert_date`` 가 이미 처리), 반대로 남겨두면 검증이 0건이 된다.
#:
#: 리터럴 복원 자체를 없애지 않는 이유: 그것은 ``"xsd:boolean"`` 처럼 LLM 이 고른
#: 타입을 살리는 정당한 동작이다 (회귀 테스트가 고정한다). 고쳐야 할 것은 "복원한
#: 타입이 OWL 2 map 안에 있는지 확인하지 않았다" 는 점이다.
_OWL2_UNSUPPORTED_RANGE_REPLACEMENT = {
    XSD.date: XSD.dateTime,
    XSD.duration: XSD.string,
    XSD.gYear: XSD.string,
    XSD.gMonth: XSD.string,
    XSD.gDay: XSD.string,
    XSD.gYearMonth: XSD.string,
    XSD.gMonthDay: XSD.string,
    XSD.QName: XSD.string,
    XSD.NOTATION: XSD.string,
    XSD.ENTITY: XSD.string,
    XSD.ID: XSD.string,
    XSD.IDREF: XSD.string,
}


def _recover_from_literal(lex: str):
    """리터럴 range 값에서 XSD 타입을 복원한다 (``"xsd:string"`` → ``xsd:string``).

    정보를 버리지 않는 것이 목적이다 — 폴백으로 뭉개면 LLM 이 고른 타입이 사라진다.
    """
    local = lex.strip().split(":", 1)[-1].split("#")[-1].split("/")[-1]
    return XSD[local] if local else _FALLBACK_RANGE


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns

    dp_range_fixed = 0
    dp_range_literal_fixed = 0
    dp_range_multi_pruned = 0
    dp_range_multi_ambiguous = 0
    dp_range_owl2_unsupported_fixed = 0
    ambiguous_samples: list[str] = []

    for prop in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if not str(prop).startswith(steel_str):
            continue
        prop_name = str(prop).split("#")[-1]

        # ── 1) 리터럴 / 비표준 range 를 XSD 타입으로 교정 ─────────────────
        for range_val in list(g.objects(prop, RDFS.range)):
            range_str = str(range_val)

            if isinstance(range_val, Literal):
                # 리터럴 range 는 **항상** 잘못이다 (range 는 IRI 여야 한다).
                recovered = _recover_from_literal(range_str)
                g.remove((prop, RDFS.range, range_val))
                g.add((prop, RDFS.range, recovered))
                logger.info(
                    "DatatypeProperty range 리터럴 교정: %s (%r → %s) — range 는 "
                    "IRI 여야 하고 리터럴은 OWL 2 DL 위반이다",
                    prop_name, range_str, str(recovered).split("#")[-1],
                )
                dp_range_literal_fixed += 1
                continue

            if any(range_str.startswith(ns) for ns in _SAFE_RANGE_PREFIXES):
                continue

            if isinstance(range_val, URIRef):
                g.remove((prop, RDFS.range, range_val))
                g.add((prop, RDFS.range, _FALLBACK_RANGE))
                range_name = (
                    range_str.split("#")[-1] if "#" in range_str else range_str
                )
                logger.info(
                    "DatatypeProperty range 교정: %s (%s → xsd:string)",
                    prop_name, range_name,
                )
                dp_range_fixed += 1

        # ── 1.5) OWL 2 비지원 타입 대체 (다중 range 판정 **전**) ──────────
        #
        # 여기서 하지 않으면 아래 2)가 ``date``/``dateTime`` 을 "의도된 타입 둘" 로
        # 보고 **둘 다 남긴다**. 그 결과 HermiT 이 온톨로지를 열지 못해 S4·S9.1
        # 검증이 통째로 사라진다 (모듈 상단 ``_OWL2_UNSUPPORTED_RANGE_REPLACEMENT``
        # 주석의 2026-08-30 실측).
        for range_val in list(g.objects(prop, RDFS.range)):
            replacement = _OWL2_UNSUPPORTED_RANGE_REPLACEMENT.get(range_val)
            if replacement is None:
                continue
            g.remove((prop, RDFS.range, range_val))
            g.add((prop, RDFS.range, replacement))
            dp_range_owl2_unsupported_fixed += 1
            logger.info(
                "DatatypeProperty range OWL 2 비지원 타입 교정: %s (%s → %s) — "
                "OWL 2 datatype map 밖이라 HermiT 이 온톨로지를 열지 못한다",
                prop_name, str(range_val).split("#")[-1],
                str(replacement).split("#")[-1],
            )

        # ── 2) 다중 range 정리 (SHACL: 정확히 하나) ───────────────────────
        ranges = list(g.objects(prop, RDFS.range))
        if len(ranges) <= 1:
            continue

        non_fallback = [r for r in ranges if r != _FALLBACK_RANGE]
        if len(non_fallback) == 1:
            keep = non_fallback[0]
            for r in ranges:
                if r != keep:
                    g.remove((prop, RDFS.range, r))
            logger.info(
                "DatatypeProperty range 다중 선언 정리: %s (%d개 → %s). "
                "xsd:string 은 이 스텝의 폴백이므로 의도된 타입을 남긴다",
                prop_name, len(ranges), str(keep).split("#")[-1],
            )
            dp_range_multi_pruned += 1
        else:
            # 판정 근거가 없다 — 조용히 고르면 타입이 뒤바뀌어 A-Box 적재가
            # 깨지고 원인 추적이 불가능해진다. 보고만 하고 남겨둔다.
            names = sorted(str(r).split("#")[-1] for r in ranges)
            logger.warning(
                "DatatypeProperty range 다중 선언 **판정 불가**: %s → %s. "
                "폴백(xsd:string) 이 아닌 타입이 %d개라 어느 쪽이 의도인지 알 수 "
                "없다. SHACL(maxCount 1) 위반으로 남으므로 CSV 실측값을 보고 "
                "사람이 정해야 한다",
                prop_name, names, len(non_fallback),
            )
            dp_range_multi_ambiguous += 1
            if len(ambiguous_samples) < 10:
                ambiguous_samples.append(f"{prop_name}: {names}")

    return StepResult(
        name="step_09c_dp_range_xsd",
        stats={
            "dp_range_fixed": dp_range_fixed,
            "dp_range_literal_fixed": dp_range_literal_fixed,
            "dp_range_multi_pruned": dp_range_multi_pruned,
            "dp_range_multi_ambiguous": dp_range_multi_ambiguous,
            "dp_range_multi_ambiguous_samples": ambiguous_samples,
            # OWL 2 map 밖 타입 교정 수. 0 이 아니면 S2 가 그 타입을 계속
            # 만들고 있다는 신호다 (프롬프트 축이 근본 원인).
            "dp_range_owl2_unsupported_fixed": dp_range_owl2_unsupported_fixed,
        },
        triples_delta=len(g) - before,
        step_number="9c",
        step_label="dp_range_xsd_fix",
    )
