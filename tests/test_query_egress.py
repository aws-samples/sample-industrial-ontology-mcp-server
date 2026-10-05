"""로컬 질의 도구의 네트워크 egress 차단 회귀 테스트."""

from __future__ import annotations

import json

import pytest


class _FailIfQueried:
    """egress 가드 전에 질의 실행으로 진입하면 실패시키는 그래프 대역."""

    def query(self, _query):
        raise AssertionError("egress 가드보다 그래프 질의가 먼저 실행됨")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { SERVICE <https://example.com/sparql> { ?s ?p ?o } }",
        "SELECT * FROM <https://example.com/data> WHERE { ?s ?p ?o }",
        "SELECT * FROM NAMED <https://example.com/data> WHERE { GRAPH ?g { ?s ?p ?o } }",
    ],
)
def test_sparql_local_rejects_egress_clauses(query, monkeypatch):
    """SERVICE와 FROM 계열 구문은 그래프 접근 전에 거부한다."""
    from tools import sparql_local as module

    monkeypatch.setattr(module, "_get_graph", lambda _source: (_FailIfQueried(), "test"))

    result = json.loads(module.sparql_local(query))

    assert result["success"] is False
    assert "SERVICE" in result["error"]
    assert "FROM" in result["error"]


@pytest.mark.parametrize(
    "query",
    [
        "select * where { service <https://example.com/sparql> { ?s ?p ?o } }",
        "SELECT *\nFROM\nNAMED\n<https://example.com/data>\nWHERE { ?s ?p ?o }",
    ],
)
def test_test_domain_queries_rejects_embedded_egress_queries(
    query,
    tmp_path,
    monkeypatch,
):
    """질의 테스트 JSON에 포함된 원격 SPARQL도 실행 경로 진입 전에 거부한다."""
    from tools import query_test as module

    query_dir = tmp_path / "query_tests"
    report_dir = tmp_path / "reports"
    query_dir.mkdir()
    report_dir.mkdir()
    cases = [
        {
            "id": "CQ01",
            "question_en": "Which records match?",
            "domains": [],
            "golden_sparql": query,
        }
    ]
    (query_dir / "cases.json").write_text(json.dumps(cases), encoding="utf-8")

    monkeypatch.setattr(module, "SOURCE_QUERY_TESTS_DIR", str(query_dir), raising=False)
    monkeypatch.setattr(module, "_CQ_PATH", str(query_dir / "cases.json"))
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_load_semantic_dict", lambda: {})
    monkeypatch.setattr(module, "_load_abox_stats", lambda: {"per_class": {}})

    result = json.loads(
        module.test_domain_queries(
            test_cases_path="cases.json",
            open_report=False,
            verify_joins=False,
        )
    )

    assert result["success"] is False
    assert "SERVICE" in result["error"]
    assert "FROM" in result["error"]


@pytest.mark.parametrize(
    "query",
    [
        'SELECT ?value WHERE { VALUES ?value { "SERVICE FROM NAMED" } } # SERVICE',
        'SELECT ?from WHERE { BIND("local" AS ?from) }',
        'PREFIX ex: <https://example.com/local#> SELECT ?s WHERE { ?s ex:service ?o }',
    ],
)
def test_query_keywords_outside_clause_positions_are_allowed(query, monkeypatch):
    """문자열, 주석, 변수, prefixed name의 단어는 egress로 오인하지 않는다."""
    from tools import sparql_local as module

    class _EmptySelectResult:
        type = "SELECT"
        vars = []

        def __iter__(self):
            return iter(())

    class _Graph:
        def query(self, _query):
            return _EmptySelectResult()

    monkeypatch.setattr(module, "_get_graph", lambda _source: (_Graph(), "test"))

    result = json.loads(module.sparql_local(query))

    assert result["success"] is True
