"""Step 3 — ObjectProperty 누락 domain 보완 (config-driven).

본문 ontology_quality.py 의 Step 3 블록을 그대로 모듈로 옮김.

``_build_domain_fixes`` (T-Box 실측 매핑 + ``rules/domain/domain_config.json`` 의
``op_domain_seeds``) 를 사용해, OP 가 ObjectProperty 로 선언돼 있지만
``rdfs:domain`` 이 없으면 사전 정의된 도메인을 부여한다.

**존재 가드**: 대상 클래스가 T-Box 에 선언돼 있지 않으면 아무것도 하지 않는다.
없는 클래스를 domain 으로 박으면 step_09d (phantom class fix) 가 그것을 같은
prefix 의 이름이 비슷한 다른 클래스로 "교정" 해, inverse 에서 유도될 정답
domain 을 덮어쓴다 (2026-08-08 규명 — owlrl ``prp-dom`` 을 거쳐 HermiT
inconsistency 까지 전파됐다). 잘못 채우는 것보다 비워 두는 것이 안전하다.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import DOMAIN_NS_OBJ, _build_domain_fixes

    before = len(g)
    domain_fixes = _build_domain_fixes()
    domain_added = 0
    skipped_missing_class: list[str] = []
    for prop_name, dom_name in domain_fixes.items():
        uri = DOMAIN_NS_OBJ[prop_name]
        if (uri, RDF.type, OWL.ObjectProperty) not in g:
            continue
        if list(g.triples((uri, RDFS.domain, None))):
            continue
        domain_uri = DOMAIN_NS_OBJ[dom_name]
        if (domain_uri, RDF.type, OWL.Class) not in g:
            # 없는 클래스를 domain 으로 선언하면 step_09d 가 엉뚱한 클래스로
            # 교정한다 (docstring 참조). no-op 이 정답.
            skipped_missing_class.append(f"{prop_name}->{dom_name}")
            continue
        g.add((uri, RDFS.domain, domain_uri))
        domain_added += 1
    if skipped_missing_class:
        logger.info(
            "Step 3: domain 클래스가 T-Box 에 없어 건너뜀 %d건 (오답 대신 미선언 유지): %s",
            len(skipped_missing_class), skipped_missing_class[:5],
        )
    return StepResult(
        name="step_03_op_domain_default",
        stats={
            "domain_fixes": domain_added,
            "skipped_missing_class": skipped_missing_class[:20],
        },
        triples_delta=len(g) - before,
        step_number=3,
        step_label="OP_domain_fixes",
    )
