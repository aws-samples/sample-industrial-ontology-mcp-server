"""relationship-outlier 게이트가 **자기 마스킹** 하지 않는지 회귀 가드.

배경 (2026-08-08 실측): 간선 수가 거의 일정해 ``iqr == 0`` 이면 임계치를
``mean + 3*stdev`` 로 폴백했다. mean/stdev 는 오염에 **견고하지 않은** 추정량이라
이상치가 자기를 잡을 임계치를 스스로 끌어올린다 — 오염이 심할수록 확실히 통과한다:

    hub  5/100 -> 검출 5,  임계치  7,071
    hub  9/100 -> 검출 9,  임계치  9,529
    hub 10/100 -> 검출 0,  임계치 10,045   ← 임계치가 이상치 값을 추월
    hub 20/100 -> 검출 0,  임계치 14,060

즉 "이상치가 많을수록 못 잡는" 게이트였다. 실제 A-Box (737,414 트리플) 에서도
수정 전 0건 → 수정 후 120건이다.

수정: median + MAD (50% 까지 견고) 로 폴백한다. MAD 도 0 이면 값이 정말 상수이므로
중앙값 초과 자체가 이상이다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL

from tools.validation_support.checks.statistical import check_relationship_outliers

STEEL = "http://example.com/steel-ontology#"


def _graph(edge_counts: list[int]) -> Graph:
    """주체별 간선 수를 그대로 재현하는 그래프."""
    g = Graph()
    op = URIRef(STEEL + "linksTo")
    g.add((op, RDF.type, OWL.ObjectProperty))
    for idx, count in enumerate(edge_counts):
        subject = URIRef(STEEL + f"S{idx}")
        for j in range(count):
            g.add((subject, op, URIRef(STEEL + f"T{idx}_{j}")))
    return g


@pytest.mark.parametrize("hubs", [5, 9, 10, 15, 20])
def test_detection_does_not_collapse_as_contamination_grows(hubs):
    """THE REGRESSION: 오염 비율이 올라도 검출 수가 0으로 무너지지 않는다.

    수정 전에는 10% 를 넘는 순간 임계치가 이상치를 추월해 0건이 됐다.
    """
    counts = [1] * (100 - hubs) + [60] * hubs
    result = check_relationship_outliers(_graph(counts))

    assert result["outlier_count"] >= hubs, (
        f"hub {hubs}/100 에서 {result['outlier_count']}건만 검출 — 오염된 추정량이 "
        "임계치를 끌어올려 이상치를 스스로 가렸다"
    )


def test_uniform_counts_produce_no_outliers():
    """오탐 방지: 모든 주체가 같은 간선 수면 이상치가 없다."""
    result = check_relationship_outliers(_graph([3] * 50))
    assert result["outlier_count"] == 0
    assert result["passed"] is True


def test_natural_variation_produces_no_outliers():
    """2~4개 수준의 자연 변동은 이상치가 아니다."""
    counts = [2 + (i % 3) for i in range(60)]
    result = check_relationship_outliers(_graph(counts))
    assert result["outlier_count"] == 0, f"오탐: {result.get('outliers')[:3]}"


def test_single_extreme_hub_is_caught():
    """전형적 케이스: 하나의 극단 hub 는 검출된다 (기능 보존)."""
    counts = [2] * 60 + [80]
    result = check_relationship_outliers(_graph(counts))
    assert result["outlier_count"] == 1
    assert result["outliers"][0]["count"] == 80


def test_iqr_path_still_used_when_spread_exists():
    """분산이 있으면 기존 IQR 경로를 그대로 쓴다 (폴백은 iqr==0 전용)."""
    counts = list(range(1, 41)) + [400]      # 넓은 분포 + 극단값 1개
    result = check_relationship_outliers(_graph(counts))
    assert result["outlier_count"] >= 1
    assert result["outliers"][0]["count"] == 400


def test_reported_threshold_is_below_the_outlier():
    """보고된 임계치가 이상치 값보다 낮아야 한다 (자기모순 방지)."""
    counts = [1] * 80 + [50] * 20
    result = check_relationship_outliers(_graph(counts))
    for outlier in result["outliers"][:5]:
        assert outlier["threshold"] < outlier["count"], (
            f"임계치({outlier['threshold']}) 가 이상치({outlier['count']}) 이상이다 — "
            "게이트가 자기모순 상태로 보고한다"
        )
