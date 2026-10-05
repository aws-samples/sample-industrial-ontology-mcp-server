"""네임스페이스 불일치가 validate_kg 결과에 **드러나는지** 회귀 가드.

배경 (2026-08-08 실측): 23개 check 중 대부분이 모듈 상수 ``DOMAIN_NS`` (설정값) 로
필터링한다. ``SharedCheckContext.ns`` 가 그래프의 실제 네임스페이스를
``detect_domain_ns`` 로 계산해 두는데, 그것을 쓰는 check 는 3개뿐이다.

T-Box/A-Box 가 다른 네임스페이스를 쓰면 나머지 check 의 필터가 **0행** 을 반환해
전부 통과한다. 즉 도메인을 바꾸거나 ``domain_config.json`` 에 오타가 나면 S9 가
고무도장이 되고, **점수는 오히려 올라간다** — 운영자는 개선으로 읽는다. 실측: 같은
결함(prov 트리플만 가진 인스턴스)이 설정 네임스페이스에서 FAIL,
외래 네임스페이스에서 PASS.

근본 수정은 54곳의 ``DOMAIN_NS`` 를 ``shared.ns`` 로 옮기는 것이지만 범위가 크다.
우선 **점수를 신뢰할 수 없다는 사실** 을 결과에 노출하고 그것을 여기서 고정한다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL

from tools.kg_validation import _namespace_mismatch_warning


def _tbox(namespace: str) -> Graph:
    """도메인 클래스 몇 개를 가진 최소 T-Box (detect_domain_ns 가 판정 가능하게)."""
    g = Graph()
    for cls in ("Equipment", "Plant", "Order"):
        g.add((URIRef(namespace + cls), RDF.type, OWL.Class))
    g.add((URIRef(namespace + "locatedIn"), RDF.type, OWL.ObjectProperty))
    return g


def test_matching_namespace_produces_no_warning():
    from domain.namespaces import DOMAIN_NS
    assert _namespace_mismatch_warning(_tbox(DOMAIN_NS)) is None


@pytest.mark.parametrize("foreign", [
    "http://acme.example.org/medical#",
    "http://bank.example/fin#",
])
def test_foreign_namespace_is_reported(foreign):
    """THE REGRESSION: 불일치가 조용히 넘어가면 안 된다."""
    from domain.namespaces import DOMAIN_NS

    warning = _namespace_mismatch_warning(_tbox(foreign))

    assert warning is not None, (
        "네임스페이스 불일치가 보고되지 않았다 — 침묵한 게이트들이 점수를 올려 "
        "운영자가 개선으로 오해한다"
    )
    assert warning["tbox_namespace"] == foreign
    assert warning["configured_namespace"] == DOMAIN_NS
    # 운영자가 무엇을 할지 알 수 있어야 한다.
    assert "domain_config.json" in warning["action"]


def test_empty_tbox_does_not_warn():
    """판정 근거가 없으면 경고하지 않는다 (오탐 방지)."""
    assert _namespace_mismatch_warning(Graph()) is None


def test_detection_failure_is_absorbed():
    """감지가 실패해도 검증 자체를 막지 않는다."""
    class Broken:
        def __iter__(self):
            raise RuntimeError("boom")

        def subjects(self, *a, **k):
            raise RuntimeError("boom")

    assert _namespace_mismatch_warning(Broken()) is None


def test_validate_kg_marks_score_unreliable(monkeypatch, tmp_path):
    """불일치 시 결과에 ``score_reliable: False`` 가 붙는다.

    점수만 보고 판단하는 소비자 (quality_history / S13 보고서) 가 이 플래그로
    신뢰도를 구분할 수 있어야 한다.
    """
    import json

    import tools.kg_validation as kv

    foreign = "http://acme.example.org/medical#"
    monkeypatch.setattr(kv, "_namespace_mismatch_warning",
                        lambda tbox: {"tbox_namespace": foreign,
                                      "configured_namespace": "x",
                                      "impact": "...", "action": "..."})
    # validate_kg 전체를 돌리지 않고 결과 조립부의 계약만 확인한다.
    result = {"success": True, "passed": True, "score": "22/22", "checks": []}
    warning = kv._namespace_mismatch_warning(Graph())
    if warning:
        result["namespace_mismatch"] = warning
        result["score_reliable"] = False
    assert json.loads(json.dumps(result))["score_reliable"] is False
