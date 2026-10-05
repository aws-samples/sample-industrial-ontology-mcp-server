"""S12 질의 테스트의 HTML 이스케이프와 CQ 도메인 보간 검증 회귀 테스트.

- 보고서: CQ 의 id, 질문, 난이도, 체크·실패 문구는 텍스트로만 렌더링된다.
- 도메인: 로컬 이름 형식이 아닌 CQ domains 값은 SPARQL 에 보간되지 않고 실패로
  기록된다. 실행되는 모든 질의는 egress 가드를 통과한 문자열이다.

네트워크 호출은 하지 않는다. 산출물은 tmp_path 에만 쓴다.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import RDF

from domain.namespaces import DOMAIN_NS, NS_PREFIX

#: 보고서 템플릿이 스스로 만드는 태그. 이 밖의 태그가 나오면 입력이 마크업으로 해석된 것이다.
_TEMPLATE_TAGS = {
    "html", "head", "meta", "title", "style", "body", "h1", "div",
    "strong", "br", "table", "tr", "th", "td",
}

_SCRIPT = "<script>globalThis.__prepublic_probe=1</script>"
_IMG = "<img src=x onerror=alert(document.domain)>"
_SVG = "<svg onload=alert(1)>"


class _TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(name for name, _ in attrs)


def _parse(html_text: str) -> _TagCollector:
    parser = _TagCollector()
    parser.feed(html_text)
    return parser


def _malicious_result() -> dict:
    return {
        "id": f"CQ01{_IMG}",
        "status": "FAIL",
        "question": _SCRIPT,
        "difficulty": _SVG,
        "domains": ["A"],
        "checks": [
            {"check": f"check {_IMG}", "passed": False, "detail": f"detail {_SCRIPT}"},
            {"check": "<b>bold</b>", "passed": True, "detail": "<i>it</i>"},
        ],
        "checks_passed": 1,
        "checks_total": 2,
        "failures": [f"failure {_SVG}", "<a href=javascript:alert(1)>x</a>"],
    }


def test_report_renders_cq_values_as_text():
    """보간한 모든 CQ 값은 이스케이프되어 요소나 이벤트 속성을 만들지 않는다."""
    from tools.query_test import _generate_cq_report_html

    summary = {"total": 1, "passed": 0, "failed": 1, "pass_rate": 0.0,
               "duration_seconds": 0.1}
    html_text = _generate_cq_report_html([_malicious_result()], summary)
    parsed = _parse(html_text)

    assert set(parsed.tags) <= _TEMPLATE_TAGS, sorted(set(parsed.tags) - _TEMPLATE_TAGS)
    assert not [a for a in parsed.attrs if a.startswith("on")]
    assert "&lt;script&gt;globalThis.__prepublic_probe=1&lt;/script&gt;" in html_text
    assert "&lt;svg onload=alert(1)&gt;" in html_text
    assert "&lt;b&gt;bold&lt;/b&gt;" in html_text


def test_report_truncates_question_before_escaping():
    """질문은 60자로 자른 뒤 이스케이프해 엔티티가 잘리지 않는다."""
    from tools.query_test import _generate_cq_report_html

    question = "가" * 58 + "<&>"
    result = dict(_malicious_result(), question=question, id="CQ02",
                  difficulty="easy", checks=[], failures=[],
                  checks_passed=0, checks_total=0)
    summary = {"total": 1, "passed": 0, "failed": 1, "pass_rate": 0.0,
               "duration_seconds": 0.1}

    html_text = _generate_cq_report_html([result], summary)

    assert "가" * 58 + "&lt;&amp;</td>" in html_text


def test_report_keeps_legit_values():
    from tools.query_test import _generate_cq_report_html

    result = {
        "id": "CQ01", "status": "PASS", "question": "설비별 알람 건수는?",
        "difficulty": "easy", "domains": ["A"],
        "checks": [{"check": "A ↔ B 연결 확인", "passed": True, "detail": "연결됨"}],
        "checks_passed": 1, "checks_total": 1, "failures": [],
    }
    summary = {"total": 1, "passed": 1, "failed": 0, "pass_rate": 100.0,
               "duration_seconds": 0.1}

    html_text = _generate_cq_report_html([result], summary)

    assert "<td>CQ01</td>" in html_text
    assert "설비별 알람 건수는?" in html_text
    assert "A ↔ B 연결 확인 — 연결됨" in html_text
    assert "100.0%" in html_text


# ── CQ domains 보간 ─────────────────────────────────────────────────────

#: 작업 목록에 기록된 payload. 클래스 이름 자리에 SERVICE 절을 덧붙인다.
_DOMAIN_PAYLOAD = "EquipmentMaster . SERVICE <https://example.com/sparql> { ?s ?p ?o }"


@pytest.mark.parametrize(
    "domain",
    [_DOMAIN_PAYLOAD, "Equipment Master", "Equipment}", "Eq<x>", "a.b", "", 7, None],
)
def test_resolve_cq_class_names_rejects_non_local_names(domain):
    from tools.query_test import _resolve_cq_class_names

    names, rejected = _resolve_cq_class_names([domain])

    assert names == []
    assert len(rejected) == 1
    assert rejected[0]["passed"] is False
    assert rejected[0]["_invalid_domain"] is True


def test_resolve_cq_class_names_keeps_local_names():
    from tools.query_test import _resolve_cq_class_names

    names, rejected = _resolve_cq_class_names(
        ["EquipmentMaster", "ghg_emission", "GHG_Emission", "설비_마스터", "Work-Order"]
    )

    assert names == ["EquipmentMaster", "GhgEmission", "GHGEmission", "설비마스터", "Work-Order"]
    assert rejected == []


class _RecordingGraph:
    """실행된 질의를 기록하고 실제 질의는 메모리 그래프에 위임한다."""

    def __init__(self, graph: Graph):
        self._graph = graph
        self.queries: list[str] = []

    def query(self, query):
        self.queries.append(query)
        return self._graph.query(query)

    def __len__(self):
        return len(self._graph)


@pytest.fixture
def _no_outbound(monkeypatch):
    import rdflib.plugins.sparql.evaluate as evaluate

    def _refuse(*_args, **_kwargs):
        raise AssertionError("로컬 SPARQL 이 원격 요청을 시도함")

    monkeypatch.setattr(evaluate, "urlopen", _refuse)


def _fallback_env(tmp_path, monkeypatch, cqs: list[dict]) -> _RecordingGraph:
    """abox_stats 가 없는 SPARQL 폴백 경로를 tmp_path 안에서 구성한다."""
    from tools import query_test as module

    query_dir = tmp_path / "query_tests"
    query_dir.mkdir()
    (query_dir / "cases.json").write_text(
        json.dumps(cqs, ensure_ascii=False), encoding="utf-8",
    )

    ns = Namespace(str(DOMAIN_NS))
    g = Graph()
    g.bind(NS_PREFIX, ns)
    g.add((ns.EQ001, RDF.type, ns.EquipmentMaster))
    recording = _RecordingGraph(g)

    monkeypatch.setattr(module, "SOURCE_QUERY_TESTS_DIR", str(query_dir))
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH",
                        str(tmp_path / "cq_feedback.json"))
    monkeypatch.setattr(module, "_load_semantic_dict", lambda: {})
    monkeypatch.setattr(module, "_load_abox_stats", lambda: None)
    monkeypatch.setattr("tools.sparql_local._get_graph",
                        lambda _source: (recording, "test"))
    return recording


def test_domain_payload_is_not_interpolated(tmp_path, monkeypatch, _no_outbound):
    """SERVICE 를 덧붙인 domain 값은 질의에 들어가지 않고 실패로 기록된다."""
    from tools import query_test as module

    recording = _fallback_env(tmp_path, monkeypatch, [{
        "id": "CQ01",
        "question_ko": _SCRIPT,
        "difficulty": "easy",
        "domains": ["EquipmentMaster", _DOMAIN_PAYLOAD],
    }])

    result = json.loads(module.test_domain_queries(
        test_cases_path="cases.json", verify_joins=False,
    ))

    assert result["success"] is True
    assert recording.queries, "정상 도메인의 폴백 질의는 실행돼야 한다"
    assert all("SERVICE" not in q.upper() for q in recording.queries)
    cq = result["results"][0]
    assert cq["status"] == "FAIL"
    assert any("CQ 도메인 이름 형식 확인" in f for f in cq["failures"])
    assert any(s["type"] == "invalid_domain" for s in result["improvement_suggestions"])
    # 정상 도메인의 인스턴스 체크는 그대로 수행된다.
    assert not any("EquipmentMaster 인스턴스 존재 확인" in f for f in cq["failures"])

    report = (tmp_path / "reports" / "query_test_report.html").read_text(encoding="utf-8")
    parsed = _parse(report)
    assert set(parsed.tags) <= _TEMPLATE_TAGS


def test_legit_fallback_cq_still_passes(tmp_path, monkeypatch, _no_outbound):
    from tools import query_test as module

    recording = _fallback_env(tmp_path, monkeypatch, [{
        "id": "CQ01", "question_ko": "설비 목록은?", "difficulty": "easy",
        "domains": ["EquipmentMaster"],
    }])

    result = json.loads(module.test_domain_queries(
        test_cases_path="cases.json", verify_joins=False,
    ))

    assert result["success"] is True
    assert result["results"][0]["status"] == "PASS"
    assert len(recording.queries) == 1


def test_verify_class_join_rejects_non_local_names():
    """조인 검증도 보간 전에 클래스·OP 이름 형식을 확인한다."""
    from tools.query_test import _verify_class_join

    class _NoQuery:
        def query(self, _q):
            raise AssertionError("형식이 아닌 이름으로 질의가 실행됨")

    with pytest.raises(ValueError):
        _verify_class_join(_NoQuery(), "ClassA", "p } SERVICE <https://example.com/sparql> { ?s ?p ?o", "ClassB",
                           ns_prefix="steel", ns_uri=str(DOMAIN_NS))
    with pytest.raises(ValueError):
        _verify_class_join(_NoQuery(), _DOMAIN_PAYLOAD, "linksTo", "ClassB",
                           ns_prefix="steel", ns_uri=str(DOMAIN_NS))


def test_run_local_query_guards_before_execution():
    from tools.query_test import _run_local_query

    class _NoQuery:
        def query(self, _q):
            raise AssertionError("가드보다 질의가 먼저 실행됨")

    with pytest.raises(ValueError, match="SERVICE"):
        _run_local_query(
            _NoQuery(),
            "SELECT ?s WHERE { FILTER(1<2) SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
        )
