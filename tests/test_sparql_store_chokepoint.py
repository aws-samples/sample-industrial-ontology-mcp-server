"""프로세스 전체 SPARQL egress 차단점 회귀 테스트.

호출자가 가드를 부르지 않아도 rdflib ``Graph.query`` / ``Graph.update`` 는 원격에 닿지
않아야 한다. 실행 경로는 두 갈래다.

- Oxigraph store: ``OxigraphStore.query`` / ``.update`` 가 최종 문자열을 어휘 검사한 뒤
  Rust 엔진에 넘긴다.
- rdflib 평가기 (Memory store, Oxigraph store 가 거절한 해석된 Query 객체): SERVICE /
  LOAD 평가 함수가 거부 함수로 바뀌어 있다.

네트워크는 쓰지 않는다. Python 소켓 연결과 rdflib 의 ``urlopen`` 은 호출되면 실패하게
바꾼다. Oxigraph 에 넘기는 원격 주소는 닫힌 loopback 포트 (127.0.0.1:65530) 와 URI
scheme 오류로 끝나는 ``urn:evil`` 뿐이다.
"""

from __future__ import annotations

import gzip
import inspect
import itertools
import random
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from rdflib import RDF, Dataset, Graph, Literal, Namespace, plugin
from rdflib.plugins.sparql import prepareQuery, prepareUpdate
from rdflib.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
_ENDPOINT = "http://127.0.0.1:65530/"
_EX = Namespace("http://example.com/chokepoint#")
_BS = "\\"
_STORES = ["default", "Oxigraph"]
#: 차단점이 내는 메시지 (Oxigraph 어휘 검사 / rdflib 평가기 거부).
_CHOKEPOINT_ERROR = "실행 직전 검사|rdflib 평가기"
#: Python ``\w`` 에는 없지만 SPARQL ``PN_CHARS_BASE`` 에 드는 이름 문자.
_IDEOGRAPHIC_COMMA = chr(0x3001)
_ZERO_WIDTH_JOINER = chr(0x200D)
_FULLWIDTH_EXCLAMATION = chr(0xFF01)


@pytest.fixture(autouse=True)
def _no_python_network(monkeypatch):
    """Python 수준의 원격 요청 시도를 즉시 실패시킨다."""
    import rdflib.plugins.sparql.evaluate as evaluate

    def _refuse(*_args, **_kwargs):
        raise AssertionError("SPARQL 실행이 네트워크 요청을 시도함")

    monkeypatch.setattr(evaluate, "urlopen", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)


def _graph(store: str, monkeypatch) -> Graph:
    """``domain.tbox_utils._new_graph`` 가 ``RDFLIB_STORE`` 값에 따라 만드는 그래프."""
    from domain import tbox_utils

    monkeypatch.setattr(tbox_utils, "_RDFLIB_STORE", store)
    g = tbox_utils._new_graph()
    g.bind("ex", _EX)
    g.add((_EX.EQ001, RDF.type, _EX.Equipment))
    g.add((_EX.EQ001, _EX.serviceLabel, Literal("SERVICE load 3<5")))
    g.add((_EX.EQ001, _EX.loadValue, Literal(3)))
    g.add((_EX.EQ001, _EX.active, Literal(True)))
    return g


def _trip_oxigraph_engine(monkeypatch) -> None:
    """Oxigraph store 가 Rust 엔진에 접근하면 실패시킨다 (그래프 적재가 끝난 뒤 호출)."""
    store_class = plugin.get("Oxigraph", Store)

    def _engine(_self):
        raise AssertionError("차단점보다 Oxigraph 엔진 접근이 먼저 일어남")

    monkeypatch.setattr(store_class, "_inner", property(_engine))


class _FailIfQueried:
    """검사보다 질의 실행이 먼저 일어나면 실패시키는 그래프 대역."""

    def query(self, _query):
        raise AssertionError("egress 검사보다 그래프 질의가 먼저 실행됨")


# ── 두 store 공통: raw g.query / g.update ──────────────────────────────

_EGRESS_QUERIES = [
    f"SELECT * WHERE {{ ?s ?p ?o SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }}",
    f"SELECT * WHERE {{ ?s ?p ?o SERVICE SILENT <{_ENDPOINT}> {{ ?s ?p ?o }} }}",
    f"select * where {{ ?s ?p ?o . service <{_ENDPOINT}> {{ ?a ?b ?c }} }}",
    f"ASK {{ ?s ?p ?o FILTER EXISTS {{ SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }} }}",
    f"SELECT ?s WHERE {{ ?s ?p ?a . FILTER(?a<5) SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }}",
]

_EGRESS_UPDATES = [
    f"LOAD <{_ENDPOINT}data>",
    f"LOAD <{_ENDPOINT}data> INTO GRAPH <urn:g>",
    f"INSERT {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }}",
]


@pytest.mark.parametrize("store", _STORES)
@pytest.mark.parametrize("query", _EGRESS_QUERIES)
def test_raw_graph_query_with_service_is_rejected(store, query, monkeypatch):
    """가드를 거치지 않은 g.query 의 SERVICE 는 원격 요청 없이 ValueError 로 끝난다."""
    g = _graph(store, monkeypatch)
    if store == "Oxigraph":
        _trip_oxigraph_engine(monkeypatch)

    with pytest.raises(ValueError, match=_CHOKEPOINT_ERROR):
        list(g.query(query))


@pytest.mark.parametrize("store", _STORES)
@pytest.mark.parametrize("update", _EGRESS_UPDATES)
def test_raw_graph_update_with_load_or_service_is_rejected(store, update, monkeypatch):
    g = _graph(store, monkeypatch)
    before = len(g)
    if store == "Oxigraph":
        _trip_oxigraph_engine(monkeypatch)

    with pytest.raises(ValueError, match=_CHOKEPOINT_ERROR):
        g.update(update)
    if store == "default":
        assert len(g) == before


def test_memory_load_silent_loads_nothing(monkeypatch):
    """rdflib 은 LOAD SILENT 의 예외를 삼킨다. 원격 요청 없이 아무것도 적재하지 않는다."""
    g = _graph("default", monkeypatch)
    before = len(g)

    g.update(f"LOAD SILENT <{_ENDPOINT}data>")

    assert len(g) == before


def test_oxigraph_load_silent_is_rejected(monkeypatch):
    g = _graph("Oxigraph", monkeypatch)
    _trip_oxigraph_engine(monkeypatch)

    with pytest.raises(ValueError, match="LOAD"):
        g.update(f"LOAD SILENT <{_ENDPOINT}data>")


@pytest.mark.parametrize("store", _STORES)
def test_parsed_query_objects_are_rejected(store, monkeypatch):
    """해석된 Query / Update 객체는 Oxigraph 가 거절하고 rdflib 평가기가 실행한다."""
    g = _graph(store, monkeypatch)
    query = prepareQuery(f"SELECT * WHERE {{ ?s ?p ?o SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }}")
    update = prepareUpdate(f"LOAD <{_ENDPOINT}data>")

    with pytest.raises(ValueError, match="rdflib 평가기의 SERVICE"):
        list(g.query(query))
    with pytest.raises(ValueError, match="rdflib 평가기의 LOAD"):
        g.update(update)


_LEGIT_QUERIES = [
    # 문자열, IRI, 주석, 변수, 언어 태그, prefixed name 의 local 부분의 단어는 키워드가 아니다.
    ('SELECT ?l WHERE { ?s ex:serviceLabel ?l . FILTER(CONTAINS(?l, "SERVICE")) }', 1),
    ("SELECT ?v WHERE { ?s ex:loadValue ?v . FILTER(?v<5 && ?v>1) } # SERVICE <urn:x>", 1),
    ("SELECT ?s WHERE { ?s a <http://example.com/chokepoint#Equipment> . "
     "FILTER(?s != <http://example.com/service/load>) }", 1),
    ("SELECT ?service ?load WHERE { ?service ex:loadValue ?load }", 1),
    ("SELECT ?x WHERE { BIND('''multi\nSERVICE''' AS ?x) }", 1),
    ('SELECT ?x WHERE { BIND("x"@service AS ?x) }', 1),
    ("SELECT ?s WHERE { ?s ex:active true ; ex:loadValue 3 }", 1),
    ("PREFIX service: <http://example.com/chokepoint#> SELECT ?s WHERE { ?s service:loadValue 3 }", 1),
    ("SELECT ?s WHERE { ?s ex:loadValue ?v . FILTER(?v<=3) }\r\n", 1),
    # 불리언 리터럴과 ``.`` 으로 시작하는 선언된 prefix 는 SERVICE 로 끊어 읽히지 않는다.
    ("PREFIX true.service: <http://example.com/chokepoint#> SELECT ?s WHERE { "
     "?s true.service:loadValue 3 ; true.service:active true }", 1),
]


@pytest.mark.parametrize("store", _STORES)
@pytest.mark.parametrize(("query", "expected_rows"), _LEGIT_QUERIES)
def test_local_queries_still_run(store, query, expected_rows, monkeypatch):
    g = _graph(store, monkeypatch)

    assert len(list(g.query(query))) == expected_rows


@pytest.mark.parametrize("store", _STORES)
def test_local_update_still_runs(store, monkeypatch):
    g = _graph(store, monkeypatch)

    g.update('INSERT DATA { ex:EQ002 ex:serviceLabel "LOAD <urn:x> SERVICE" }')

    assert len(list(g.query("SELECT ?s WHERE { ?s ex:serviceLabel ?l }"))) == 2


def test_oxigraph_only_syntax_passes_without_rdflib_parse(monkeypatch):
    """차단점은 rdflib 파서를 쓰지 않는다. rdflib 이 해석하지 못하는 RDF-star 도 실행된다."""
    g = _graph("Oxigraph", monkeypatch)

    rows = list(g.query("SELECT ?service WHERE { BIND(<<( ex:EQ001 ex:loadValue 3 )>> AS ?service) }"))

    assert len(rows) == 1


# ── Oxigraph 어휘: 공백 없이 붙은 키워드와 해석이 갈리는 ``<`` ─────────────
#
# Oxigraph 파서는 토큰 사이 공백을 요구하지 않고, ``<`` 를 문맥에 따라 IRI 시작이나
# 비교 연산자로 읽는다. 아래 payload 는 단어 경계 정규식이나 ``<`` 를 항상 IRI 로 읽는
# 판정에서는 키워드가 보이지 않지만 Oxigraph 는 SERVICE / LOAD 를 실행한다.

_GLUED_SERVICE_PAYLOADS = [
    # 비교 연산자 ``<`` 뒤의 ``'>b'`` 를 IRI 로 읽으면 뒤의 ``'`` 가 SERVICE 를 문자열로 가린다.
    "SELECT * WHERE { ?s ?p ?o BIND(1<'>b' AS ?x)SERVICE<urn:evil>{ ?a ?b ?c } }",
    # 같은 해석 차이를 주석으로 만든다.
    "SELECT * WHERE { ?s ?p ?o BIND(?o<ex:a#>'\n AS ?x)SERVICE<urn:evil>{ ?a ?b ?c } #'\n}",
    # 숫자 / 불리언 리터럴 바로 뒤의 키워드.
    "SELECT * WHERE { ?s ?p 3SERVICE<urn:evil>{ ?a ?b ?c } }",
    "SELECT * WHERE { ?s ?p trueSERVICE<urn:evil>{ ?a ?b ?c } }",
    # prefixed name 처럼 보이는 키워드 (``service:`` = SERVICE + 빈 prefix 이름).
    "PREFIX : <urn:evil> SELECT * WHERE { ?s ?p ?o . service:{ ?a ?b ?c } }",
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o . SERVICEx:{ ?a ?b ?c } }",
    # PN_LOCAL 이스케이프 ``\'`` 를 문자열 시작으로 읽으면 SERVICE 가 가려진다.
    "PREFIX ex: <urn:ex#> SELECT * WHERE { ?s ?p ?o OPTIONAL { ?s ?p ex:a" + _BS + "' } "
    "SERVICE <urn:evil> { ?a ?b ?c } } #'",
    # IRI 안의 코드포인트 이스케이프. IRI 로 읽지 않으면 ``#`` 가 SERVICE 를 주석으로 가린다.
    "SELECT * WHERE { ?s ?p ?o FILTER(?o != <urn:a" + _BS + "u0041#>) SERVICE <urn:evil> { ?a ?b ?c } }",
    # ``\w`` 밖의 이름 문자. ``\w`` 로 끊으면 prefixed name 이 그 문자 앞에서 끝나고, 그 문자가
    # ``{`` 대신 다음 토큰으로 보여 SERVICE 해석을 놓친다.
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o . SERVICEx:a" + _IDEOGRAPHIC_COMMA + "{ ?a ?b ?c } }",
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o . SERVICEx:a" + _ZERO_WIDTH_JOINER + "{ ?a ?b ?c } }",
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o . SERVICEx:" + _FULLWIDTH_EXCLAMATION + "a{ ?a ?b ?c } }",
    # 선언되지 않은 prefix ``true.SERVICEx`` 를 Oxigraph 는 ``true . SERVICE x:`` 로 되돌아가 읽는다.
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p true.SERVICEx:{ ?a ?b ?c } }",
    "PREFIX : <urn:evil> SELECT * WHERE { ?s ?p true.service:{ ?a ?b ?c } }",
    "PREFIX x.y: <urn:evil> SELECT * WHERE { ?s ?p true.SERVICEx.y:z{ ?a ?b ?c } }",
    # 동사 ``a`` (rdf:type) 뒤에 공백 없이 붙은 리터럴. 단어 ``atrueSERVICE`` 와 prefix
    # ``atrue.SERVICEx`` 를 Oxigraph 는 ``a true SERVICE`` ``a true . SERVICE x:`` 로 읽는다.
    "SELECT * WHERE { ?s !atrueSERVICE<urn:evil>{ ?a ?b ?c } }",
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s !atrue.SERVICEx:{ ?a ?b ?c } }",
    "SELECT * WHERE { ?s !a3SERVICE<urn:evil>{ ?a ?b ?c } }",
    "PREFIX x: <urn:evil> SELECT * WHERE { ?s !a3.SERVICEx:{ ?a ?b ?c } }",
    "SELECT * WHERE { ?s ex:active|atrueSERVICE<urn:evil>{ ?a ?b ?c } }",
]

_GLUED_LOAD_PAYLOADS = [
    "PREFIX x: <urn:evil> LOADx:",
    "PREFIX : <urn:evil> LOAD:",
    "PREFIX x: <urn:x>LOAD<urn:evil>",
    "INSERT DATA { <urn:a> <urn:b> 1 };LOAD<urn:evil>",
    # Oxigraph 는 ``INTO`` 와 ``GRAPH`` 사이에도 공백을 요구하지 않는다.
    "PREFIX x: <urn:evil> LOADx:y INTOGRAPH<urn:g>",
    # SPARQL 1.2 prologue 의 ``VERSION`` 선언은 문자열로 끝나고 그 뒤가 연산의 시작이다.
    'PREFIX x: <urn:evil> VERSION "1.2" LOADx:y',
    "PREFIX x: <urn:evil> LOADx:a" + _IDEOGRAPHIC_COMMA,
]


@pytest.mark.parametrize("query", _GLUED_SERVICE_PAYLOADS)
def test_glued_service_payloads_reach_oxigraph_engine(query, monkeypatch):
    """전제 확인: 차단점 아래 Rust 엔진은 이 payload 의 SERVICE 를 실제로 평가한다.

    대상이 ``urn:`` IRI 라 네트워크 요청 없이 HTTP 클라이언트의 IRI 오류 (OSError) 로
    끝난다. 구문 오류는 SyntaxError 이므로 이 단정을 통과하지 못한다.
    """
    g = _graph("Oxigraph", monkeypatch)

    with pytest.raises(OSError):
        list(g.store._inner.query(query, use_default_graph_as_union=True, prefixes={"ex": str(_EX)}))


@pytest.mark.parametrize("update", _GLUED_LOAD_PAYLOADS)
def test_glued_load_payloads_reach_oxigraph_engine(update, monkeypatch):
    g = _graph("Oxigraph", monkeypatch)

    with pytest.raises(OSError):
        g.store._inner.update(update)


@pytest.mark.parametrize("query", _GLUED_SERVICE_PAYLOADS)
def test_glued_service_payloads_are_rejected_by_chokepoint(query, monkeypatch):
    g = _graph("Oxigraph", monkeypatch)
    _trip_oxigraph_engine(monkeypatch)

    with pytest.raises(ValueError, match="SERVICE 키워드"):
        list(g.query(query))


@pytest.mark.parametrize("update", _GLUED_LOAD_PAYLOADS)
def test_glued_load_payloads_are_rejected_by_chokepoint(update, monkeypatch):
    g = _graph("Oxigraph", monkeypatch)
    _trip_oxigraph_engine(monkeypatch)

    with pytest.raises(ValueError, match="LOAD 키워드"):
        g.update(update)


# ── Oxigraph 가 업데이트를 직접 실행하는 Dataset 경로 ─────────────────────
#
# 일반 Graph 의 update 는 oxrdflib 가 기본 그래프가 아니라는 이유로 거절해 rdflib 평가기로
# 넘어간다. Dataset 의 update 는 Oxigraph 엔진이 LOAD 를 실제로 수행하므로, 이 경로에서는
# store 차단점의 어휘 검사가 유일한 방어선이다.

_DATASET_LOAD_PAYLOADS = ["LOAD <urn:evil>", "LOAD <urn:evil> INTO GRAPH <urn:g>"] + _GLUED_LOAD_PAYLOADS


@pytest.mark.parametrize("update", _DATASET_LOAD_PAYLOADS)
def test_dataset_update_reaches_engine_without_store_check(update, monkeypatch):
    """전제 확인: store 차단점의 어휘 검사를 끄면 Dataset.update 는 Oxigraph 의 LOAD 에 닿는다."""
    import domain.sparql_templates as templates

    monkeypatch.setattr(templates, "_reject_store_egress", lambda _query: None)
    dataset = Dataset(store="Oxigraph")

    with pytest.raises(OSError):
        dataset.update(update)


@pytest.mark.parametrize("update", _DATASET_LOAD_PAYLOADS)
def test_dataset_update_with_load_is_rejected_by_chokepoint(update, monkeypatch):
    dataset = Dataset(store="Oxigraph")
    _trip_oxigraph_engine(monkeypatch)

    with pytest.raises(ValueError, match="LOAD 키워드"):
        dataset.update(update)


def test_dataset_local_update_still_runs():
    dataset = Dataset(store="Oxigraph")

    dataset.update('INSERT DATA { <urn:a> <urn:b> "LOAD <urn:x> INTO GRAPH <urn:g>" }')

    assert len(list(dataset.query("SELECT * WHERE { ?s ?p ?o }"))) == 1


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { ?s ?p 1.5SERVICE<urn:evil>{ } }",
        "SELECT * WHERE { ?s ?p 1e5SERVICE<urn:evil>{ } }",
        "SELECT * WHERE { ?s ?p 'x'SERVICE<urn:evil>{ } }",
        "SELECT * WHERE { ?s ?p ?o.SERVICE<urn:evil>{ } }",
        "SELECT * WHERE { ?s ?p ?o . SERVICESILENT<urn:evil>{ } }",
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p trueSERVICEx:{ } }",
        "SELECT * WHERE { FILTER(1<2 && EXISTS{SERVICE<urn:evil>{}}) }",
        "SELECT * WHERE { <<?s?p?o#>>\n?q ?r . SERVICE <urn:evil> {} }",
        "SELECT * WHERE { ?s ?p ?o # x\rSERVICE <urn:evil> { ?a ?b ?c }\n}",
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o SERVICESILENTx:y # c\n { } }",
        "LOADSILENT<urn:evil>",
        "PREFIX x: <urn:evil> LOADx:y INTO GRAPH <urn:g>",
        "PREFIX x: <urn:evil> LOADx:y ; CLEAR ALL",
        "PREFIX x: <urn:evil> LOADx:y INTO#c\rGRAPH <urn:g>",
        "PREFIX x: <urn:evil> BASE <urn:b> VERSION '1.2' LOADx:y",
        # 불리언 리터럴과 ``.`` 뒤에 붙은 SERVICE 로 시작하는 prefixed name.
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p false.SERVICEx:{ } }",
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p true.SERVICESILENTx: # c\n{ } }",
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o ; ?q true.SERVICEx:y{ } }",
        # 동사 ``a`` 뒤에 붙은 불리언, 소수, 부호 있는 정수.
        "SELECT * WHERE { ?s ?p ?o ;afalseSERVICE<urn:evil>{ } }",
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s a1.5.SERVICEx:{ } }",
        "PREFIX x: <urn:evil> SELECT * WHERE { ?s a-1SERVICEx:{ } }",
        # ``\w`` 밖의 ``PN_CHARS_BASE`` 문자 (U+2103, U+2F00, U+FEFF, U+200C) 로 끝나는 이름.
        *(
            "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p ?o . SERVICEx:a" + chr(code) + "{ } }"
            for code in (0x2103, 0x2F00, 0xFEFF, 0x200C)
        ),
    ],
)
def test_lexical_check_finds_keywords_oxigraph_would_read(query):
    from domain.sparql_templates import _find_egress_keyword

    assert _find_egress_keyword(query) in {"SERVICE", "LOAD"}


@pytest.mark.parametrize(
    "query",
    [
        'SELECT ?v WHERE { VALUES ?v { "SERVICE FROM NAMED LOAD USING" } } # SERVICE',
        "SELECT ?v WHERE { VALUES ?v { '''multi\nSERVICE''' } }",
        'SELECT ?v WHERE { VALUES ?v { "esc ' + _BS + '" SERVICE" } }',
        "PREFIX ex: <https://example.com/local#SERVICE> SELECT ?s WHERE { ?s ex:service ?o }",
        'SELECT ?service ?x WHERE { BIND(1 AS ?service) BIND("x"@service-load AS ?x) }',
        "SELECT * WHERE { ?s <http://example.org/service/load> ?o . FILTER(?o != <http://example.org/loads/x>) }",
        "SELECT * WHERE { _:service ?p ?o . _:b.load ?p ?o . ?s ex:loadFactor ?o }",
        "PREFIX download: <urn:d#> PREFIX service: <urn:s#> SELECT * WHERE { ?s download:x ?o }",
        "SELECT ?s WHERE { ?s ?p ?a ; ?q ?b . FILTER(?a<?b && ?b>1) } # load",
        # service: / load: prefix 를 쓰는 도메인의 정상 질의.
        "PREFIX load: <urn:l#> PREFIX service: <urn:s#> SELECT ?x WHERE { "
        "?x a load:Foo ; load:bar ?y . GRAPH service:g { ?x service:p ?z } }",
        "PREFIX service: <urn:s#> SELECT * FROM service:g { ?s ?p ?o }",
        "PREFIX load: <urn:l#> INSERT DATA { <urn:a> load:b 1 ; load:c 2 }",
        # ``INTO`` 뒤에 ``GRAPH`` 가 없으면 LOAD 해석이 성립하지 않는다.
        "PREFIX load: <urn:l#> SELECT * WHERE { ?s load:into ?o }",
        # 문자열 바로 뒤의 ``load:`` 이름. 뒤에 끝, ``;``, ``INTO GRAPH`` 가 없으면 LOAD 가 아니다.
        'PREFIX load: <urn:l#> SELECT * WHERE { VALUES (?a ?b) { ("v" load:c) } }',
        # 이름 중간에 ``\w`` 밖의 이름 문자가 든 변수, prefixed name, blank node label.
        "PREFIX ex: <urn:e#> SELECT ?service" + _IDEOGRAPHIC_COMMA + "load WHERE { "
        "?s ex:a" + _ZERO_WIDTH_JOINER + "service ?o . _:x" + _FULLWIDTH_EXCLAMATION + "load ?p ?o }",
        # ``.`` 이 든 prefix. 앞 조각이 불리언 리터럴이 아니거나, 뒤에 ``{`` 가 없거나, GRAPH 의
        # 그래프 이름이면 SERVICE 로 끊어 읽히지 않는다.
        "PREFIX ns.service: <urn:s#> SELECT * WHERE { ?s ?p ns.service:x . GRAPH ns.service: { ?s ?p ?o } }",
        "PREFIX true.service: <urn:s#> SELECT * WHERE { ?s true.service:p true.service:x }",
        "PREFIX true.service: <urn:s#> SELECT * WHERE { GRAPH true.service: { ?s ?p ?o } }",
        "PREFIX true.load: <urn:l#> INSERT DATA { <urn:a> true.load:b true.load:c }",
    ],
)
def test_lexical_check_ignores_words_that_are_not_keywords(query):
    from domain.sparql_templates import _find_egress_keyword

    assert _find_egress_keyword(query) is None


# ── 동사 ``a`` 와 리터럴 붙임: Oxigraph 엔진과 어휘 판정의 구조화 차등 비교 ─────────
#
# 동사 위치 (``a`` ``!a`` ``^a``, 경로 연산자와 ``;`` 뒤의 ``a``) 와 목적어 위치에 리터럴을 공백
# 없이 붙이고 그 뒤에 SERVICE 를 둔다. 엔진이 SERVICE 로 읽은 질의는 어휘 판정도 SERVICE 로
# 판정해야 하고 (미탐 0), 엔진이 SERVICE 없이 해석한 질의는 키워드 없음으로 판정해야 한다
# (정상 질의 오탐 0).

_GLUE_PROLOGUE = f"PREFIX x: <urn:evil> PREFIX ex: <{_EX}> "
_GLUE_LEADS = [
    "?s ?p ", "?s a", "?s !a", "?s ^a", "?s ex:active/a", "?s ex:active|a", "?s ?p ?o ;a",
    "?s (a)", "?s a*", "?s ex:active?",
]
_GLUE_LITERALS = ["", "true", "false", "3", "-1", "1.5", ".5", "1e5", "-1.5e+3", "'x'", "_:b", "x:a"]
_GLUE_SEPARATORS = ["", ".", " ", ";"]
_GLUE_KEYWORDS = ["SERVICE", "service SILENT"]
_GLUE_TARGETS = ["<urn:evil>", "x:", "x:y"]


def _engine_status(engine, query: str) -> str:
    """차단점 아래 Oxigraph 엔진의 결과. ``service`` 는 SERVICE 평가에 닿았다는 뜻이다."""
    try:
        list(engine.query(query, use_default_graph_as_union=True))
    except SyntaxError:
        return "syntax"
    except OSError:
        return "service"
    return "ok"


def test_verb_and_literal_glue_matches_oxigraph(monkeypatch):
    """Oxigraph 가 SERVICE 로 읽는 붙임 형태는 어휘 판정도 SERVICE 로 보고, 나머지는 통과시킨다.

    왼쪽 패턴의 해가 없으면 엔진은 SERVICE 를 평가하지 않는다. 그런 질의는 키워드 글자 하나를
    바꾼 대조 질의와 비교한다. 원래 질의만 구문 오류 없이 해석되면 엔진이 그 글자열을 키워드로
    읽은 것이다.
    """
    from domain.sparql_templates import _find_egress_keyword

    engine = _graph("Oxigraph", monkeypatch).store._inner
    misses: list[str] = []
    false_positives: list[str] = []
    glued_leads: set[str] = set()
    for lead, literal, separator, keyword, target in itertools.product(
        _GLUE_LEADS, _GLUE_LITERALS, _GLUE_SEPARATORS, _GLUE_KEYWORDS, _GLUE_TARGETS
    ):
        head = _GLUE_PROLOGUE + "SELECT * WHERE { " + lead + literal + separator
        tail = target + "{ ?a ?b ?c } }"
        query = head + keyword + tail
        status = _engine_status(engine, query)
        control = head + keyword.replace("ICE", "IXE").replace("ice", "ixe") + tail
        reads_service = status == "service" or (
            status == "ok" and _engine_status(engine, control) == "syntax"
        )
        found = _find_egress_keyword(query)
        if reads_service:
            if literal and not separator:
                glued_leads.add(lead)
            if found != "SERVICE":
                misses.append(query)
        elif status == "ok" and found is not None:
            false_positives.append(query)

    assert misses == []
    assert false_positives == []
    # 전제 확인: 모든 동사 위치에서 공백 없이 붙인 리터럴 뒤의 SERVICE 가 엔진에 닿는다.
    assert glued_leads == set(_GLUE_LEADS)


def test_lexical_check_accepts_large_iri_lists():
    """IRI 후보마다 두 해석을 따라가도 결과가 같고 큰 VALUES 질의를 받아들인다."""
    from domain.sparql_templates import _find_egress_keyword

    values = " ".join(f"<http://example.com/load#Item{i}>" for i in range(5000))
    assert _find_egress_keyword(f"SELECT * WHERE {{ VALUES ?x {{ {values} }} ?x ?p ?o }}") is None
    assert _find_egress_keyword(
        f"SELECT * WHERE {{ VALUES ?x {{ {values} }} SERVICE <urn:evil> {{ }} }}"
    ) == "SERVICE"


#: 단어 토큰이 한 글자씩 끊기지만 ``[PN_CHARS.]`` 구간은 이어지는 단위 (``.``, ``-``, U+00B7).
_NAME_RUN_UNITS = ["a.", "a-", "a" + chr(0xB7)]
#: 약 200KB 구간. prefix 를 시작 위치마다 정규식으로 다시 읽으면 수 분이 걸리는 길이다.
_NAME_RUN_REPEAT = 100_000
#: 이 프로세스의 CPU 시간 상한. 선형 판정은 이 길이에서 상한보다 충분히 짧게 끝나고, 시작
#: 위치마다 구간을 다시 읽는 판정은 같은 입력에서 CPU 시간으로도 상한을 크게 넘는다.
_TIME_LIMIT_SECONDS = 1.0


def _cpu_seconds(call) -> float:
    """``call`` 이 쓴 이 프로세스의 CPU 시간. 다른 프로세스의 부하는 값에 들지 않는다."""
    started = time.process_time()
    call()
    return time.process_time() - started


@pytest.mark.parametrize("unit", _NAME_RUN_UNITS)
def test_lexical_check_is_linear_on_long_name_runs(unit):
    """긴 이름 문자 구간에서도 어휘 판정과 호출자 가드가 길이에 비례하는 시간 안에 끝난다.

    시간은 벽시계가 아니라 이 프로세스의 CPU 시간으로 잰다.
    """
    from domain.sparql_templates import _find_egress_keyword, reject_sparql_egress

    run = unit * _NAME_RUN_REPEAT
    clean = "SELECT * WHERE { ?s ?p " + run + " } # LOAD"
    evil = "SELECT * WHERE { ?s ?p " + run + "true.SERVICE<urn:evil>{} }"
    found: list[str | None] = []

    assert _cpu_seconds(lambda: found.append(_find_egress_keyword(clean))) < _TIME_LIMIT_SECONDS
    assert _cpu_seconds(lambda: found.append(_find_egress_keyword(evil))) < _TIME_LIMIT_SECONDS
    assert found == [None, "SERVICE"]
    # 키워드가 없는 질의는 rdflib 파서가 해석하지 못해 거부되고, 있는 질의는 어휘 판정이 거부한다.
    for query, message in ((clean, "구문을 해석할 수 없어"), (evil, "SERVICE")):

        def _reject(query=query, message=message):
            with pytest.raises(ValueError, match=message):
                reject_sparql_egress(query)

        assert _cpu_seconds(_reject) < _TIME_LIMIT_SECONDS


@pytest.mark.parametrize("unit", _NAME_RUN_UNITS)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("true.SERVICE<urn:evil>{} }", "SERVICE"),
        ("x.SERVICE<urn:evil>{} }", "SERVICE"),
        (" true.SERVICEx:{} }", "SERVICE"),
        ("x.service:p ?o }", None),
        (" } # SERVICE <urn:evil> {}", None),
    ],
)
def test_lexical_check_finds_keywords_inside_name_runs(unit, tail, expected):
    """이름 문자 구간 끝에 붙은 키워드 판정은 구간 길이와 무관하게 같다."""
    from domain.sparql_templates import _find_egress_keyword

    for repeat in (1, 1000):
        query = "PREFIX x: <urn:evil> SELECT * WHERE { ?s ?p " + unit * repeat + tail
        assert _find_egress_keyword(query) == expected


def test_token_matcher_agrees_with_grammar_regex():
    """``_match_token`` 의 prefixed name 판정은 ``PN_PREFIX ':' PN_LOCAL?`` 문법 정규식과 같다.

    정규식 판정은 시작 위치마다 구간을 다시 읽어 느리지만 문법을 그대로 옮긴 기준이다.
    prefix 가 성립하지 않는 위치에서는 같은 토큰 정규식의 결과와 같아야 한다.
    """
    import domain.sparql_templates as templates

    grammar_pname = re.compile(
        rf"(?P<prefix>[{templates._PN_CHARS_BASE}](?:[{templates._PN_CHARS}.]*[{templates._PN_CHARS}])?)"
        rf":(?:{templates._PN_LOCAL})?"
    )
    alphabet = list("aSEz09_-.:%\\'\"#<>{}?@ \n") + [
        chr(0xB7), _IDEOGRAPHIC_COMMA, _ZERO_WIDTH_JOINER, _FULLWIDTH_EXCLAMATION, chr(0x0300), "%2F",
    ]
    rng = random.Random(0)
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        name_runs = templates._name_runs(text)
        for pos in range(len(text)):
            reference = grammar_pname.match(text, pos)
            if reference:
                expected = ("pname", reference.end(), reference.group("prefix"))
            else:
                token = templates._LEXICAL_TOKEN.match(text, pos)
                expected = (token.lastgroup, token.end(), "")
            assert templates._match_token(text, pos, name_runs) == expected, (text, pos)


def test_existing_guard_uses_the_same_lexical_check(monkeypatch):
    """호출자 가드 (reject_sparql_egress) 의 어휘 판정도 같은 함수를 쓴다."""
    import domain.sparql_templates as templates

    monkeypatch.setattr(templates, "_find_egress_keyword", lambda _query: "SERVICE")

    assert templates._lexical_service_keyword("SELECT * WHERE { ?s ?p ?o }") is True
    with pytest.raises(ValueError, match="SERVICE"):
        templates.reject_sparql_egress("SELECT * WHERE { ?s ?p ?o }")


# ── 설치 ──────────────────────────────────────────────────────────────

def test_install_is_idempotent_and_keeps_store_signatures():
    import rdflib.plugins.sparql as rdflib_sparql
    import rdflib.plugins.sparql.evaluate as evaluate
    import rdflib.plugins.sparql.update as update

    from domain.sparql_templates import (
        _CHOKEPOINT_MARK,
        _refuse_load_evaluation,
        _refuse_service_evaluation,
        install_sparql_egress_chokepoint,
    )

    install_sparql_egress_chokepoint()
    install_sparql_egress_chokepoint()

    store_class = plugin.get("Oxigraph", Store)
    for name in ("query", "update"):
        method = getattr(store_class, name)
        assert getattr(method, _CHOKEPOINT_MARK, False) is True
        assert getattr(method.__wrapped__, _CHOKEPOINT_MARK, False) is False
        assert inspect.signature(method) == inspect.signature(method.__wrapped__)
    assert evaluate.evalServiceQuery is _refuse_service_evaluation
    assert update.evalLoad is _refuse_load_evaluation
    assert rdflib_sparql.SPARQL_LOAD_GRAPHS is False


_INSTALL_PROBE = """
import rdflib.plugins.sparql as sparql
import rdflib.plugins.sparql.evaluate as evaluate
import rdflib.plugins.sparql.update as update
from rdflib import plugin
from rdflib.store import Store
store_class = plugin.get("Oxigraph", Store)
print(
    evaluate.evalServiceQuery.__name__,
    update.evalLoad.__name__,
    getattr(store_class.query, "_sparql_egress_chokepoint", False),
    getattr(store_class.update, "_sparql_egress_chokepoint", False),
    sparql.SPARQL_LOAD_GRAPHS,
)
"""

_INSTALLED = "_refuse_service_evaluation _refuse_load_evaluation True True False"


def _probe_install_after(setup: str) -> str:
    """새 프로세스에서 ``setup`` 을 실행한 뒤 차단점 설치 상태를 한 줄로 돌려준다."""
    completed = subprocess.run(
        [sys.executable, "-c", setup + "\n" + _INSTALL_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip().splitlines()[-1]


def test_fresh_process_has_no_chokepoint():
    """대조군: 아무것도 import 하지 않은 프로세스에는 차단점이 없다."""
    assert _probe_install_after("") == "evalServiceQuery evalLoad False False True"


@pytest.mark.parametrize(
    "setup",
    [
        "import domain.rules_paths",
        "import tools.registry",
        "import server",
        "import sys\nsys.path.insert(0, 'scripts')\nimport verify_workshop_sparql",
    ],
    ids=["domain-submodule", "tools-registry", "server", "workshop-script"],
)
def test_import_paths_install_chokepoint(setup):
    """domain 하위 모듈, 도구 레지스트리, 서버 진입점, 워크샵 검증 스크립트 import 가 차단점을 설치한다.

    도구 레지스트리는 도구 타깃을 해석하기 전이라도 import 만으로 설치해야 한다.
    """
    assert _probe_install_after(setup) == _INSTALLED


# ── 워크샵 검증 스크립트 ───────────────────────────────────────────────

def _workshop_module():
    from scripts import verify_workshop_sparql

    return verify_workshop_sparql


def _steel_graph() -> Graph:
    g = Graph()
    steel = Namespace("http://example.com/steel-ontology#")
    g.add((steel.EQ001, RDF.type, steel.EquipmentMaster))
    return g


def test_workshop_verifier_rejects_egress_block_before_graph():
    workshop = _workshop_module()

    ok, message = workshop.verify_sparql(
        f"SELECT * WHERE {{ SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }}", _FailIfQueried()
    )

    assert ok is False
    assert message.startswith("EGRESS REJECTED")


def test_workshop_verifier_graph_stays_guarded_without_block_check(monkeypatch):
    """블록 검사를 건너뛰어도 그래프 실행 경로의 차단점이 원격 요청을 막는다."""
    workshop = _workshop_module()
    monkeypatch.setattr(workshop, "reject_sparql_egress", lambda _query: None)

    ok, message = workshop.verify_sparql(
        f"SELECT * WHERE {{ ?s ?p ?o SERVICE <{_ENDPOINT}> {{ ?s ?p ?o }} }}", _steel_graph()
    )

    assert ok is False
    assert "SERVICE" in message


def test_workshop_verifier_still_runs_local_block():
    workshop = _workshop_module()

    assert workshop.verify_sparql("SELECT ?eq WHERE { ?eq a steel:EquipmentMaster }", _steel_graph()) == (
        True,
        "1 rows",
    )


def test_workshop_verifier_reads_docs_as_utf8_under_legacy_locale(tmp_path, monkeypatch, capsys):
    """Windows 기본 locale 인코딩 (cp949) 에서도 UTF-8 한국어 문서를 읽는다.

    ``Path.read_text`` 가 인코딩 인자 없이 불리면 cp949 로 디코드하도록 바꿔 재현한다.
    """
    workshop = _workshop_module()
    tbox = tmp_path / "t_box.ttl"
    tbox.write_text(
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "steel:EquipmentMaster a <http://www.w3.org/2002/07/owl#Class> .\n",
        encoding="utf-8",
    )
    abox = tmp_path / "a_box.ttl.gz"
    with gzip.open(abox, "wt", encoding="utf-8") as stream:
        stream.write(
            "<http://example.com/steel-ontology/instances#EQ001> a "
            "<http://example.com/steel-ontology#EquipmentMaster> .\n"
        )
    doc = tmp_path / "workshop" / "guide.md"
    doc.parent.mkdir()
    doc.write_text(
        "# 설비 데이터를 조회한다\n\n```sparql\nSELECT ?eq WHERE { ?eq a steel:EquipmentMaster }\n```\n",
        encoding="utf-8",
    )
    for name, value in {
        "REPO": tmp_path,
        "PRE_TBOX": tbox,
        "PRE_ABOX": abox,
        "DOCS": [doc],
        "verify_gzip_artifact": lambda *_args: [],
        "verify_dictionary": list,
    }.items():
        monkeypatch.setattr(workshop, name, value)
    monkeypatch.setattr(sys, "argv", ["verify_workshop_sparql.py", "--ignore-placeholders"])

    original_read_text = Path.read_text

    def _legacy_locale_read_text(self, encoding=None, errors=None):
        return original_read_text(self, encoding=encoding or "cp949", errors=errors)

    monkeypatch.setattr(Path, "read_text", _legacy_locale_read_text)

    assert workshop.main() == 0
    assert "PASS 1" in capsys.readouterr().out
