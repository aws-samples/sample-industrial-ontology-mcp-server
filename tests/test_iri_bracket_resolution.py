"""Turtle IRI 표기 ``<...>`` 가 해석기에서 폐기되지 않는다.

2026-08-19 실측 피해: LLM 은 DSL 값에 Turtle 관용을 그대로 쓴다
(``"on_property": "<https://…/Core/followedBy>"``). 두 해석기가 브래킷을 몰라
각자 다르게 망가졌다:

- ``resolve_entity_name`` — ``<https`` 를 **미등록 prefix** 로 읽고 콜론 앞을 버려
  ``<DOMAIN_NS>//spec…/followedBy>`` 를 만들었다. rdflib 이 ``Invalid IRI code
  point '>'`` 로 거부 → **지시가 폐기됐다** (서버 로그 누적 136건).
- ``parse_object_term`` — 완전 IRI 판정에 걸리지 않아 조용히 **평문 Literal** 이
  됐다. 경고조차 없어 삭제·교체 매칭이 영구 실패했다.

피해 규모: 한 S2 실행의 R4 에서 버려진 Jury 지시 7건 중 6건이 이것이고, 그중 4건이
SME 가 4라운드 반복 요구한 **공정 흐름 체인** (고로→제강→연주→압연의 followedBy /
precededBy someValuesFrom) 이었다. 배포 T-Box 에 ``steel:followedBy`` 는 선언만
남고 ``owl:onProperty`` 사용 0건 — 리뷰어가 매 라운드 같은 결함을 다시 지적해
veto 가 풀리지 않았다.

이 파일의 주장은 **양방향** 이다 (프로젝트 규칙: 파괴적/해석 변경은 "카운터가
올랐는가" 가 아니라 "정당한 입력이 보존되는가" 를 주장해야 한다):

1. POSITIVE — 브래킷 IRI 가 올바른 IRI 로 해석된다.
2. NEGATIVE — 브래킷 **없는** 기존 6개 해석 경로가 한 글자도 바뀌지 않는다.
3. NEGATIVE — 리터럴로 의도된 값이 IRI 로 승격되지 않는다 (``"< 5mm"``, ``<a b>``).

원래 버그를 되살리면 실패해야 한다: ``resolve_entity_name`` 의
``strip_iri_brackets`` 호출을 지우면 1번이 깨지고, ``strip_iri_brackets`` 의
공백/꺾쇠 검사를 없애면 3번이 깨진다.
"""
from __future__ import annotations

import pytest
from rdflib import Literal, URIRef

from domain.graph_utils import (
    parse_object_term,
    resolve_entity_name,
    strip_iri_brackets,
)
from domain.namespaces import DOMAIN_NS, FOREIGN_PREFIXES, IOF_CORE, NS_PREFIX

#: 실측 폐기 사례 — R4 에서 버려진 공정 흐름 지시의 on_property 값.
IOF_FOLLOWED_BY = IOF_CORE.rstrip("/") + "/followedBy"


# ──────────────────────────────────────────────────────────────────
# 1. POSITIVE — 브래킷 IRI 가 살아난다
# ──────────────────────────────────────────────────────────────────

def test_bracketed_full_iri_resolves_to_that_iri():
    """``<https://…>`` 는 그 IRI 자체다 — 도메인 NS 로 뭉개지지 않는다."""
    raw = "<https://spec.industrialontologies.org/ontology/core/Core/followedBy>"
    result = resolve_entity_name(raw)
    assert result == URIRef(
        "https://spec.industrialontologies.org/ontology/core/Core/followedBy",
    )
    # 실측 실패 형태를 명시적으로 배제한다.
    assert ">" not in str(result), "꺾쇠가 IRI 에 남으면 rdflib 직렬화가 깨진다"
    assert not str(result).startswith(DOMAIN_NS), (
        "외래 IRI 가 도메인 NS 로 뭉개졌다 — 폐기됐던 원래 버그"
    )


def test_bracketed_ontology_iri_resolves_for_add_triple():
    """``add_triple`` 의 subject 로 오던 ``<http://…/steel-ontology>`` 형태."""
    result = resolve_entity_name("<http://example.com/steel-ontology>")
    assert result == URIRef("http://example.com/steel-ontology")


def test_bracketed_prefixed_name_still_resolves_via_prefix_table():
    """브래킷 안이 prefixed name 이어도 해석 순서를 그대로 탄다."""
    result = resolve_entity_name("<iof-core:MaterialArtifact>")
    assert result == URIRef(FOREIGN_PREFIXES["iof-core"] + "MaterialArtifact")


def test_bracketed_iri_produces_serializable_graph():
    """해석 결과가 실제로 그래프에 쓰이고 직렬화되는가 (산출물로 확인)."""
    from rdflib import OWL, RDF, Graph

    g = Graph()
    prop = resolve_entity_name(f"<{IOF_FOLLOWED_BY}>")
    restriction = URIRef(DOMAIN_NS + "R1")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, prop))
    # 예전에는 여기서 "Invalid IRI code point '>'" 로 깨졌다.
    text = g.serialize(format="turtle")
    assert IOF_FOLLOWED_BY in text


def test_object_term_bracketed_iri_is_not_a_literal():
    """``parse_object_term`` 이 브래킷 IRI 를 Literal 로 만들지 않는다."""
    result = parse_object_term(f"<{IOF_FOLLOWED_BY}>")
    assert isinstance(result, URIRef), (
        "브래킷 IRI 가 평문 Literal 이 되면 삭제·교체 매칭이 영구 실패한다"
    )
    assert result == URIRef(IOF_FOLLOWED_BY)


# ──────────────────────────────────────────────────────────────────
# 2. NEGATIVE — 기존 해석 경로가 보존된다 (핵심)
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 완전 IRI (해석 순서 1)
        ("https://example.org/plain", "https://example.org/plain"),
        ("http://a.b/c", "http://a.b/c"),
        ("urn:x:y", "urn:x:y"),                    # prefix 'urn' 으로 읽으면 안 된다
        ("file:///tmp/a.ttl", "file:///tmp/a.ttl"),
        # 콜론 없음 (해석 순서 2)
        ("followedBy", DOMAIN_NS + "followedBy"),
        # 도메인 prefix 정확 일치 (해석 순서 3)
        (f"{NS_PREFIX}:followedBy", DOMAIN_NS + "followedBy"),
        # 외래 prefix 표 (해석 순서 5)
        ("iof-core:MaterialArtifact", IOF_CORE + "MaterialArtifact"),
    ],
)
def test_unbracketed_paths_unchanged(raw, expected):
    """브래킷이 없는 입력은 브래킷 지원 도입 전과 **동일하게** 해석된다."""
    assert str(resolve_entity_name(raw)) == expected


def test_empty_and_colon_only_still_raise():
    """빈 값 검증이 브래킷 해제 뒤에도 살아 있다."""
    for bad in ("", "   ", ":", "<>", "<:>"):
        with pytest.raises(ValueError):
            resolve_entity_name(bad)


# ──────────────────────────────────────────────────────────────────
# 3. NEGATIVE — 리터럴이 IRI 로 승격되지 않는다
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        '"< 5mm"',              # 인용된 리터럴 — 부등호가 값의 일부
        "< 5mm",                # 공백 있음 → IRI 가 아니다
        "<a b>",                # 내부 공백
        "<a<b>",                # 내부 꺾쇠
        "<unclosed",            # 짝이 맞지 않음
        "unopened>",
    ],
)
def test_literal_intent_not_promoted_to_iri(raw):
    """부등호가 있어도 IRI 표기가 아니면 리터럴로 남는다."""
    result = parse_object_term(raw)
    assert isinstance(result, Literal), f"{raw!r} 가 IRI 로 승격됐다"


def test_strip_helper_is_conservative():
    """``strip_iri_brackets`` 판정 단위 — 좁게 잡는지 고정."""
    assert strip_iri_brackets("<http://a>") == ("http://a", True)
    assert strip_iri_brackets("<>") == ("", True)
    for keep in ("<a b>", "<a<b>", "<unclosed", "unopened>", "plain", ""):
        text, changed = strip_iri_brackets(keep)
        assert (text, changed) == (keep, False), f"{keep!r} 를 건드렸다"


def test_language_and_datatype_literals_unaffected():
    """리터럴 표기 해석(1번 경로)이 브래킷 지원에 영향받지 않는다."""
    assert parse_object_term('"lit"@ko') == Literal("lit", lang="ko")
    typed = parse_object_term('"true"^^xsd:boolean')
    assert isinstance(typed, Literal) and str(typed) == "true"
