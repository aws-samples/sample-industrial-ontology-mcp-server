"""Step 02b — 파일에 박힌 **구 IOF 네임스페이스** IRI 를 정본으로 마이그레이션.

## 왜 필요한가

IOF 는 Core / Maintenance / SupplyChain 을 **하나의 네임스페이스**
(``/ontology/construct/``) 에 선언한다. 리포 코드는 모듈별로 나뉜 IRI
(``/ontology/core/Core/`` 등) 를 쓰고 있었고, 그것으로는 T-Box 가 참조하는 IOF
용어 24개 중 **3개만** 해소됐다 (그 3개도 온톨로지 헤더 IRI). 즉 ``owl:imports``
3건이 한 트리플도 로드하지 못했고, HermiT 의 침묵은 "일관됨" 이 아니라 **공리가
없어서 얻은 침묵** 이었다.

``domain/namespaces.py`` 상수를 고쳤지만 그것만으로는 **앞으로 만들 IRI** 에만
적용된다 — 실측 (2026-08-18): 상수 교정 + S3 재실행 후에도 해소율이 12% → 14% 로
거의 그대로였다. 배포 T-Box 에 구 IRI 문자열이 47건 박혀 있었기 때문이다.

이번 세션에 반복 확인한 패턴이다 (``project_guard_blocks_only_new_writes``):
**생성 지점 수정 + 정리 스텝 두 벌**이 필요하다.

## 왜 안전한가

같은 local name 이 같은 개념을 뜻한다 — 세 모듈 IRI 는 애초에 실재하지 않으므로
"다른 개념을 합치는" 위험이 없다. 교정 후 IOF 병합 상태로 HermiT 를 돌려
**consistent=true / unsat 0** 을 확인했다 (``step_12`` 주석이 예고한
``SupplierMaster`` unsatisfiable 은 나타나지 않았다).

BFO 범주 혼합은 별도 문제였고 ``rules/domain/design_patterns.json`` 의 ``iof_parent``
교정으로 12개 → 2개로 줄였다. 남은 2개(``ProductionResult`` /
``Transportation``) 는 S2 LLM 이 만든 직접 매핑과 추상 부모의 분류가 충돌한 것으로,
CSV 상 두 테이블은 프로세스가 아니라 **기록** 이다.
"""
from __future__ import annotations

import logging

from rdflib import Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: 실재하지 않는 구 모듈별 IOF 네임스페이스. 값은 ``domain.namespaces`` 의 정본
#: 으로 치환된다. 도메인-중립: 이 표는 IOF(외래 표준) 전용이고 자사 도메인
#: 네임스페이스와 무관하다.
_LEGACY_IOF_PREFIXES = (
    "https://spec.industrialontologies.org/ontology/core/Core/",
    "https://spec.industrialontologies.org/ontology/maintenance/Maintenance/",
    "https://spec.industrialontologies.org/ontology/supplychain/SupplyChain/",
)


def _migrate(g: Graph) -> dict:
    from domain.namespaces import IOF_CORE

    def _fix(node):
        """구 IOF IRI 면 정본으로 바꾼 노드를, 아니면 None 을 돌려준다."""
        if not isinstance(node, URIRef):
            return None
        s = str(node)
        for legacy in _LEGACY_IOF_PREFIXES:
            if s.startswith(legacy):
                local = s[len(legacy):]
                # 온톨로지 헤더 IRI (local 이 빈 문자열) 는 건드리지 않는다 —
                # ``owl:imports`` 대상이고, 정본으로 바꾸면 세 imports 가 한
                # IRI 로 합쳐져 무엇을 불러오려 했는지 이력이 사라진다.
                if not local:
                    return None
                return URIRef(IOF_CORE + local)
        return None

    migrated = 0
    header_left = 0
    for s, p, o in list(g):
        ns_s, ns_p, ns_o = _fix(s), _fix(p), _fix(o)
        if ns_s is None and ns_p is None and ns_o is None:
            continue
        g.remove((s, p, o))
        g.add((ns_s or s, ns_p or p, ns_o or o))
        migrated += 1

    for node in set(g.subjects()) | set(g.objects()):
        if isinstance(node, URIRef) and any(
            str(node) == legacy for legacy in _LEGACY_IOF_PREFIXES
        ):
            header_left += 1

    # 리터럴에 IRI 를 담은 경우도 있다 (dcterms:source 등에 문자열로 박힌 표기).
    literal_fixed = 0
    for s, p, o in list(g):
        if not isinstance(o, Literal):
            continue
        val = str(o)
        new_val = val
        for legacy in _LEGACY_IOF_PREFIXES:
            if legacy in new_val:
                new_val = new_val.replace(legacy, IOF_CORE)
        if new_val != val:
            g.remove((s, p, o))
            g.add((s, p, Literal(new_val, datatype=o.datatype, lang=o.language)))
            literal_fixed += 1

    if migrated or literal_fixed:
        logger.info(
            "Step 2b: 구 IOF 네임스페이스 IRI %d건 + 리터럴 %d건 정본으로 "
            "마이그레이션 (%s). 모듈별 IRI 는 실재하지 않아 참조가 공리 없는 "
            "고립 리프였다 — 교정 후 IOF 병합 시 실제 상위 공리를 얻는다.",
            migrated, literal_fixed, IOF_CORE,
        )
    if header_left:
        logger.info(
            "Step 2b: 온톨로지 헤더 IRI %d건은 보존 (owl:imports 대상 — 정본으로 "
            "합치면 무엇을 불러오려 했는지 이력이 사라진다)", header_left,
        )
    return {
        "iof_legacy_iris_migrated": migrated,
        "iof_legacy_literals_migrated": literal_fixed,
        "iof_ontology_headers_preserved": header_left,
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats = _migrate(g)
    return StepResult(
        name="step_02b_iof_namespace_migrate",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="2b",
        step_label="iof_namespace_migration",
    )
