"""Import smoke test — 등록된 모든 quality_step 모듈이 실제로 실행 가능한지 검증.

회귀 방지: step 모듈은 `apply()` 안에서 `tools.ontology_quality` 의 헬퍼를
lazy import 한다. 그 심볼이 실제로 존재하지 않으면 `ImportError` 가 나는데,
`run_step_pipeline` 은 `RuntimeError` 만 재전파하고 그 외 `Exception` 은
logger.error 후 skip 하므로 파이프라인이 **조용히 no-op** 이 된다(크래시 없음).
그 결과 step 이 등록만 되고 실제로는 아무 일도 안 하는 버그가 로그에만 남고
묻힌다 (실제 발생: step_22c/23c 가 미정의 `_break_sub_class_cycles` /
`_remove_cross_domain_subproperties` 를 import 하던 이슈).

이 테스트는 등록된 모든 step 의 `apply` 를 **빈 그래프에 직접 호출**(파이프라인의
예외 안전망을 우회)해, lazy import 실패나 시그니처 불일치를 즉시 드러낸다.
"""
from __future__ import annotations

import pytest
from rdflib import Graph

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import (
    _MAIN_POST_STEP9,
    _MAIN_PRE_STEP9,
    _POST_STEPS,
    _PRE_STEPS,
    _STEP9_GROUP,
    StepContext,
    StepResult,
)

# 등록된 모든 step 함수 (실행 순서 그룹 전부)
_ALL_STEPS = [
    *_PRE_STEPS,
    *_MAIN_PRE_STEP9,
    *_STEP9_GROUP,
    *_MAIN_POST_STEP9,
    *_POST_STEPS,
]


def _step_id(fn) -> str:
    return getattr(fn, "__module__", repr(fn))


@pytest.mark.parametrize("step_fn", _ALL_STEPS, ids=_step_id)
def test_registered_step_applies_without_import_error(step_fn):
    """각 step 의 apply 를 빈 그래프에 직접 호출 — lazy import 심볼이 실존하고
    시그니처가 맞아야 한다. 빈 그래프이므로 대부분 no-op(0 delta)으로 끝나지만,
    ImportError / AttributeError / TypeError 는 여기서 바로 실패로 드러난다.

    (RuntimeError 는 step 12c coverage gate 처럼 의도적 contract 위반 신호이므로
    허용 — 빈 그래프에서 게이트가 fail 하는 것은 정상 동작이다.)
    """
    g = Graph()
    ctx = StepContext(domain_ns=str(DOMAIN_NS))
    try:
        result = step_fn(g, ctx)
    except RuntimeError:
        # 의도적 contract 위반 신호 (예: TBOX_COVERAGE_GATE) — import 문제 아님
        return
    assert isinstance(result, StepResult), (
        f"{_step_id(step_fn)}.apply 가 StepResult 를 반환하지 않음: {type(result)}"
    )


def test_no_registered_step_is_missing():
    """등록 리스트가 비어있지 않은지 (수집 자체가 깨지지 않았는지) 확인."""
    assert len(_ALL_STEPS) > 0
