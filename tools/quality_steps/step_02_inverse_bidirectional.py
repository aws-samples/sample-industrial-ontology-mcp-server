"""Step 2 — InverseOf 양방향 선언 (**설정 선언** 쌍 기반).

본문 ontology_quality.py 의 Step 2 블록을 그대로 모듈로 옮김.

``rules/domain/domain_config.json`` 의 ``inverse_pairs`` 에 선언된 ``(fwd, inv)`` 쌍에
대해, 어느 한쪽이 ``owl:ObjectProperty`` 로 존재하거나 inverseOf 트리플을 가지고
있으면 양방향 ``owl:inverseOf`` 트리플을 보장.

Step 0b (asymmetric inverseOf 자동 보강) 와의 차이: Step 0b 는 graph 에
**이미 있는** inverseOf 트리플을 대칭화. Step 2 는 **설정에 선언된** 쌍에 대해
OP 가 존재하면 강제 선언 — 보강 정책 (data-driven vs config-driven) 분리.

.. note::
   이 docstring 은 예전에 ``rules/inverse_pairs.json`` 을 소스라고 적었는데 그
   파일은 **리포에 존재하지 않았고 코드도 열지 않았다** — 실제로는
   ``_build_inverse_pairs`` 가 배포 T-Box(``data/generated/tbox/t_box.ttl``)를
   읽어 S3 가 자기 이전 출력에 의존했다. 2026-08-11 에 설정 기반으로 교체하고
   문구를 사실에 맞췄다 (근거는 ``ontology_quality._build_inverse_pairs``
   docstring 참조).

   현 철강 예시는 ``inverse_pairs`` 를 선언하지 않으므로 하드코딩 보장 2쌍만 남는다:
   ``(followedBy, precededBy)`` / ``(performsProcess, performedByEquipment)``.

   .. warning::
      이 자리에 예전에 "그 네 이름은 T-Box 에 없어 **확정적 no-op**" 이라고 적혀
      있었다. **그 전제는 깨졌다** — step_22 의 중복 OP 통합이 ``followedBy`` 를
      승자로 남기면서(경쟁자 ``directlyFollows``/``directlyPrecedes`` 제거) T-Box 에
      존재하게 됐고, 이 스텝이 발화한다. 2026-08-17 S3 재실행에서 실제로
      ``precededBy`` 를 **선언 없이** 참조해 S9 undeclared_op FAIL 을 만들었다.
      "no-op 이니 안전하다" 는 주석은 다른 스텝의 산출물이 바뀌면 무효가 된다.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import (
        DOMAIN_NS_OBJ,
        _add_if_missing,
        _build_inverse_pairs,
    )

    before = len(g)
    inverse_pairs = _build_inverse_pairs()
    inv_added = 0
    for fwd, inv in inverse_pairs:
        fwd_uri = DOMAIN_NS_OBJ[fwd]
        inv_uri = DOMAIN_NS_OBJ[inv]
        fwd_exists = (
            (fwd_uri, RDF.type, OWL.ObjectProperty) in g
            or list(g.triples((fwd_uri, OWL.inverseOf, None)))
        )
        inv_exists = (
            (inv_uri, RDF.type, OWL.ObjectProperty) in g
            or list(g.triples((inv_uri, OWL.inverseOf, None)))
        )
        if fwd_exists or inv_exists:
            inv_added += _add_if_missing(g, fwd_uri, OWL.inverseOf, inv_uri)
            inv_added += _add_if_missing(g, inv_uri, OWL.inverseOf, fwd_uri)
            # **참조하는 프로퍼티는 선언한다.**
            #
            # ``owl:inverseOf`` 만 넣고 ``owl:ObjectProperty`` 선언을 빼면 T-Box 가
            # 스스로 미선언 술어를 만든다 — ``validate_kg`` 의 undeclared_op check 가
            # 정확히 그것을 잡는다.
            #
            # 실측 (2026-08-17 S3 재실행): 이 스텝이 ``followedBy owl:inverseOf
            # precededBy`` 를 주입했지만 ``precededBy`` 를 선언하지 않아 S9 가
            # **미선언 OP 1건 (usage 3)** 으로 FAIL 했다. 위 docstring 이 "네 이름은
            # T-Box 에 없어 확정적 no-op" 이라고 적어둔 전제가 깨진 것이다 —
            # step_22 의 중복 OP 통합이 ``followedBy`` 를 승자로 남기면서 존재하게 됐다.
            #
            # domain/range 는 **넣지 않는다**: 짝의 것을 뒤집어 채우면 방향 오류를
            # 복제할 위험이 있다 (step_09b 가 같은 방식으로 오류를 전파한 전례).
            # step_09a(inverse_range_promotion) 가 근거를 갖고 채우도록 남긴다.
            for _uri in (fwd_uri, inv_uri):
                inv_added += _add_if_missing(g, _uri, RDF.type, OWL.ObjectProperty)

    # 방금 주입한 inverseOf 가 기존 TransitiveProperty 와 충돌할 수 있으므로 여기서
    # 다시 정리한다. Step 0d 가 같은 검사를 하지만 **이 단계보다 먼저** 실행되므로,
    # step 0d 시점에 inverse 가 없던 프로퍼티는 그물을 빠져나갔다.
    #
    # 결과가 비멱등이었다 (2026-08-08 실측): 1회차 출력은 ``followedBy`` 를
    # TransitiveProperty + inverseOf 로 함께 들고 나가고 (이 리포의 자체 검증
    # ``_rule_circular_property`` 가 **critical** 로 잡는 조합 — 실제로 출하 T-Box 가
    # 이 상태였다), 2회차에서야 정리됐다. 반대로 재실행 경로
    # (TBOX_INCREMENTAL / S4-FAIL 자동수정 루프) 에서는 ``followedBy`` 가 조용히
    # 전이성을 잃어 공정 흐름 다중 홉 추론이 끊긴다.
    #
    # 한 패스 안에서 수렴시키면 두 방향 모두 사라진다.
    from tools.quality_steps.step_00_antipatterns import (
        _strip_transitive_with_inverse,
    )
    late_stripped = _strip_transitive_with_inverse(g, ctx.domain_ns)

    return StepResult(
        name="step_02_inverse_bidirectional",
        stats={
            "inverse_triples_added": inv_added,
            # 0d 가 놓친 뒤늦은 충돌 (주입으로 새로 생긴 것). 0 이 정상이다.
            "late_transitive_stripped": late_stripped,
        },
        triples_delta=len(g) - before,
        step_number=2,
        step_label="InverseOf_bidirectional",
    )
