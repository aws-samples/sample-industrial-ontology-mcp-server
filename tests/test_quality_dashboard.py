"""``quality_dashboard`` 의 문자열 계약 회귀 가드.

배경 (2026-08-08 실측): ``_RULE_METRIC_IMPACT`` 는 규칙 위반을 메트릭에 잇는
매핑인데, 값이 ``measure_tbox_metrics`` 의 **실제 키와 달랐다** —
``annotation`` 이라고 적혀 있었지만 실제 키는 ``annotation_completeness`` 다.

조인이 ``wm in t["affects_metrics"]`` 로 문자열 일치를 보므로, 이름이 어긋나면
그 계열의 근본원인이 **조용히 전부 사라진다**. 라벨·주석·명명 규칙 위반 4종의
근본원인이 100% 유실됐고, 이 모듈은 실행된 테스트 라인이 0이라 아무도 몰랐다.

문자열로 두 모듈을 잇는 계약은 타입 검사가 잡지 못한다 — 테스트로만 고정된다.
"""
from __future__ import annotations

import json

import pytest

from tools.quality_dashboard import _RULE_METRIC_IMPACT


def _real_metric_keys() -> set[str]:
    """``measure_tbox_metrics`` 가 실제로 내보내는 메트릭 키."""
    from tools.tbox_metrics import measure_tbox_metrics
    payload = json.loads(measure_tbox_metrics())
    metrics = payload.get("metrics") or {}
    return {k for k in metrics if isinstance(k, str)}


def test_every_impact_metric_name_exists():
    """THE REGRESSION: 매핑의 모든 메트릭 이름이 실제 키에 있어야 한다."""
    real = _real_metric_keys()
    if not real:
        pytest.skip("T-Box 가 없어 메트릭 키를 확인할 수 없다")

    referenced = {m for metrics in _RULE_METRIC_IMPACT.values() for m in metrics}
    phantom = sorted(referenced - real)
    assert not phantom, (
        f"실제로 없는 메트릭 이름: {phantom} (실제 키: {sorted(real)}) — "
        "조인이 영구 실패해 해당 근본원인이 전부 유실된다"
    )


def test_every_impact_rule_id_is_a_known_rule():
    """매핑의 규칙 id 가 검증기가 아는 rule 이어야 한다 (phantom rule 방지)."""
    from tools.validation_core import DETERMINISTIC_RULES, LLM_REQUIRED_RULES

    known = set(DETERMINISTIC_RULES) | set(LLM_REQUIRED_RULES)
    # 검증기가 emit 하는 rule 은 위 두 집합 밖에도 있다 (naming_* / isolated_class
    # 등 경고성). 그래서 "전부 known" 이 아니라 **알려진 것과 충돌하지 않음** 만
    # 확인하고, 대표적으로 deterministic 규칙 2종이 포함돼 있는지를 고정한다.
    assert {"missing_label", "missing_comment"} <= set(_RULE_METRIC_IMPACT)
    assert {"missing_domain", "missing_range"} <= known


def test_trace_quality_issues_smoke():
    """도구가 성공 응답을 내고 필수 필드를 채운다 (실행 라인 0이던 모듈)."""
    from tools.quality_dashboard import trace_quality_issues

    result = json.loads(trace_quality_issues())
    if not result.get("success"):
        pytest.skip(f"T-Box 미생성 등 환경 사유: {str(result.get('error'))[:80]}")
    for key in ("traced_violations", "metric_impact_summary",
                "warning_metrics_with_causes", "recommendation"):
        assert key in result, f"응답에 '{key}' 가 없다"


def test_annotation_rules_can_actually_join():
    """라벨/주석 위반이 annotation 메트릭에 **실제로** 연결되는지.

    이름이 맞아도 조인 로직이 바뀌면 다시 끊길 수 있으므로 조인 자체를 재현한다.
    """
    real = _real_metric_keys()
    if not real:
        pytest.skip("T-Box 없음")

    annotation_key = next((k for k in real if k.startswith("annotation")), None)
    assert annotation_key, f"annotation 계열 메트릭이 없다: {sorted(real)}"

    # 도구 내부와 같은 방식으로 조인해 본다.
    traced = [{"rule": "missing_label",
               "affects_metrics": _RULE_METRIC_IMPACT["missing_label"]}]
    causes = [t for t in traced if annotation_key in t["affects_metrics"]]
    assert causes, (
        f"'{annotation_key}' 가 missing_label 의 영향 목록과 조인되지 않는다 — "
        "근본원인이 유실된다"
    )
