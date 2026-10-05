"""자주 사용하는 SPARQL 쿼리 템플릿 + 결과 파싱 유틸.

사용법: f-string 또는 .format()으로 변수 삽입.
모든 쿼리에 SPARQL_PREFIXES를 prefix로 붙여 사용.
"""
from __future__ import annotations

import re

import rdflib.plugins.sparql as _rdflib_sparql
from rdflib import Graph
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.query import Result

from domain.namespaces import DOMAIN_NS, prepend_prefixes, sanitize_sparql_value

#: rdflib Memory store 는 FROM / FROM NAMED 의 IRI 를 원격에서 읽어 온다. 이 서버의
#: 질의는 로컬 그래프 전용이므로 store 수준에서도 원격 로드를 끈다 (가드와 별개의 방어선).
_rdflib_sparql.SPARQL_LOAD_GRAPHS = False

#: 로컬 그래프 밖의 데이터를 요청하게 만드는 rdflib 구문 트리 노드 이름.
#: SERVICE 패턴, 질의 dataset 절(FROM / FROM NAMED), 업데이트의 LOAD 와
#: USING / USING NAMED 가 여기에 해당한다.
_EGRESS_NODE_NAMES = frozenset({
    "ServiceGraphPattern",
    "DatasetClause",
    "Load",
    "UsingClause",
})

_EGRESS_MESSAGE = (
    "로컬 SPARQL은 SERVICE, FROM/FROM NAMED, LOAD, USING 구문을 허용하지 않습니다."
)

#: 판정 파서(rdflib)와 실행 파서(기본 store 인 Oxigraph)가 다르게 끊는 어휘.
#: 이 어휘가 있으면 rdflib 구문 트리가 실제 실행 구조를 대변하지 못하므로 거부한다.
#: - 바로 뒤에 LF 가 없는 CR: rdflib 은 ``#`` 주석을 LF 에서만 끝내고 Oxigraph 는
#:   CR 에서도 끝낸다. CRLF 는 두 파서가 같게 처리하므로 허용한다.
#: - 코드포인트 이스케이프 ``\uXXXX`` / ``\UXXXXXXXX``: rdflib 은 파싱 전에 질의 전체에서
#:   펼치고 (``expandUnicodeEscapes``) Oxigraph 는 문자열과 IRI 안에서만 해석한다.
#:   정규식은 rdflib 의 펼침 조건 (대소문자 무시, 16진수 4자리 이상) 을 그대로 따른다.
_PARSER_DIVERGENT_LEXEMES = (
    (re.compile(r"\r(?!\n)"), "LF 가 뒤따르지 않는 CR"),
    (re.compile(r"\\u[0-9a-f]{4}", re.IGNORECASE), "코드포인트 이스케이프 (\\uXXXX, \\UXXXXXXXX)"),
)


def _reject_parser_divergent_lexemes(query: str) -> None:
    """두 SPARQL 파서가 다르게 해석하는 어휘가 있으면 ValueError 를 던진다."""
    for pattern, label in _PARSER_DIVERGENT_LEXEMES:
        if pattern.search(query):
            raise ValueError(
                f"{_EGRESS_MESSAGE} 질의에 {label} 가 있어 판정 파서와 실행 파서의 "
                "해석이 같다고 보장할 수 없으므로 실행하지 않습니다."
            )


#: SPARQL ``IRIREF`` 본문에 올 수 없는 문자 (문법: ``[^<>"{}|^`\\]-[#x00-#x20]``).
_IRIREF_FORBIDDEN = frozenset('<>"{}|^`\\') | {chr(c) for c in range(0x21)}
#: 키워드 SERVICE. 변수 (``?`` ``$``), prefixed name (``:``), 언어 태그 (``@``),
#: 더 긴 이름의 일부 (``\w`` ``-``) 는 키워드가 아니다.
_SERVICE_KEYWORD = re.compile(r"(?<![\w?$:@-])SERVICE(?![\w:-])", re.IGNORECASE)


def _lexical_service_keyword(query: str) -> bool:
    """SPARQL 어휘 규칙으로 문자열, IRI, 주석을 지운 뒤 SERVICE 키워드가 남는지 본다.

    rdflib 구문 트리 판정과 독립된 두 번째 판정이다. 실행 store (Oxigraph) 는 자체
    파서를 쓰므로, 한 파서의 해석에만 기대면 두 파서가 다르게 끊는 입력이 가드를
    우회할 수 있다. SERVICE 절은 키워드 토큰이 원문에 그대로 있어야 성립하므로
    (코드포인트 이스케이프는 ``_PARSER_DIVERGENT_LEXEMES`` 가 먼저 거부한다)
    두 판정 중 하나라도 SERVICE 를 보면 거부한다.

    ``<`` 는 뒤따르는 문자가 모두 ``IRIREF`` 허용 문자이고 ``>`` 로 닫힐 때만 IRI 로
    지운다. 그렇지 않은 ``<`` 는 비교 연산자로 남긴다.
    """
    out: list[str] = []
    index = 0
    length = len(query)
    while index < length:
        char = query[index]
        if char == "#":
            while index < length and query[index] not in "\r\n":
                index += 1
            out.append(" ")
            continue
        if char == "<":
            end = index + 1
            while end < length and query[end] not in _IRIREF_FORBIDDEN:
                end += 1
            if end < length and query[end] == ">":
                out.append(" ")
                index = end + 1
                continue
            out.append(char)
            index += 1
            continue
        if char in "'\"":
            marker = char * 3 if query.startswith(char * 3, index) else char
            index += len(marker)
            while index < length and not query.startswith(marker, index):
                index += 2 if query[index] == "\\" else 1
            index += len(marker)
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return bool(_SERVICE_KEYWORD.search("".join(out)))


def _parse_sparql_text(query: str):
    """질의 문법으로 먼저 해석하고, 실패하면 업데이트 문법으로 해석한다.

    둘 다 실패하면 ValueError 를 던진다. 해석하지 못한 텍스트는 egress 여부를
    판정할 수 없으므로 호출자는 실행하지 않는다.
    """
    try:
        return parseQuery(query)
    except Exception as query_error:  # noqa: BLE001 - 업데이트 문법으로 재시도
        try:
            return parseUpdate(query)
        except Exception:  # noqa: BLE001 - 아래에서 질의 오류로 보고
            reason = str(query_error).splitlines()[0][:200] if str(query_error) else ""
            raise ValueError(
                "SPARQL 구문을 해석할 수 없어 실행하지 않습니다"
                + (f": {reason}" if reason else ".")
            ) from query_error


def _egress_node_names(tree) -> set[str]:
    """구문 트리 전체를 순회해 egress 노드 이름을 모은다 (재귀 없이 스택 사용)."""
    found: set[str] = set()
    stack = [tree]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if isinstance(node, str) or id(node) in seen:
            # rdflib 의 URIRef / Literal / Variable 은 str 하위 타입이라 여기서 끝난다.
            continue
        seen.add(id(node))
        if isinstance(node, CompValue):
            if node.name in _EGRESS_NODE_NAMES:
                found.add(node.name)
            stack.extend(node.values())
        elif isinstance(node, dict):
            stack.extend(node.values())
        else:
            try:
                stack.extend(iter(node))
            except TypeError:
                continue
    return found


def reject_sparql_egress(query: str) -> None:
    """로컬 그래프 밖으로 요청할 수 있는 SPARQL 을 거부한다.

    판정은 문자열 패턴이 아니라 rdflib SPARQL 파서의 구문 트리로 한다. 비교
    연산자, 문자열, IRI, 주석 안의 단어는 트리에 egress 노드로 나타나지 않으므로
    오탐하지 않는다. 해석할 수 없는 텍스트는 거부한다.

    실행 store 의 파서가 rdflib 과 다르게 끊는 어휘 (``_PARSER_DIVERGENT_LEXEMES``)
    가 있으면 구문 트리를 보기 전에 거부한다. 이 검사를 통과한 텍스트에서는 rdflib 의
    코드포인트 펼침이 항등이므로 판정 파서와 실행 파서가 같은 문자열을 해석한다.

    호출자는 **실제로 실행할 최종 문자열** (PREFIX 를 붙인 뒤) 을 넘겨야 한다.

    Raises:
        ValueError: egress 구문이 있거나, 파서 간 해석이 갈리는 어휘가 있거나,
            텍스트를 해석할 수 없을 때.
    """
    if not isinstance(query, str):
        raise ValueError("SPARQL 질의는 문자열이어야 합니다.")
    _reject_parser_divergent_lexemes(query)
    if _lexical_service_keyword(query):
        raise ValueError(_EGRESS_MESSAGE)
    tree = _parse_sparql_text(query)
    if _egress_node_names(tree):
        raise ValueError(_EGRESS_MESSAGE)


def format_sparql_results(results: Result) -> list[dict[str, str | None]]:
    """rdflib SELECT query 결과를 JSON-serializable dict 리스트로 변환한다.

    SELECT 쿼리 전용. CONSTRUCT/ASK 결과를 넣으면 AttributeError 발생.
    """
    rows = []
    for row in results:
        r: dict[str, str | None] = {}
        for var in results.vars:
            val = row[var]
            r[str(var)] = str(val) if val is not None else None
        rows.append(r)
    return rows


def execute_local_sparql(graph: Graph, query: str) -> list[dict[str, str | None]]:
    """로컬 rdflib Graph에서 SPARQL SELECT를 실행하고 결과를 dict 리스트로 반환한다.

    PREFIX가 없으면 자동 추가. SELECT 쿼리 전용. 실행 직전의 최종 문자열 하나에
    ``reject_sparql_egress`` 를 적용하고 같은 문자열을 실행한다.

    템플릿에 보간된 값은 ``sanitize_sparql_value`` 를 거친다. 그 출력에는 가드가 거부하는
    코드포인트 이스케이프가 없으므로 문자열을 펼치거나 바꾸지 않는다.
    """
    full_query = prepend_prefixes(query)
    reject_sparql_egress(full_query)
    results = graph.query(full_query)
    return format_sparql_results(results)

COUNT_ALL_TRIPLES = """\
SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }
"""

COUNT_NAMED_GRAPH_TRIPLES = """\
SELECT (COUNT(*) AS ?count) WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}
"""

CLASS_DISTRIBUTION = f"""\
SELECT ?class (COUNT(?instance) AS ?count)
WHERE {{
    ?instance a ?class .
    FILTER(STRSTARTS(STR(?class), "{DOMAIN_NS}"))
}}
GROUP BY ?class
ORDER BY DESC(?count)
"""

ENTITY_PROPERTIES = """\
SELECT ?property ?value
WHERE {{
    <{entity_uri}> ?property ?value .
}}
ORDER BY ?property
"""

# NOTE: entity_uri 는 IRI 자리이므로 문자열 리터럴용 sanitize_sparql_value 로는 막을 수
# 없다. 외부 입력은 build_entity_properties_query() 로 IRI 형식을 검증한 뒤 삽입할 것.

SUBCLASS_HIERARCHY = f"""\
SELECT ?class ?parent
WHERE {{
    ?class rdfs:subClassOf ?parent .
    FILTER(STRSTARTS(STR(?class), "{DOMAIN_NS}"))
    FILTER(STRSTARTS(STR(?parent), "{DOMAIN_NS}"))
}}
ORDER BY ?parent ?class
"""

OBJECT_PROPERTY_USAGE = """\
SELECT ?property (COUNT(*) AS ?usage)
WHERE {
    ?s ?property ?o .
    ?property a owl:ObjectProperty .
}
GROUP BY ?property
ORDER BY DESC(?usage)
"""

ORPHAN_NODES = f"""\
SELECT ?entity ?type
WHERE {{
    ?entity a ?type .
    FILTER NOT EXISTS {{ ?entity ?p1 ?other1 . FILTER(?p1 != rdf:type) }}
    FILTER NOT EXISTS {{ ?other2 ?p2 ?entity . }}
    FILTER(STRSTARTS(STR(?type), "{DOMAIN_NS}"))
}}
LIMIT 100
"""

DUPLICATE_TRIPLES = """\
SELECT ?s ?p ?o (COUNT(*) AS ?cnt)
WHERE { ?s ?p ?o }
GROUP BY ?s ?p ?o
HAVING (COUNT(*) > 1)
LIMIT 50
"""

LIST_NAMED_GRAPHS = """\
SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }
"""

_SEARCH_ENTITIES_TEMPLATE = """\
SELECT DISTINCT ?entity ?type ?label
WHERE {{
    ?entity a ?type .
    OPTIONAL {{ ?entity rdfs:label ?label }}
    FILTER(
        CONTAINS(LCASE(STR(?entity)), LCASE("{keyword}")) ||
        CONTAINS(LCASE(STR(?label)), LCASE("{keyword}"))
    )
}}
LIMIT {limit}
"""


def build_search_entities_query(keyword: str, limit: int = 100) -> str:
    """SEARCH_ENTITIES 쿼리를 안전하게 생성한다. keyword를 sanitize 후 삽입."""
    safe_keyword = sanitize_sparql_value(keyword)
    return _SEARCH_ENTITIES_TEMPLATE.format(keyword=safe_keyword, limit=int(limit))


#: SPARQL IRIREF 본문에 올 수 있는 문자 (``<`` ``>`` ``"`` ``{`` ``}`` ``|`` ``^``
#: 백틱, 역슬래시, 공백·제어 문자 제외). 역슬래시를 막으므로 유니코드 이스케이프로
#: IRI 경계를 바꿀 수 없다.
_IRIREF_BODY_RE = re.compile(r'[^<>"{}|^`\\\x00-\x20]+')


def _require_iri(entity_uri: str) -> str:
    """``<...>`` 자리에 넣을 값이 IRIREF 본문 문자만으로 이뤄졌는지 확인한다.

    ``sanitize_sparql_value`` 는 문자열 리터럴 전용이라 IRI 자리에 쓰지 않는다.
    """
    if not isinstance(entity_uri, str) or not _IRIREF_BODY_RE.fullmatch(entity_uri):
        raise ValueError(f"IRI 로 쓸 수 없는 값입니다: {str(entity_uri)[:120]!r}")
    return entity_uri


def build_entity_properties_query(entity_uri: str) -> str:
    """ENTITY_PROPERTIES 쿼리를 안전하게 생성한다. IRI 형식을 검증한 뒤 삽입."""
    return ENTITY_PROPERTIES.format(entity_uri=_require_iri(entity_uri))


def build_entity_relationships_query(entity_uri: str) -> str:
    """ENTITY_RELATIONSHIPS 쿼리를 안전하게 생성한다. IRI 형식을 검증한 뒤 삽입."""
    return ENTITY_RELATIONSHIPS.format(entity_uri=_require_iri(entity_uri))


# 하위 호환: 레지스트리에서 참조하는 문자열 템플릿 유지.
# 외부 입력을 보간할 때는 반드시 sanitize_sparql_value 를 거쳐야 함.
SEARCH_ENTITIES = _SEARCH_ENTITIES_TEMPLATE

ENTITY_RELATIONSHIPS = """\
SELECT ?direction ?property ?related ?relatedType
WHERE {{
    {{
        <{entity_uri}> ?property ?related .
        BIND("outgoing" AS ?direction)
    }} UNION {{
        ?related ?property <{entity_uri}> .
        BIND("incoming" AS ?direction)
    }}
    OPTIONAL {{ ?related a ?relatedType }}
}}
ORDER BY ?direction ?property
"""
