"""자주 사용하는 SPARQL 쿼리 템플릿 + 결과 파싱 유틸.

사용법: f-string 또는 .format()으로 변수 삽입.
모든 쿼리에 SPARQL_PREFIXES를 prefix로 붙여 사용.
"""
from __future__ import annotations

import bisect
import functools
import re

import rdflib.plugins.sparql as _rdflib_sparql
import rdflib.plugins.sparql.evaluate as _rdflib_evaluate
import rdflib.plugins.sparql.update as _rdflib_update
from rdflib import Graph, plugin
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.query import Result
from rdflib.store import Store

from domain.namespaces import DOMAIN_NS, prepend_prefixes, sanitize_sparql_value

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


# ── 어휘 판정: Oxigraph 가 원격 요청으로 해석할 수 있는 SERVICE / LOAD 키워드 ──
#
# 판정 대상은 실행 store (Oxigraph) 의 해석이다. Oxigraph 파서는 토큰 사이 공백을
# 요구하지 않는다 (``1SERVICE`` ``trueSERVICE`` ``atrueSERVICE`` ``service:{`` ``LOADx:y`` 도
# 키워드로 읽는다). 그래서 단어 경계 정규식이 아니라 토큰 단위로 판정한다.

# 이름 문자 클래스는 SPARQL 문법의 ``PN_CHARS_BASE`` ``PN_CHARS_U`` ``PN_CHARS``
# ``VARNAME`` 을 그대로 옮긴다. Python ``\w`` 로 대신하면 U+3001, U+200D, U+FF01 처럼
# ``\w`` 밖의 이름 문자에서 토큰이 Oxigraph 보다 일찍 끝나고, 그 문자를 "다음 토큰" 으로
# 보게 되어 키워드 판정이 빗나간다. 문법 클래스는 Oxigraph 가 이름에 받는 문자를 모두
# 포함한다. Oxigraph 가 받지 않는 문자 (U+10000 이상 등) 까지 이름으로 읽어 토큰이 더
# 길어지는 경우에는 Oxigraph 가 그 문자에서 구문 오류를 내므로 질의가 실행되지 않는다.
_PN_CHARS_BASE = (
    r"A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF"
    r"\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD"
    r"\U00010000-\U000EFFFF"
)
_PN_CHARS_U = _PN_CHARS_BASE + "_"
#: ``VARNAME`` 의 두 번째 이후 문자. ``PN_CHARS`` 에서 ``-`` 만 빠진다.
_VARNAME_REST = _PN_CHARS_U + r"0-9\u00B7\u0300-\u036F\u203F-\u2040"
_PN_CHARS = _VARNAME_REST + r"\-"
#: ``PN_LOCAL`` 의 ``%XX`` 와 ``\`` 이스케이프. 이스케이프된 ``'`` ``#`` 를 문자열이나
#: 주석의 시작으로 읽으면 뒤따르는 키워드를 지우게 되므로 이름의 일부로 소비한다.
_PLX = r"%[0-9A-Fa-f]{2}|\\[_~.\-!$&'()*+,;=/?#@%]"
_PN_LOCAL = (
    rf"(?:[{_PN_CHARS_U}:0-9]|{_PLX})"
    rf"(?:(?:[{_PN_CHARS}.:]|{_PLX})*(?:[{_PN_CHARS}:]|{_PLX}))?"
)

# ``PN_PREFIX`` 는 ``PN_CHARS_BASE`` 로 시작해 ``[PN_CHARS.]`` 로 이어지고 ``.`` 로 끝나지
# 않는다. prefix 의 문자는 모두 ``[PN_CHARS.]`` 이고 ``:`` 는 여기에 들지 않으므로, prefix
# 뒤의 ``:`` 는 시작 위치가 든 ``[PN_CHARS.]`` 최대 구간의 끝에만 올 수 있다. 그래서 prefix
# 판정은 정규식 역추적이 아니라 질의마다 한 번 계산한 구간 끝으로 한다. 정규식으로 prefix 를
# 시도하면 구간 안의 시작 위치마다 구간 끝까지 다시 읽어 구간 길이의 제곱에 비례하는 시간이
# 든다 (``a.a.a.`` 처럼 단어 토큰이 짧게 끊기는 구간).

#: prefix 후보 문자 (``PN_CHARS`` 와 ``.``) 의 최대 연속 구간.
_NAME_RUN = re.compile(rf"[{_PN_CHARS}.]+")
#: ``PN_PREFIX`` 의 첫 문자.
_PN_PREFIX_START = re.compile(rf"[{_PN_CHARS_BASE}]")
#: prefix 뒤의 ``:`` 와 local 부분.
_PNAME_AFTER_PREFIX = re.compile(rf":(?:{_PN_LOCAL})?")
#: 부호 없는 숫자 리터럴 (``INTEGER`` ``DECIMAL`` ``DOUBLE``).
_NUMBER = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"

#: Oxigraph 어휘 규칙을 따르는 토큰. 앞선 대안이 우선한다.
#: - 주석은 CR 또는 LF 에서 끝난다.
#: - IRI 후보는 ``IRIREF`` 허용 문자와 코드포인트 이스케이프 (Oxigraph 는 IRI 안에서
#:   ``\uXXXX`` 를 해석한다) 로 이뤄지고 ``>`` 로 닫힌다.
#: - 문자열은 닫힌 것만 토큰이다. 닫히지 않은 문자열은 Oxigraph 가 구문 오류로 거부한다.
#: - prefix 가 있는 prefixed name 은 ``_match_token`` 이 이 정규식보다 먼저 판정한다.
#:   ``PN_CHARS_BASE`` 로 시작하는 위치에서는 ``pname`` 앞의 대안이 성립하지 않으므로
#:   순서가 같다. 여기의 ``pname`` 은 빈 prefix (``:x``) 만 받는다.
_LEXICAL_TOKEN = re.compile("|".join((
    r"(?P<ws>[ \t\r\n]+)",
    r"(?P<comment>#)",
    r'(?P<iri><(?:[^<>"{}|^`\\\x00-\x20]|\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8})*>)',
    r"(?P<string>'''(?:(?:''|')?(?:[^'\\]|\\[\s\S]))*'''"
    r'|"""(?:(?:""|")?(?:[^"\\]|\\[\s\S]))*"""'
    r"|'(?:[^'\\\r\n]|\\[\s\S])*'"
    r'|"(?:[^"\\\r\n]|\\[\s\S])*")',
    r"(?P<open_string>['\"])",
    # 이름 없는 ``?`` 는 경로 수식자이므로 하나의 토큰으로 소비한다.
    rf"(?P<var>[?$](?:[{_PN_CHARS_U}0-9][{_VARNAME_REST}]*)?)",
    r"(?P<lang>@[A-Za-z]+(?:-[A-Za-z0-9]+)*)",
    rf"(?P<bnode>_:[{_PN_CHARS_U}0-9](?:[{_PN_CHARS}.]*[{_PN_CHARS}])?)",
    rf"(?P<pname>:(?:{_PN_LOCAL})?)",
    rf"(?P<number>{_NUMBER})",
    r"(?P<word>[^\W\d]\w*)",
    r"(?P<other>[\s\S])",
)))

#: 단어나 prefix 안에서 키워드 앞에 공백 없이 붙을 수 있는 리터럴. Oxigraph 는 불리언 리터럴과,
#: 동사 ``a`` (``rdf:type``) 와 그 뒤의 리터럴을 공백 없이 끊어 읽는다. ``atrueSERVICE`` 는
#: ``a true SERVICE`` 로, ``a-1.5SERVICEx:`` 는 ``a -1.5 SERVICE x:`` 로 읽는다. 숫자 리터럴로
#: 시작하는 경우 (``3SERVICE``) 는 토크나이저가 숫자와 단어를 나눠 읽으므로 여기서 다루지 않는다.
_GLUED_LITERAL = rf"(?:a?(?:true|false)|a[-+]?{_NUMBER})"
#: 단어나 prefix 가 이 형태로 시작하면 Oxigraph 가 키워드로 끊어 읽을 수 있다.
_EGRESS_KEYWORD_START = re.compile(rf"{_GLUED_LITERAL}?(SERVICE|LOAD)", re.IGNORECASE)
#: prefix 가 리터럴과 트리플 끝 ``.`` 뒤의 SERVICE 로 끊기는 형태 (``true.SERVICEx``
#: ``a3.SERVICEx``). 소수점과 지수가 든 숫자도 정규식 역추적으로 ``.`` 위치를 찾는다.
_DOTTED_SERVICE_START = re.compile(rf"{_GLUED_LITERAL}\.(SERVICE)", re.IGNORECASE)
#: 원문에 이 글자열이 없으면 어떤 해석에서도 키워드가 생기지 않는다 (빠른 경로).
_EGRESS_KEYWORD_HINT = re.compile(r"SERVICE|LOAD", re.IGNORECASE)
#: 공백 한 글자 또는 줄 끝까지의 주석 하나. 주석은 반드시 줄 끝(또는 텍스트 끝)까지
#: 읽으므로 같은 입력을 여러 방식으로 나눌 수 없고, 반복해도 선형 시간에 끝난다.
_TRIVIA_UNIT = r"(?:[ \t\r\n]|#[^\r\n]*(?=[\r\n]|\Z))"
#: 다음 토큰 앞의 공백과 주석.
_TRIVIA = re.compile(rf"{_TRIVIA_UNIT}*")
#: ``LOAD iri`` 뒤에 올 수 있는 것 (끝, 다음 연산의 ``;``, ``INTO GRAPH``). Oxigraph 는
#: ``INTO`` 와 ``GRAPH`` 사이에도 공백을 요구하지 않으므로 ``INTOGRAPH`` 도 받는다.
_AFTER_LOAD_IRI = re.compile(rf"\Z|;|INTO{_TRIVIA_UNIT}*GRAPH", re.IGNORECASE)
#: 뒤의 prefixed name 을 그래프 이름으로 받는 키워드.
_GRAPH_NAME_KEYWORDS = frozenset({"GRAPH", "FROM", "NAMED"})


def _egress_keyword_at_start(text: str) -> str | None:
    match = _EGRESS_KEYWORD_START.match(text)
    return match.group(1).upper() if match else None


def _prefix_keyword(prefix: str) -> str | None:
    """Oxigraph 가 prefix 를 SERVICE / LOAD 키워드로 끊어 읽을 수 있으면 그 키워드를 돌려준다.

    Oxigraph 는 선언되지 않은 prefix 를 이름으로 읽지 못하면 같은 위치의 다른 해석으로
    되돌아간다. 이때 prefix 앞부분을 불리언 리터럴, 또는 동사 ``a`` 와 그 뒤의 리터럴로 공백
    없이 끊어 읽는다 (``_GLUED_LITERAL``). 그래서 prefix 가 키워드로 시작하는 경우
    (``SERVICEx`` ``trueSERVICEx`` ``a3SERVICEx``) 와, 그 리터럴 뒤의 트리플 끝 ``.`` 에서
    끊기는 경우 (``true.SERVICEx:{`` 는 ``true . SERVICE x: {``, ``atrue.SERVICEx:{`` 는
    ``a true . SERVICE x: {``) 를 본다. ``.`` 뒤에서는 업데이트 연산이 시작하지 않으므로
    ``.`` 에서 끊기는 경우는 SERVICE 만 해당한다.
    """
    keyword = _egress_keyword_at_start(prefix)
    if keyword:
        return keyword
    match = _DOTTED_SERVICE_START.match(prefix)
    return match.group(1).upper() if match else None


def _keyword_pname_is_egress(
    query: str, keyword: str, end: int, previous: tuple[str, str] | None
) -> bool:
    """키워드로 읽을 수 있는 prefixed name (``service:x`` ``LOADx:y`` ``true.SERVICEx:``) 을 본다.

    Oxigraph 는 ``SERVICEx:{`` 를 ``SERVICE x: {`` 로, ``LOADx:`` 를 ``LOAD x:`` 로 읽는다.
    키워드 해석이 성립하는 자리에서만 거부한다. 그래야 ``service:`` ``load:`` prefix 를 쓰는
    도메인의 정상 질의가 통과한다.

    - SERVICE: 뒤에 그래프 패턴 ``{`` 가 와야 한다. ``GRAPH`` ``FROM`` ``NAMED`` 바로 뒤의
      이름은 그래프 이름이다.
    - LOAD: 업데이트 연산의 시작이고 뒤에 끝, ``;``, ``INTO GRAPH`` 가 와야 한다. 연산의
      시작은 텍스트 시작, ``;`` 뒤, prologue 선언의 끝이다. prologue 선언은 IRI
      (``PREFIX`` ``BASE``) 또는 문자열 (SPARQL 1.2 ``VERSION "1.2"``) 로 끝난다. 트리플의
      ``<IRI> load:x ;`` 도 이 모양이라 거부한다.
    """
    following = _TRIVIA.match(query, end).end()
    if keyword == "SERVICE":
        after_graph_keyword = (
            previous is not None
            and previous[0] == "word"
            and previous[1].upper() in _GRAPH_NAME_KEYWORDS
        )
        return query.startswith("{", following) and not after_graph_keyword
    at_operation_start = (
        previous is None or previous == ("other", ";") or previous[0] in ("iri", "string")
    )
    return at_operation_start and _AFTER_LOAD_IRI.match(query, following) is not None


def _find_egress_keyword(query: str) -> str | None:
    """Oxigraph 가 SERVICE 또는 LOAD 키워드로 읽을 수 있는 토큰을 찾아 그 이름을 돌려준다.

    주석, IRI, 문자열 안의 단어와 변수 (``?x`` ``$x``), prefixed name 의 local 부분,
    언어 태그, blank node label 은 키워드가 아니다. 구문 해석 (rdflib 파서) 없이 토큰만
    본다. 키워드가 없으면 None 을 돌려준다.

    ``<`` 는 문맥에 따라 IRI 의 시작이기도 하고 비교 연산자이기도 하다. 두 해석은 뒤따르는
    텍스트를 다르게 끊는다 (``FILTER(1<'>b')SERVICE ...`` 처럼 연산자 해석에서만 보이는
    키워드가 있다). 그래서 IRI 후보마다 두 해석을 모두 따라가고, 어느 한 해석에서라도
    키워드가 보이면 그 키워드를 돌려준다. 비교 연산자 해석은 식 안에 있으므로 짝이 없는
    ``)`` 로 식을 벗어나거나 ``{`` 로 그래프 패턴을 열기 전에는 키워드를 세지 않는다.
    ``<<`` 의 두 번째 ``<`` (RDF-star 인용 트리플) 는 그래프 패턴 안이므로 바로 센다.

    같은 위치에서 같은 토큰열을 읽는 해석은 하나만 남긴다 (키워드를 세는 해석이 세지
    않는 해석을, 식 깊이가 얕은 해석이 깊은 해석을 포함한다). 판정이 바로 앞 토큰에 달린
    토큰 (키워드로 읽을 수 있는 prefixed name) 에서는 해석을 합치지 않는다. 줄 끝과 이름
    문자 구간은 질의마다 한 번 계산해 모든 해석이 함께 쓴다. 그래서 IRI 가 많거나 이름
    문자 구간이 긴 질의에서도 길이에 비례하는 시간 안에 끝난다.
    """
    if not _EGRESS_KEYWORD_HINT.search(query):
        return None
    line_ends = [match.start() for match in re.finditer(r"[\r\n]", query)]
    name_runs = _name_runs(query)
    live_seen: set[int] = set()
    depth_seen: dict[int, int] = {}
    pending: list[tuple[int, bool, int]] = [(0, True, 0)]
    while pending:
        keyword = _scan_reading(
            query, *pending.pop(), line_ends, name_runs, live_seen, depth_seen, pending
        )
        if keyword:
            return keyword
    return None


def _name_runs(query: str) -> tuple[list[int], list[int]]:
    """``[PN_CHARS.]`` 최대 구간의 시작 위치 목록과 끝 위치 목록을 돌려준다."""
    starts: list[int] = []
    ends: list[int] = []
    for match in _NAME_RUN.finditer(query):
        starts.append(match.start())
        ends.append(match.end())
    return starts, ends


def _match_token(
    query: str, pos: int, name_runs: tuple[list[int], list[int]]
) -> tuple[str, int, str]:
    """``pos`` 에서 시작하는 토큰의 종류, 끝 위치, prefix 를 돌려준다.

    prefix 는 prefix 가 있는 prefixed name 에서만 비어 있지 않다. prefix 는 ``pos`` 가 든
    이름 문자 구간의 끝이 ``:`` 이고 그 앞 문자가 ``.`` 가 아닐 때 성립하며, 구간 전체가
    prefix 다.
    """
    if _PN_PREFIX_START.match(query, pos):
        starts, ends = name_runs
        run_end = ends[bisect.bisect_right(starts, pos) - 1]
        if query.startswith(":", run_end) and query[run_end - 1] != ".":
            end = _PNAME_AFTER_PREFIX.match(query, run_end).end()
            return "pname", end, query[pos:run_end]
    match = _LEXICAL_TOKEN.match(query, pos)
    return match.lastgroup, match.end(), ""


def _scan_reading(
    query: str,
    pos: int,
    live: bool,
    depth: int,
    line_ends: list[int],
    name_runs: tuple[list[int], list[int]],
    live_seen: set[int],
    depth_seen: dict[int, int],
    pending: list[tuple[int, bool, int]],
) -> str | None:
    """한 해석을 따라 토큰을 읽는다. IRI 후보를 만나면 연산자 해석을 ``pending`` 에 넣는다.

    ``live`` 가 거짓이면 비교 연산자 식 안이고 ``depth`` 는 그 식의 괄호 깊이다.
    """
    length = len(query)
    previous: tuple[str, str] | None = None if pos == 0 else ("other", "<")
    while pos < length:
        kind, end, prefix = _match_token(query, pos, name_runs)
        if kind == "ws":
            pos = end
            continue
        pname_keyword = _prefix_keyword(prefix) if prefix else None
        if not pname_keyword:
            if pos in live_seen or (not live and depth_seen.get(pos, depth + 1) <= depth):
                return None
            if live:
                live_seen.add(pos)
            else:
                depth_seen[pos] = depth
        if kind == "comment":
            index = bisect.bisect_left(line_ends, pos)
            pos = line_ends[index] if index < len(line_ends) else length
            continue
        if kind == "open_string":
            return None
        text = query[pos:end]
        if kind == "iri":
            nested = pos > 0 and query[pos - 1] == "<"
            pending.append((pos + 1, nested, 0 if live or nested else depth))
        elif kind == "word" and live:
            keyword = _egress_keyword_at_start(text)
            if keyword:
                return keyword
        elif pname_keyword and live:
            if _keyword_pname_is_egress(query, pname_keyword, end, previous):
                return pname_keyword
        elif kind == "other" and not live:
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
                live = depth < 0
            elif text == "{":
                live = True
        previous = (kind, text)
        pos = end
    return None


def _lexical_service_keyword(query: str) -> bool:
    """문자열, IRI, 주석 밖에 SERVICE 또는 LOAD 키워드가 있는지 본다.

    rdflib 구문 트리 판정과 독립된 두 번째 판정이다. 실행 store (Oxigraph) 는 자체
    파서를 쓰므로, 한 파서의 해석에만 기대면 두 파서가 다르게 끊는 입력이 가드를
    우회할 수 있다. 판정 규칙은 ``_find_egress_keyword`` 하나이며 store 차단점
    (``install_sparql_egress_chokepoint``) 도 같은 함수를 쓴다.
    """
    return _find_egress_keyword(query) is not None


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


# ── 프로세스 전체 차단점: rdflib Graph.query / Graph.update 가 원격에 닿지 않게 한다 ──
#
# 위 가드는 호출자가 직접 부르는 검사다. 아래 차단점은 호출 위치와 무관하게 실행 경로에
# 설치된다. 실행 경로는 두 갈래다.
# - Oxigraph store: ``OxigraphStore.query`` / ``.update`` 가 최종 문자열을 받아 Rust
#   엔진에 넘긴다. 넘기기 전에 ``_find_egress_keyword`` 로 검사한다. 구문 해석은 하지
#   않는다 (내부 질의는 RDF-star 같은 Oxigraph 전용 구문을 쓸 수 있고, 큰 VALUES 질의는
#   rdflib 해석 비용이 크다).
# - rdflib 평가기: Memory store 와, Oxigraph store 가 해석된 Query 객체를 거절해 rdflib 이
#   대신 평가하는 경우다. 원격 요청 함수 (SERVICE 평가, LOAD 평가) 를 거부 함수로 바꾸고
#   FROM / FROM NAMED / USING 의 원격 그래프 로드를 끈다.

#: 차단점이 감싼 store 메서드에 붙이는 표지 (반복 설치 방지).
_CHOKEPOINT_MARK = "_sparql_egress_chokepoint"


def _reject_store_egress(query: str) -> None:
    """store 에 넘길 최종 문자열에 SERVICE / LOAD 키워드가 있으면 ValueError 를 던진다."""
    keyword = _find_egress_keyword(query)
    if keyword:
        raise ValueError(
            f"{_EGRESS_MESSAGE} 실행 직전 검사에서 {keyword} 키워드를 발견해 실행하지 않습니다."
        )


def _refuse_service_evaluation(ctx, part):
    """rdflib 평가기의 SERVICE 원격 질의를 대신한다. 항상 거부한다."""
    raise ValueError(f"{_EGRESS_MESSAGE} rdflib 평가기의 SERVICE 원격 질의를 차단했습니다.")


def _refuse_load_evaluation(ctx, u):
    """rdflib 평가기의 LOAD 를 대신한다. 항상 거부한다.

    ``LOAD SILENT`` 는 rdflib 이 이 예외를 삼키므로 오류 없이 아무것도 적재하지 않는다.
    """
    raise ValueError(f"{_EGRESS_MESSAGE} rdflib 평가기의 LOAD 를 차단했습니다.")


def _guard_store_method(method):
    """문자열 인자를 ``_reject_store_egress`` 로 검사한 뒤 원래 메서드에 넘기는 래퍼."""

    @functools.wraps(method)
    def guarded(self, query_or_update, *args, **kwargs):
        if isinstance(query_or_update, str):
            _reject_store_egress(query_or_update)
        return method(self, query_or_update, *args, **kwargs)

    setattr(guarded, _CHOKEPOINT_MARK, True)
    return guarded


def _oxigraph_store_class():
    """``Graph(store="Oxigraph")`` 가 만드는 store 클래스. 플러그인이 없으면 None."""
    try:
        return plugin.get("Oxigraph", Store)
    except plugin.PluginException:
        return None


def install_sparql_egress_chokepoint() -> None:
    """이 프로세스의 rdflib SPARQL 실행 경로에서 원격 요청을 막는다.

    여러 번 호출해도 한 번 설치한 것과 같다. ``domain`` 패키지 import 시 호출된다.
    """
    _rdflib_sparql.SPARQL_LOAD_GRAPHS = False
    _rdflib_evaluate.evalServiceQuery = _refuse_service_evaluation
    _rdflib_update.evalLoad = _refuse_load_evaluation
    store_class = _oxigraph_store_class()
    if store_class is None:
        return
    for name in ("query", "update"):
        method = getattr(store_class, name)
        if not getattr(method, _CHOKEPOINT_MARK, False):
            setattr(store_class, name, _guard_store_method(method))


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
