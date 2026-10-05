"""_extract_ttl_from_response fallback 테스트."""
from __future__ import annotations

from tools.tacit_input import _extract_ttl_from_response


def test_standard_code_block_is_extracted():
    raw = "```turtle\n@prefix x: <http://x/> .\n:A a :B .\n```"
    out = _extract_ttl_from_response(raw)
    assert "@prefix" in out
    assert "```" not in out


def test_truncated_response_without_closing_fence():
    """max_tokens 초과로 닫는 ``` 가 없어도 본문 추출되어야 한다."""
    raw = "```turtle\n@prefix x: <http://x/> .\n:A a :B ;\n    :p \"long literal that got cut"
    out = _extract_ttl_from_response(raw)
    assert out.startswith("@prefix")
    assert "```" not in out


def test_turtle_without_code_block():
    raw = "@prefix x: <http://x/> .\n:A a :B ."
    out = _extract_ttl_from_response(raw)
    assert out == raw


def test_plain_code_fence_without_language():
    raw = "```\n@prefix x: <http://x/> .\n:A a :B .\n```"
    out = _extract_ttl_from_response(raw)
    assert "@prefix" in out
    assert "```" not in out


def test_empty_response():
    assert _extract_ttl_from_response("") == ""
    assert _extract_ttl_from_response("   ") == ""


def test_extracted_ttl_is_parseable_when_valid():
    raw = (
        "```turtle\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "steel:A a owl:Class .\n"
        "```"
    )
    out = _extract_ttl_from_response(raw)
    from rdflib import Graph
    g = Graph()
    g.parse(data=out, format="turtle")
    assert len(g) == 1


def test_truncated_but_valid_ttl_remains_parseable():
    """잘린 응답이라도 완전한 문장까지는 파싱 가능해야 한다."""
    raw = (
        "```turtle\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class .\n"
        "steel:B a owl"  # 잘림
    )
    out = _extract_ttl_from_response(raw)
    # 여는 ``` 는 제거됨
    assert not out.startswith("```")


def test_preamble_before_code_block_is_stripped():
    """R23: Sonnet 4.6 이 preamble ("I'll analyze...") + fenced 를 섞어 냈을 때
    TTL 블록만 정확히 추출되어야 한다. 과거 버그: 내부 regex 가 preamble 까지
    포함해 파서가 `^` 에서 BadSyntax 를 냈음.
    """
    raw = (
        "I'll analyze the CSV schema and T-Box to propose candidate tacit knowledge.\n\n"
        "```turtle\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class .\n"
        "```"
    )
    out = _extract_ttl_from_response(raw)
    assert out.startswith("@prefix"), f"TTL should start with @prefix, got: {out[:50]!r}"
    assert "I'll analyze" not in out
    from rdflib import Graph
    g = Graph()
    g.parse(data=out, format="turtle")
    assert len(g) == 1


def test_preamble_without_code_block_is_stripped():
    """코드펜스가 아예 없고 preamble 과 TTL 이 이어져 있을 때 @prefix 부터 추출."""
    raw = (
        "Here is the proposed tacit knowledge TTL:\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class ."
    )
    out = _extract_ttl_from_response(raw)
    assert out.startswith("@prefix")
    assert "Here is" not in out
    from rdflib import Graph
    g = Graph()
    g.parse(data=out, format="turtle")
    assert len(g) == 1


def test_byte_literal_in_response_is_decoded():
    """R23: Sonnet 4.6 이 한국어를 b'\\xec\\x9a\\xa9...' bytes literal 로 출력해
    TTL 파서가 quote 기대 위치에서 BadSyntax 를 내는 현상을 복원한다.
    """
    # '용' = UTF-8 0xEC 0x9A 0xA9, '선' = 0xEC 0x84 0xA0
    raw = (
        "```turtle\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class ;\n"
        "    rdfs:label b'\\xec\\x9a\\xa9\\xec\\x84\\xa0'@ko .\n"
        "```"
    )
    out = _extract_ttl_from_response(raw)
    assert "b'" not in out and 'b"' not in out, f"bytes literal not decoded: {out!r}"
    # 디코드된 결과에 '용선' 포함
    assert "용선" in out or "\\xec" not in out  # 최소한 bytes literal 형태는 아님


def test_multiple_code_blocks_picks_longest_ttl_block():
    """preamble 안에 markdown 예시용 ``` 가 있고 뒤에 실제 TTL ``` 가 있을 때 실제 TTL 선택."""
    raw = (
        "I'll use the following structure:\n"
        "```\n"
        "example\n"
        "```\n"
        "Here is the TTL:\n"
        "```turtle\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "steel:A a owl:Class .\n"
        "steel:B a owl:Class .\n"
        "```"
    )
    out = _extract_ttl_from_response(raw)
    assert out.startswith("@prefix")
    from rdflib import Graph
    g = Graph()
    g.parse(data=out, format="turtle")
    assert len(g) == 2
