"""추론기가 명명 Restriction 을 보는가 — 눈멂 자기진단 (Fix 3).

2026-08-30 실측. 배포 T-Box 의 ``owl:Restriction`` **74개가 전부 명명 IRI** 이고
익명 bnode 는 0개다 (step_19 skolemize 결과). owlready2 는 IRI 노드를
``ThingClass`` 로 만들 뿐 ``owl:Restriction`` 을 클래스 표현식으로 재구성하지
않으므로 HermiT 에게는 "이름 있는 두 클래스가 동등" 으로만 보인다::

    :D owl:equivalentClass :R      ← onProperty/hasValue 제약이 사라진다

같은 모순을 익명 bnode 로 적으면 INCONSISTENT 가 나온다 (아래 probe 가 그것을
실증한다). 즉 S4/S9.1 의 ``consistent: true`` 는 정의 클래스 20개·존재 공리
54개·파티션 10건에 대해 **아무 증거가 아니다**. 예외도 경고도 없이 정상 응답을
주므로 "검증했다" 로 읽힌다.

이 리포에 같은 실패가 두 번 기록돼 있다 — "검증기 실패 = 검증 없음",
"onto_path 는 파일명으로 대입한다". 그 둘은 로드 실패 흔적이 있었지만 이번은
정상 응답을 주므로 더 은밀하다. 그래서 **수정 전에 측정 장치를 먼저** 만든다.

## 이 테스트의 방향

probe 자체가 게이트이므로 "probe 가 무엇을 주장하는가" 를 고정한다:

* **픽스처 유효성** — 두 TTL 이 의미상 동일한 모순이다 (익명 쪽이 실제로 잡힌다)
* **눈멂 검출** — 명명형에서 탐지 실패를 감지한다
* **영향 판정** — 이 T-Box 가 명명 Restriction 을 쓰는지 세어 결과에 반영한다
* **fail closed** — probe 가 아무것도 못 잡으면 blind 로 본다 (안전 쪽)
"""
from __future__ import annotations

import pathlib

import pytest

from tools import owl_reasoner

TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """probe 캐시를 비워 각 테스트가 실제로 측정하게 한다."""
    owl_reasoner._BLINDNESS_PROBE_CACHE.clear()
    yield
    owl_reasoner._BLINDNESS_PROBE_CACHE.clear()


# ── 픽스처 유효성 ───────────────────────────────────────────────────────


def test_probe_fixtures_are_semantically_identical():
    """두 픽스처의 차이는 Restriction 표기 하나뿐이다.

    다른 곳이 다르면 probe 가 눈멂이 아닌 것을 재게 된다.
    """
    named = owl_reasoner._PROBE_EXAMPLE_NAMED
    anon = owl_reasoner._PROBE_EXAMPLE_ANON

    assert owl_reasoner._PROBE_EXAMPLE_PREAMBLE in named
    assert owl_reasoner._PROBE_EXAMPLE_PREAMBLE in anon
    # 공통 전제는 글자 단위로 같다.
    assert named.split(owl_reasoner._PROBE_EXAMPLE_PREAMBLE)[0] == ""
    assert anon.split(owl_reasoner._PROBE_EXAMPLE_PREAMBLE)[0] == ""
    # 차이는 명명 vs 익명 표기.
    assert "a owl:Restriction ;" in named and ":R" in named
    assert "[ a owl:Restriction" in anon and ":R a owl:Restriction" not in anon


def test_anonymous_restriction_contradiction_is_detected():
    """픽스처가 실제로 모순인가 — 익명형은 반드시 탐지돼야 한다.

    이것이 실패하면 probe 는 눈멂을 재는 것이 아니라 깨진 픽스처를 재고 있다.
    """
    detected = owl_reasoner._probe_detects_contradiction(
        owl_reasoner._PROBE_EXAMPLE_ANON,
    )

    if detected is None:
        pytest.skip("추론기 실행 불가 (Java/JAVA_EXE)")
    assert detected is True, "익명 Restriction 모순이 탐지되지 않았다 — 픽스처 결함"


# ── 눈멂 검출 ───────────────────────────────────────────────────────────


def test_probe_reports_named_restriction_blindness():
    """THE MEASUREMENT: 명명형에서 탐지 실패를 감지한다.

    이 스택(owlready2 + HermiT)에서는 ``blind=True`` 가 현재 사실이다. 상류가
    고쳐지면 ``blind=False`` 가 되고, 그때 이 테스트는 아래 분기로 통과한다 —
    어느 쪽이든 **probe 가 두 형태를 구분한다** 는 것이 주장이다.
    """
    result = owl_reasoner.check_reasoner_blindness()

    if result["blind"] is None:
        pytest.skip("추론기 실행 불가")
    assert result["anon_restriction_detected"] is True
    if result["blind"]:
        assert result["named_restriction_detected"] is False
        assert "명명" in result["explanation"]
    else:
        assert result["named_restriction_detected"] is True


def test_probe_result_is_cached():
    """probe 1회에 HermiT 2회 — 반복 호출을 캐시가 막는다."""
    first = owl_reasoner.check_reasoner_blindness()
    assert owl_reasoner._BLINDNESS_PROBE_CACHE

    second = owl_reasoner.check_reasoner_blindness()
    assert second is first


def test_fail_closed_when_probe_detects_nothing(monkeypatch):
    """익명조차 못 잡으면 blind 로 본다 — 침묵을 PASS 로 읽지 않는다."""
    monkeypatch.setattr(
        owl_reasoner, "_probe_detects_contradiction", lambda _ttl: False,
    )

    result = owl_reasoner.check_reasoner_blindness()

    assert result["blind"] is True
    assert "fail closed" in result["explanation"]


def test_probe_failure_yields_none_not_false(monkeypatch):
    """실행 실패는 '눈멀지 않았다' 가 아니다 — None 으로 구분한다."""
    monkeypatch.setattr(
        owl_reasoner, "_probe_detects_contradiction", lambda _ttl: None,
    )

    result = owl_reasoner.check_reasoner_blindness()

    assert result["blind"] is None
    assert "판정 불가" in result["explanation"]


# ── 영향 판정: 이 T-Box 가 명명 Restriction 을 쓰는가 ────────────────────


def test_named_restriction_counter_sees_deployed_tbox():
    """배포 T-Box 의 명명 Restriction 을 센다 (익명은 세지 않는다)."""
    if not TBOX.exists():
        pytest.skip("T-Box 없음")

    named = owl_reasoner._count_named_restrictions(
        TBOX.read_text(encoding="utf-8"),
    )

    assert len(named) > 0, (
        "배포 T-Box 에 명명 Restriction 이 0개로 측정됐다 — "
        "카운터가 죽었거나 T-Box 세대가 바뀌었다"
    )


def test_counter_ignores_anonymous_restrictions():
    """익명 Restriction 은 세지 않는다 — 그것은 추론기가 정상 처리한다."""
    ttl = owl_reasoner._PROBE_EXAMPLE_ANON

    assert owl_reasoner._count_named_restrictions(ttl) == set()


def test_counter_counts_named_restrictions():
    ttl = owl_reasoner._PROBE_EXAMPLE_NAMED

    assert len(owl_reasoner._count_named_restrictions(ttl)) == 1


# ── 통합: validate_owl_consistency 응답에 실린다 ─────────────────────────


def test_consistency_response_carries_blindness_verdict():
    """``consistent: true`` 가 무엇의 증거인지 응답이 말한다.

    이 필드가 없으면 PASS 를 그 축의 검증으로 오독한다 — 그것이 이 결함의 본질이다.
    """
    import json

    if not TBOX.exists():
        pytest.skip("T-Box 없음")

    out = json.loads(
        owl_reasoner.validate_owl_consistency(ttl_path=TBOX.name),
    )
    if not out.get("success"):
        pytest.skip(f"추론기 실행 실패: {out.get('error')}")

    verdict = out.get("validator_blindness")
    assert verdict, "응답에 validator_blindness 가 없다"
    assert "named_restrictions_in_this_tbox" in verdict
    if verdict.get("blind") and verdict["named_restrictions_in_this_tbox"]:
        assert verdict["affects_this_result"] is True
        assert out.get("hint_validator_blind"), (
            "눈멂이 이 결과에 영향을 주는데 hint 가 없다 — 사람이 PASS 를 오독한다"
        )


# ── 익명화 (2026-08-30 수정) ─────────────────────────────────────────────


def test_anonymization_converts_named_restrictions():
    """변환 경계에서 명명 Restriction 을 익명 bnode 로 바꾼다."""
    from rdflib import OWL, RDF, BNode, Graph, URIRef

    g = Graph()
    g.parse(data=owl_reasoner._PROBE_EXAMPLE_NAMED, format="turtle")
    assert [s for s in g.subjects(RDF.type, OWL.Restriction) if isinstance(s, URIRef)]

    count = owl_reasoner._anonymize_named_restrictions(g)

    assert count == 1
    assert not [
        s for s in g.subjects(RDF.type, OWL.Restriction) if isinstance(s, URIRef)
    ]
    assert [s for s in g.subjects(RDF.type, OWL.Restriction) if isinstance(s, BNode)]


def test_anonymization_preserves_restriction_content():
    """제약 내용(onProperty/hasValue)이 보존되는가 — 빈 노드만 남으면 무의미하다."""
    from rdflib import OWL, RDF, Graph

    g = Graph()
    g.parse(data=owl_reasoner._PROBE_EXAMPLE_NAMED, format="turtle")
    owl_reasoner._anonymize_named_restrictions(g)

    nodes = list(g.subjects(RDF.type, OWL.Restriction))
    assert len(nodes) == 1
    assert list(g.objects(nodes[0], OWL.onProperty))
    assert list(g.objects(nodes[0], OWL.hasValue))


def test_anonymization_skips_nodes_also_typed_as_class():
    """``owl:Class`` 로도 타입된 노드는 이름으로 참조될 수 있어 건드리지 않는다."""
    from rdflib import OWL, RDF, Graph, URIRef

    g = Graph()
    node = URIRef("http://ex.org/R")
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, RDF.type, OWL.Class))

    assert owl_reasoner._anonymize_named_restrictions(g) == 0
    assert (node, RDF.type, OWL.Restriction) in g


def test_deployed_tbox_file_keeps_named_form():
    """**T-Box 파일은 바뀌지 않는다** — 익명화는 추론기 입력 사본에만 적용된다.

    원본을 바꾸면 클래스 열거 배제 헬퍼 23곳과 계층 지표가 동시에 흔들린다.
    """
    from rdflib import OWL, RDF, Graph, URIRef

    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    g = Graph()
    g.parse(str(TBOX), format="turtle")

    named = [
        s for s in g.subjects(RDF.type, OWL.Restriction) if isinstance(s, URIRef)
    ]

    assert named, (
        "T-Box 파일의 Restriction 이 익명화됐다 — 변환 경계에서만 바꿔야 한다"
    )


def test_env_switch_restores_previous_behaviour(monkeypatch):
    """``OWL_KEEP_NAMED_RESTRICTIONS=true`` 로 A/B 비교가 가능하다."""
    monkeypatch.setenv("OWL_KEEP_NAMED_RESTRICTIONS", "true")
    owl_reasoner._BLINDNESS_PROBE_CACHE.clear()

    result = owl_reasoner.check_reasoner_blindness()

    if result["blind"] is None:
        pytest.skip("추론기 실행 불가")
    assert result["blind"] is True, (
        "익명화를 껐는데 blind=False — probe 가 실제 경로를 재지 않는다"
    )
