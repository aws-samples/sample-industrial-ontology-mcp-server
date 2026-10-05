"""Step 21c — 익명 클래스 표현식에 잘못 붙은 ``owl:deprecated`` 잔재 제거.

## 왜 별도 스텝인가 (가드만으로는 안 되는 이유)

커밋 420b250 이 **생성 지점** 을 고쳤다: step_21 은 이제 ``named_domain_classes``
로 skolemize 된 ``Union_*`` / ``owl:Restriction`` 본체를 제외하므로 새로 플래그를
붙이지 않는다. 그런데 그것만으로는 **이미 파일에 박힌 플래그가 사라지지 않는다** —
step_21 이 익명 노드를 아예 보지 않게 됐으므로 자기가 과거에 붙인 것을 제거할
경로가 없어졌다. S3 스텝은 additive 라 자기수리하지 않는다.

실측 (2026-08-19, 커밋보다 1시간 19분 뒤에 생성된 배포 T-Box):

  $ check_quality_rules → critical 0 / high 1
  high | deprecated_reference | Deprecated entity
         Union_ManufacturingProcessStep_18a9fee9 is still referenced by:
         ManufacturingProcessStep

이 high 1건이 게이트를 **baseline FAIL** 로 고정시키고, ``mutation_runner._caught_by``
는 PASS→FAIL 강등만 검출로 세므로 이 체크로는 **어떤 mutant 도 잡히지 않았다**
(S4.5 검출률 14.3%, 생존한 M5 3건은 정확히 이 게이트의 담당 영역이었다).

``_rule_deprecated_reference`` 쪽에도 익명 표현식 필터를 넣었지만 (보고 억제),
**산출물에 남은 무의미한 트리플은 이 스텝이 지운다** — 게이트를 조용히 만드는 것과
데이터를 실제로 정리하는 것은 다른 일이고, 후자가 없으면 다른 소비자(시각화·
딕셔너리·외부 스토어)가 계속 그 플래그를 본다.

## 무엇을 지우는가 (좁게)

``owl:deprecated`` 와 step_21 이 함께 붙인 ``rdfs:seeAlso "over_engineered: …"``
표지만, 그리고 **익명 클래스 표현식일 때만** 제거한다. 판정은 공용 헬퍼
``is_anonymous_class_expression`` 하나에 위임한다 (사본 금지 — 이 리포는 같은
판정이 사본으로 갈라진 사고가 반복됐다).

**명명 클래스의 정당한 deprecated 는 절대 건드리지 않는다.** 배포 T-Box 실측으로
``HighStrengthProduct`` / ``QualityMeasurement`` 2건이 그 대상이며, 이 스텝이
그것을 지우면 step_21 의 over-engineered 마킹 기능 자체가 무력화된다.
회귀 테스트가 이 보존 방향을 고정한다 (파괴적 스텝은 "카운터 ≥ 1" 이 아니라
"정당한 입력을 보존하는가" 를 주장해야 한다).

## 배치

``_POST_STEPS`` 의 step_21b 직후. step_21(마킹) → 21b(dead stub) 가 끝난 뒤에
봐야 이번 실행이 붙인 것까지 포함해 판정이 확정된다. 게이트 성격 스텝(22d/22f)
보다 앞이어야 정리된 상태가 측정에 반영된다.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDFS, Graph, Literal

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: step_21 이 마킹과 함께 붙이는 표지. 이 접두로 시작하는 seeAlso 만 제거한다 —
#: 사람이 손으로 남긴 다른 seeAlso 를 지우지 않기 위한 보수 조건.
_SEEALSO_MARKER = "over_engineered:"


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    cleaned = 0
    preserved = 0
    error: str | None = None
    samples: list[str] = []
    try:
        from domain.graph_utils import is_anonymous_class_expression

        domain_ns = ctx.domain_ns
        # 순회 중 그래프를 변경하므로 대상을 먼저 확정한다.
        flagged = [
            s for s in set(g.subjects(OWL.deprecated, Literal(True)))
            if str(s).startswith(domain_ns)
        ]
        for node in sorted(flagged, key=str):
            if not is_anonymous_class_expression(g, node, domain_ns):
                preserved += 1
                continue
            g.remove((node, OWL.deprecated, Literal(True)))
            for obj in list(g.objects(node, RDFS.seeAlso)):
                if isinstance(obj, Literal) and str(obj).startswith(_SEEALSO_MARKER):
                    g.remove((node, RDFS.seeAlso, obj))
            cleaned += 1
            if len(samples) < 5:
                samples.append(str(node)[len(domain_ns):])
        if cleaned:
            logger.info(
                "익명 클래스 표현식의 deprecated 잔재 %d건 제거 (명명 클래스 %d건 "
                "보존): %s — 이 잔재가 check_quality_rules 를 baseline FAIL 로 "
                "고정시켜 mutation 검출을 막고 있었다",
                cleaned, preserved, ", ".join(samples),
            )
    except Exception as e:  # noqa: BLE001 — 정리 실패가 파이프라인을 막지 않는다
        logger.warning("익명 표현식 deprecated 정리 실패: %s", e)
        error = str(e)
    return StepResult(
        name="step_21c_anon_expr_deprecated_cleanup",
        stats={
            "anon_expr_deprecated_cleaned": cleaned,
            "named_deprecated_preserved": preserved,
        },
        triples_delta=len(g) - before,
        error=error,
        step_number="21c",
        step_label="anon_expr_deprecated_cleanup",
    )
