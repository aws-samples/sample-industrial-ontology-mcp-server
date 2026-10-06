"""SPARQL egress 가드의 구문 트리 판정 회귀 테스트.

가드는 rdflib SPARQL 파서의 구문 트리에서 SERVICE, FROM/FROM NAMED, LOAD,
USING 노드를 찾는다. 해석할 수 없는 텍스트는 거부한다. 네트워크 호출은 하지
않는다. 거부 경로는 그래프에 닿기 전에 끝나고, rdflib 의 원격 요청 함수는
호출되면 실패하도록 대체한다.
"""

from __future__ import annotations

import json

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF

from domain.namespaces import DOMAIN_NS, NS_PREFIX, prepend_prefixes


class _FailIfQueried:
    """가드보다 질의 실행이 먼저 일어나면 실패시키는 그래프 대역."""

    def query(self, _query):
        raise AssertionError("egress 가드보다 그래프 질의가 먼저 실행됨")


@pytest.fixture(autouse=True)
def _no_outbound(monkeypatch):
    """rdflib SERVICE 평가의 원격 요청 함수를 호출 즉시 실패하도록 바꾼다."""
    import rdflib.plugins.sparql.evaluate as evaluate

    def _refuse(*_args, **_kwargs):
        raise AssertionError("로컬 SPARQL 이 원격 요청을 시도함")

    monkeypatch.setattr(evaluate, "urlopen", _refuse)


#: 작업 목록에 기록된 우회 질의와 같은 계열의 변형. 공백 없는 ``<`` 비교식이
#: 뒤의 SERVICE 를 가렸던 결함을 재현한다.
_COMPARISON_BYPASS = [
    "SELECT ?s WHERE { FILTER(1<2) SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
    "SELECT ?s WHERE { ?s ?p ?a . FILTER(?a<?b) SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
    "SELECT ?s WHERE { BIND(1<2 AS ?x) SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
]

_EGRESS_QUERIES = _COMPARISON_BYPASS + [
    "SELECT * WHERE { SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
    "SELECT * WHERE { SERVICE SILENT <https://example.com/sparql> { ?s ?p ?o } }",
    "SELECT * WHERE { ?s ?p ?e SERVICE ?e { ?s ?p ?o } }",
    "SELECT * WHERE { OPTIONAL { SERVICE <https://example.com/sparql> { ?s ?p ?o } } }",
    "SELECT * WHERE { ?s ?p ?o FILTER EXISTS { SERVICE <https://example.com/sparql> { ?s ?p ?o } } }",
    "ASK FROM <https://example.com/data> WHERE { ?s ?p ?o }",
    "SELECT * FROM NAMED <https://example.com/data> WHERE { GRAPH ?g { ?s ?p ?o } }",
    "CONSTRUCT { ?s ?p ?o } FROM <https://example.com/data> WHERE { ?s ?p ?o }",
    "DESCRIBE ?s FROM <https://example.com/data> WHERE { ?s ?p ?o }",
    "LOAD <https://example.com/data>",
    "LOAD SILENT <https://example.com/data> INTO GRAPH <urn:g>",
    "INSERT { ?s ?p ?o } USING <https://example.com/data> WHERE { ?s ?p ?o }",
    "DELETE { ?s ?p ?o } USING NAMED <https://example.com/data> WHERE { ?s ?p ?o }",
    "INSERT { ?s ?p ?o } WHERE { SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
]


@pytest.mark.parametrize("query", _EGRESS_QUERIES)
def test_guard_rejects_egress_nodes(query):
    """구문 트리에 egress 노드가 있으면 ValueError 로 거부한다."""
    from tools.sparql_local import _reject_query_egress

    with pytest.raises(ValueError, match="SERVICE"):
        _reject_query_egress(query)


@pytest.mark.parametrize("query", _EGRESS_QUERIES)
def test_guard_rejects_egress_after_prefix_prepend(query):
    """호출자가 PREFIX 를 붙인 최종 문자열에서도 같은 판정을 낸다."""
    from tools.sparql_local import _reject_query_egress

    with pytest.raises(ValueError, match="SERVICE"):
        _reject_query_egress(prepend_prefixes(query))


@pytest.mark.parametrize(
    "query",
    [
        "SELECT ?s WHERE { NOT SPARQL",
        "this is not sparql {{{",
        "SELECT ?s WHERE { ?s ?p ?o } SERVICE",
    ],
)
def test_guard_fails_closed_on_unparseable_text(query):
    """해석할 수 없는 텍스트는 egress 여부를 판정할 수 없으므로 거부한다."""
    from tools.sparql_local import _reject_query_egress

    with pytest.raises(ValueError):
        _reject_query_egress(query)


def test_guard_rejects_non_string():
    from tools.sparql_local import _reject_query_egress

    with pytest.raises(ValueError):
        _reject_query_egress(None)  # type: ignore[arg-type]


_LEGIT_QUERIES = [
    # 비교식은 egress 가 아니다.
    "SELECT ?s WHERE { ?s ?p ?a . FILTER(?a<5) }",
    "SELECT ?s WHERE { ?s ?p ?a ; ?q ?b . FILTER(?a<?b && ?b>1) }",
    # 문자열, 주석, 변수, prefixed name 의 단어는 키워드가 아니다.
    'SELECT ?value WHERE { VALUES ?value { "SERVICE FROM NAMED LOAD USING" } } # SERVICE',
    'SELECT ?from ?service WHERE { BIND("local" AS ?from) BIND(1 AS ?service) }',
    "PREFIX ex: <https://example.com/local#> SELECT ?s WHERE { ?s ex:service ?o . ?s ex:from ?f }",
    # PREFIX 선언 없는 prefixed name 도 해석된다 (실행 시 PREFIX 가 붙는다).
    f"SELECT ?id WHERE {{ ?x a {NS_PREFIX}:EquipmentMaster ; {NS_PREFIX}:equipmentID ?id }}",
    "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }",
    "ASK { ?s ?p ?o }",
    "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o } LIMIT 1",
    "DESCRIBE <https://example.com/local#x>",
    "SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s LIMIT 5",
]


@pytest.mark.parametrize("query", _LEGIT_QUERIES)
def test_guard_allows_local_queries(query):
    """로컬 질의는 원문과 PREFIX 를 붙인 최종 문자열 모두 통과한다."""
    from tools.sparql_local import _reject_query_egress

    _reject_query_egress(query)
    _reject_query_egress(prepend_prefixes(query))


@pytest.mark.parametrize("query", _COMPARISON_BYPASS)
def test_sparql_local_rejects_comparison_bypass_before_graph(query, monkeypatch):
    """공개 도구 sparql_local 은 우회 질의를 그래프 로드 전에 거부한다."""
    from tools import sparql_local as module

    def _no_graph(_source):
        raise AssertionError("egress 질의가 그래프 로드까지 진행됨")

    monkeypatch.setattr(module, "_get_graph", _no_graph)

    result = json.loads(module.sparql_local(query))

    assert result["success"] is False
    assert "SERVICE" in result["error"]


def _memory_graph() -> Graph:
    """Python Memory store 그래프 (rdflib 자체 평가기가 질의를 실행한다)."""
    g = Graph()
    ns = Namespace(str(DOMAIN_NS))
    g.bind(NS_PREFIX, ns)
    g.add((ns.EQ001, RDF.type, ns.EquipmentMaster))
    g.add((ns.EQ001, ns.equipmentID, Literal("EQ001")))
    return g


def test_sparql_local_still_runs_prefixed_local_query(monkeypatch):
    """PREFIX 선언 없는 prefixed name 질의는 이전처럼 실행된다."""
    from tools import sparql_local as module

    g = _memory_graph()
    monkeypatch.setattr(module, "_get_graph", lambda _source: (g, "test"))

    result = json.loads(module.sparql_local(
        f"SELECT ?id WHERE {{ ?x a {NS_PREFIX}:EquipmentMaster ; "
        f"{NS_PREFIX}:equipmentID ?id . FILTER(STRLEN(?id)<10) }}"
    ))

    assert result["success"] is True
    assert result["results"] == [{"id": "EQ001"}]


def test_execute_local_sparql_guards_final_query():
    """공용 실행 헬퍼도 그래프 질의 전에 egress 를 거부한다."""
    from domain.sparql_templates import execute_local_sparql

    with pytest.raises(ValueError, match="SERVICE"):
        execute_local_sparql(_FailIfQueried(), _COMPARISON_BYPASS[0])


def test_execute_local_sparql_runs_local_query():
    from domain.sparql_templates import execute_local_sparql

    rows = execute_local_sparql(
        _memory_graph(),
        f"SELECT ?id WHERE {{ ?x {NS_PREFIX}:equipmentID ?id }}",
    )
    assert rows == [{"id": "EQ001"}]


def test_golden_run_single_rejects_egress_before_graph(monkeypatch):
    """golden_sparql 의 egress 질의는 ERROR 로 기록되고 그래프에 닿지 않는다."""
    from tools import golden_queries

    def _no_graph(_source):
        raise AssertionError("egress golden 질의가 그래프 로드까지 진행됨")

    monkeypatch.setattr("tools.sparql_local._get_graph", _no_graph)
    case = {
        "id": "g1",
        "golden_sparql": _COMPARISON_BYPASS[0],
        "assertions": {"min_rows": 1},
    }

    result = golden_queries._run_single(case, "merge")

    assert result["status"] == "ERROR"
    assert "SERVICE" in result["error"]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/x> } SERVICE <https://example.com/sparql> { ?s ?p ?o } . { <https://example.com/y",
        "https://example.com/x\\u003E",
        "https://example.com/a b",
        'https://example.com/"x"',
    ],
)
def test_entity_query_builders_reject_iri_breakout(value):
    """IRI 자리 보간은 IRIREF 문자만 허용한다."""
    from domain.sparql_templates import (
        build_entity_properties_query,
        build_entity_relationships_query,
    )

    with pytest.raises(ValueError):
        build_entity_properties_query(value)
    with pytest.raises(ValueError):
        build_entity_relationships_query(value)


def test_entity_query_builders_keep_valid_iri():
    from domain.sparql_templates import (
        build_entity_properties_query,
        build_entity_relationships_query,
    )
    from tools.sparql_local import _reject_query_egress

    iri = "https://example.com/steel-instances#EQ001"
    for query in (
        build_entity_properties_query(iri),
        build_entity_relationships_query(iri),
    ):
        assert f"<{iri}>" in query
        _reject_query_egress(prepend_prefixes(query))


# ── 판정 파서(rdflib)와 실행 파서(Oxigraph)의 해석 차이 ──────────────────
#
# 기본 store 는 Oxigraph 이고, 가드는 rdflib 파서로 판정한다. 두 파서가 같은 텍스트를
# 다르게 끊는 어휘(LF 없는 CR, 코드포인트 이스케이프)는 구문 트리를 보기 전에 거부한다.
# SERVICE 대상은 네트워크가 없는 ``<urn:evil>`` 이다.

_EVIL_SERVICE = "SERVICE <urn:evil> { ?a ?b ?c }"

_BARE_CR_PAYLOADS = [
    f"SELECT * WHERE {{ ?s ?p ?o # x\r{_EVIL_SERVICE}\n}}",
]

_ESCAPE_FORMS = ["\\u005C", "\\u005c", "\\U0000005C"]
_QUOTE_FORMS = ['"', "'", '"""', "'''"]

_ESCAPE_PAYLOADS = [
    f"SELECT * WHERE {{ ?s ?p ?o BIND({quote}a{escape}{quote} AS ?x) "
    f"{_EVIL_SERVICE} #{quote} AS ?y)\n}}"
    for quote in _QUOTE_FORMS
    for escape in _ESCAPE_FORMS
]

_DIVERGENT_PAYLOADS = _BARE_CR_PAYLOADS + _ESCAPE_PAYLOADS


def _oxigraph_graph() -> Graph:
    """메모리 Oxigraph store 그래프 (실행 파서가 기본 경로와 같다)."""
    g = Graph(store="Oxigraph")
    ns = Namespace(str(DOMAIN_NS))
    g.bind(NS_PREFIX, ns)
    g.add((ns.EQ001, RDF.type, ns.EquipmentMaster))
    g.add((ns.EQ001, ns.equipmentID, Literal("EQ001")))
    return g


def _spy_queries(graph: Graph) -> list[str]:
    """그래프의 query 호출 문자열을 기록하는 목록을 돌려준다."""
    calls: list[str] = []
    original = graph.query

    def _recording_query(query_object, *args, **kwargs):
        calls.append(query_object)
        return original(query_object, *args, **kwargs)

    graph.query = _recording_query  # type: ignore[method-assign]
    return calls


@pytest.mark.parametrize("query", _DIVERGENT_PAYLOADS)
def test_guard_rejects_parser_divergent_lexemes(query):
    """파서 간 해석이 갈리는 어휘는 egress 메시지와 함께 거부한다."""
    from tools.sparql_local import _reject_query_egress

    with pytest.raises(ValueError) as exc_info:
        _reject_query_egress(query)
    assert "SERVICE" in str(exc_info.value)
    assert "FROM" in str(exc_info.value)

    with pytest.raises(ValueError, match="SERVICE"):
        _reject_query_egress(prepend_prefixes(query))


@pytest.mark.parametrize("query", _DIVERGENT_PAYLOADS)
def test_divergent_payloads_reach_service_on_oxigraph(query):
    """전제 확인: 이 payload 들은 Oxigraph 엔진에서 실제로 SERVICE 평가에 도달한다.

    이 전제가 깨지면 (Oxigraph 가 rdflib 과 같게 해석하게 되면) 위 거부 테스트의
    의미가 바뀌므로 함께 확인한다. ``g.query`` 는 store 차단점이 엔진에 넘기기 전에
    거부하므로, 차단점 아래의 엔진 (``g.store._inner``) 을 직접 부른다. 대상이
    ``urn:evil`` 이라 네트워크 요청 없이 URI scheme 오류로 끝난다.
    """
    g = _oxigraph_graph()

    with pytest.raises(OSError, match="urn:evil"):
        list(g.store._inner.query(query, use_default_graph_as_union=True))
    with pytest.raises(ValueError, match="실행 직전 검사에서 SERVICE"):
        list(g.query(query))


@pytest.mark.parametrize("query", _DIVERGENT_PAYLOADS)
def test_sparql_local_rejects_divergent_payload_on_oxigraph(query, monkeypatch):
    """공개 sparql_local 은 Oxigraph 그래프에 질의를 넘기기 전에 거부한다."""
    from tools import sparql_local as module

    g = _oxigraph_graph()
    calls = _spy_queries(g)
    monkeypatch.setattr(module, "_get_graph", lambda _source: (g, "test"))

    result = json.loads(module.sparql_local(query))

    assert result["success"] is False
    assert "SERVICE" in result["error"]
    assert "FROM" in result["error"]
    assert "URI scheme" not in result["error"]
    assert calls == []


def test_sparql_local_runs_crlf_query_on_oxigraph(monkeypatch):
    """CRLF 줄바꿈과 주석이 있는 정상 질의는 Oxigraph 에서 그대로 실행된다."""
    from tools import sparql_local as module

    g = _oxigraph_graph()
    monkeypatch.setattr(module, "_get_graph", lambda _source: (g, "test"))

    query = (
        "SELECT ?id WHERE { # 설비 ID 조회\r\n"
        f"  ?x a {NS_PREFIX}:EquipmentMaster ; {NS_PREFIX}:equipmentID ?id .\r\n"
        "}\r\n"
    )
    result = json.loads(module.sparql_local(query))

    assert result["success"] is True
    assert result["results"] == [{"id": "EQ001"}]


@pytest.mark.parametrize("query", _DIVERGENT_PAYLOADS[:2])
def test_golden_run_single_rejects_divergent_payload(query, monkeypatch):
    """golden 실행 경로도 같은 가드로 거부하고 그래프에 닿지 않는다."""
    from tools import golden_queries

    g = _oxigraph_graph()
    calls = _spy_queries(g)
    monkeypatch.setattr("tools.sparql_local._get_graph", lambda _source: (g, "test"))

    result = golden_queries._run_single(
        {"id": "g1", "golden_sparql": query, "assertions": {"min_rows": 1}},
        "merge",
    )

    assert result["status"] == "ERROR"
    assert "SERVICE" in result["error"]
    assert calls == []


def test_execute_local_sparql_rejects_bare_cr_payload():
    from domain.sparql_templates import execute_local_sparql

    g = _oxigraph_graph()
    calls = _spy_queries(g)

    with pytest.raises(ValueError, match="SERVICE"):
        execute_local_sparql(g, _BARE_CR_PAYLOADS[0])
    assert calls == []


@pytest.mark.parametrize("query", _ESCAPE_PAYLOADS)
def test_execute_local_sparql_runs_one_string_for_guard_and_store(query):
    """execute_local_sparql 은 펼친 문자열 하나를 가드와 실행에 함께 쓴다.

    두 파서가 같은 문자열을 보므로 SERVICE 는 양쪽 모두 문자열 리터럴 안에 있다.
    Oxigraph 는 SERVICE 평가에 도달하지 않고 (OSError 없음), 실행 문자열에는
    코드포인트 이스케이프가 남지 않는다.
    """
    from domain.sparql_templates import _PARSER_DIVERGENT_LEXEMES, execute_local_sparql

    g = _oxigraph_graph()
    calls = _spy_queries(g)

    try:
        execute_local_sparql(g, query)
    except ValueError:
        assert calls == []
        return
    assert len(calls) == 1
    assert not _PARSER_DIVERGENT_LEXEMES[1][0].search(calls[0])


@pytest.mark.parametrize(
    "raw_value",
    ["a<b", "x>1", "O<>K", "<beef", "x>2024", "<2024-01-01", "O'Brien",
     'say "hi"', "back\\slash", "tab\tsep"],
)
def test_execute_local_sparql_keeps_sanitized_values(raw_value):
    """sanitize_sparql_value 를 거친 값은 가드를 통과하고 원래 값과 일치한다."""
    from domain.namespaces import sanitize_sparql_value
    from domain.sparql_templates import execute_local_sparql

    g = _oxigraph_graph()
    ns = Namespace(str(DOMAIN_NS))
    g.add((ns.EQ002, ns.equipmentID, Literal(raw_value)))

    safe = sanitize_sparql_value(raw_value)
    assert "\\u" not in safe
    rows = execute_local_sparql(
        g,
        f'SELECT ?x WHERE {{ ?x {NS_PREFIX}:equipmentID ?id . FILTER(?id = "{safe}") }}',
    )

    assert rows == [{"x": str(ns.EQ002)}]


def test_execute_local_sparql_rejects_value_with_codepoint_escape_text():
    """값 자체에 ``\\u`` + 16진수 4자리가 있으면 실행하지 않는다 (fail closed).

    rdflib 은 이 텍스트를 질의 전체에서 펼쳐 Oxigraph 와 다르게 해석하므로, 가드는
    두 파서가 같은 구조를 본다고 보장할 수 없는 입력을 거부한다.
    """
    from domain.namespaces import sanitize_sparql_value
    from domain.sparql_templates import execute_local_sparql

    g = _oxigraph_graph()
    calls = _spy_queries(g)
    safe = sanitize_sparql_value("\\u0041")
    with pytest.raises(ValueError):
        execute_local_sparql(
            g,
            f'SELECT ?x WHERE {{ ?x {NS_PREFIX}:equipmentID ?id . FILTER(?id = "{safe}") }}',
        )
    assert calls == []


@pytest.mark.parametrize(
    "text",
    [
        "\\u005C", "\\u005c", "\\U0000005C", "\\uABCD", "\\UABCD0000",
        "\\u12", "\\u12G4", "\\x005C", "u005C", "\\\\u0041", "plain",
    ],
)
def test_escape_rule_matches_rdflib_expansion(text):
    """가드의 코드포인트 이스케이프 규칙은 rdflib 이 펼치는 조건과 일치한다."""
    from rdflib.plugins.sparql.parser import expandUnicodeEscapes

    from domain.sparql_templates import _PARSER_DIVERGENT_LEXEMES

    escape_rule = _PARSER_DIVERGENT_LEXEMES[1][0]
    try:
        expanded = expandUnicodeEscapes(text)
    except ValueError:
        expanded = None
    assert bool(escape_rule.search(text)) == (expanded != text)


def test_embedded_query_validation_skips_assertion_tokens_and_empty_golden():
    """실행되지 않는 어설션 토큰 목록과 빈 golden_sparql 은 가드 대상이 아니다.

    ``sparql_must_contain`` 의 "DELETE", "GROUP BY" 같은 토큰은 SPARQL 전문이 아니므로
    fail-closed 파서에 넣으면 정상 입력이 거부된다.
    """
    from tools.golden_queries import _GOLDEN_SCHEMA_EXAMPLE
    from tools.query_test import _cq_to_test_case, _validate_embedded_queries

    _validate_embedded_queries(_GOLDEN_SCHEMA_EXAMPLE)
    _validate_embedded_queries([
        _cq_to_test_case({"id": "CQ01", "question_ko": "질문", "domains": ["EquipmentMaster", "AlarmEvents"]}),
    ])


def test_embedded_query_validation_still_rejects_service_in_golden_sparql():
    from tools.query_test import _validate_embedded_queries

    with pytest.raises(ValueError, match="SERVICE"):
        _validate_embedded_queries([{"id": "x", "golden_sparql": _COMPARISON_BYPASS[0]}])


@pytest.mark.parametrize(
    "query",
    [
        "SELECT ?s WHERE { FILTER(1<2) SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
        "SELECT ?s WHERE { FILTER(?a<?b)SERVICE<https://example.com/sparql>{ ?s ?p ?o } }",
        "select ?s where { service silent <urn:evil> { ?s ?p ?o } }",
        "SELECT ?s WHERE { ?s ?p ?e SERVICE ?e { ?s ?p ?o } }",
    ],
)
def test_lexical_check_sees_service_keyword(query):
    from domain.sparql_templates import _lexical_service_keyword

    assert _lexical_service_keyword(query) is True


@pytest.mark.parametrize(
    "query",
    [
        'SELECT ?value WHERE { VALUES ?value { "SERVICE FROM NAMED" } } # SERVICE',
        "SELECT ?v WHERE { VALUES ?v { '''multi\nSERVICE''' } }",
        'SELECT ?v WHERE { VALUES ?v { "esc \\" SERVICE" } }',
        "PREFIX ex: <https://example.com/local#SERVICE> SELECT ?s WHERE { ?s ex:service ?o }",
        "SELECT ?service ?x WHERE { BIND(1 AS ?service) BIND(\"x\"@service AS ?x) }",
    ],
)
def test_lexical_check_ignores_service_in_literals_iris_names(query):
    from domain.sparql_templates import _lexical_service_keyword

    assert _lexical_service_keyword(query) is False


def test_lexical_check_rejects_even_when_parse_tree_misses_service(monkeypatch):
    """구문 트리 판정이 SERVICE 를 놓쳐도 어휘 판정만으로 거부한다 (두 판정은 독립)."""
    import domain.sparql_templates as templates

    monkeypatch.setattr(templates, "_egress_node_names", lambda tree: set())
    with pytest.raises(ValueError, match="SERVICE"):
        templates.reject_sparql_egress(
            "SELECT ?s WHERE { FILTER(1<2) SERVICE <https://example.com/sparql> { ?s ?p ?o } }"
        )


def test_rdflib_remote_graph_loading_is_disabled():
    """rdflib Memory store 가 FROM 의 IRI 를 원격에서 읽지 않도록 store 수준에서 끈다."""
    import rdflib.plugins.sparql as rdflib_sparql

    import domain.sparql_templates  # noqa: F401 - import 부수효과가 검사 대상

    assert rdflib_sparql.SPARQL_LOAD_GRAPHS is False
