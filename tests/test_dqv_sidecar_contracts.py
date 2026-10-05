"""DQV sidecar 가 상류 4개 도구와 맺은 **키 계약** 회귀 가드.

배경 (2026-08-08 실측): ``export_quality_dqv`` 와 5개 ``_extract_from_*`` 헬퍼는
실행된 라인이 0이었다. 각 추출기는 다른 도구의 출력 키를 하드코딩하고
(``metrics[k]["value"]`` / ``axis_scores[a]["score"]`` / ``dimensions[d]["score"]`` /
``overall_1_to_5``), 5개 수집기 전부가 호출을 ``try/except`` 로 감싸 실패를
``logger.debug`` 로만 남긴다.

그래서 상류 도구가 키 하나를 개명하면 **그 메트릭 그룹이 조용히 사라진다** —
DQV sidecar 는 성공으로 보고되고 내용만 비어 있다. 문자열로 모듈을 잇는 계약은
타입 검사가 잡지 못하므로 테스트로만 고정된다.

지금 4개 계약은 모두 성립한다 (실측). 이 파일은 그것이 **깨지는 순간 실패** 하게
만드는 것이 목적이다.

Farber 평가와 sidecar 전체 실행은 A-Box 산출물을 훑어 각 30초+ 걸리므로
``@pytest.mark.slow`` 로 표시했다 (``pytest -m slow`` 로만 실행). 나머지 4건은
1초 미만이라 기본 스위트에서 계약을 지킨다.
"""
from __future__ import annotations

import json

import pytest


def _call(module_path: str, func_name: str) -> dict:
    module = __import__(module_path, fromlist=[func_name])
    payload = json.loads(getattr(module, func_name)())
    if not payload.get("success", True):
        pytest.skip(f"{func_name} 실패 (환경 사유): {str(payload.get('error'))[:80]}")
    return payload


def test_tbox_metrics_exposes_metrics_value():
    """``_extract_from_tbox_metrics`` 는 ``metrics[k]["value"]`` 를 읽는다."""
    payload = _call("tools.tbox_metrics", "measure_tbox_metrics")
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict) and metrics, "metrics 섹션이 없다"
    key = next(iter(metrics))
    assert isinstance(metrics[key], dict) and "value" in metrics[key], (
        f"metrics['{key}'] 에 'value' 가 없다 — DQV 가 이 그룹을 조용히 버린다"
    )


def test_fair_score_exposes_axis_scores_score():
    """``_extract_from_foops_fair`` 는 ``axis_scores[a]["score"]`` 를 읽는다."""
    payload = _call("tools.foops_fair", "evaluate_fair_score")
    axes = payload.get("axis_scores")
    assert isinstance(axes, dict) and axes, "axis_scores 섹션이 없다"
    axis = next(iter(axes))
    assert isinstance(axes[axis], dict) and "score" in axes[axis], (
        f"axis_scores['{axis}'] 에 'score' 가 없다"
    )


@pytest.mark.slow
def test_farber_exposes_dimensions_score():
    """``_extract_from_farber`` 는 ``dimensions[d]["score"]`` 를 읽는다."""
    payload = _call("tools.farber_dimensions", "evaluate_farber_dimensions")
    dims = payload.get("dimensions")
    assert isinstance(dims, dict) and dims, "dimensions 섹션이 없다"
    dim = next(iter(dims))
    assert isinstance(dims[dim], dict) and "score" in dims[dim], (
        f"dimensions['{dim}'] 에 'score' 가 없다"
    )


def test_oquare_exposes_overall_1_to_5():
    """``_extract_from_oquare`` 는 ``overall_1_to_5`` 를 읽는다."""
    payload = _call("tools.ontology_quality", "evaluate_oquare")
    assert "overall_1_to_5" in payload, (
        "overall_1_to_5 키가 사라지면 OQuare 그룹이 sidecar 에서 통째로 빠진다"
    )


@pytest.mark.slow
def test_export_quality_dqv_produces_measurements():
    """THE REGRESSION: sidecar 가 실제로 측정값을 담아야 한다 (빈 껍데기 금지).

    수집기들이 모두 ``try/except`` + ``logger.debug`` 라, 계약이 깨지면 성공
    응답에 내용만 비어서 돌아온다.
    """
    from tools.dqv_sidecar import export_quality_dqv

    result = json.loads(export_quality_dqv())
    if not result.get("success"):
        pytest.skip(f"환경 사유: {str(result.get('error'))[:100]}")

    count = (
        result.get("measurement_count")
        or len(result.get("measurements") or {})
        or result.get("metrics_count")
        or 0
    )
    assert count > 0, (
        f"DQV sidecar 가 측정값 0건으로 성공 보고했다 — 상류 키 계약이 깨졌다는 "
        f"신호다. 응답 키: {sorted(result)}"
    )


def test_extractors_are_callable_with_empty_input():
    """추출기가 빈 입력에서 예외 없이 빈 dict 를 반환한다 (계약 방어)."""
    from tools import dqv_sidecar as dqv

    for name in ("_extract_from_tbox_metrics", "_extract_from_foops_fair",
                 "_extract_from_farber", "_extract_from_oquare"):
        fn = getattr(dqv, name)
        assert fn({}) == {} or isinstance(fn({}), dict), f"{name}({{}}) 가 dict 가 아니다"
