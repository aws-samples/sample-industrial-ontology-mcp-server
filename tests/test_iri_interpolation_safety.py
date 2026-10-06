"""설정 네임스페이스와 그래프 IRI 를 SPARQL 텍스트에 넣는 지점의 회귀 테스트.

- ``domain.namespaces`` 는 로드 시점에 ``namespace`` 설정을 검증한다. IRI 값은 스킴이 있는
  절대 IRI 이면서 IRIREF 와 큰따옴표 문자열 리터럴 어느 쪽의 경계도 바꾸지 못하는 문자만
  쓸 수 있고, prefix 는 SPARQL ``PN_PREFIX`` 형식이어야 한다.
- ``sparql_iri`` 는 그래프에서 읽은 IRI 를 같은 문자 규칙으로 확인한 뒤 ``<...>`` 항으로
  만든다.
- 검증 check, 추론 후처리, 딕셔너리 통계, 교차 저장소 비교는 이 두 경로를 거친 값만
  질의에 넣는다. 거부 경로에서는 주입 문자열이 그래프에 전달되지 않았는지 기록으로
  확인한다.

기본 Oxigraph store 는 이런 IRI 를 적재 단계에서 거부하므로 그래프는 메모리 store 를
쓴다. 네트워크에는 접속하지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from rdflib import OWL, RDF, XSD, BNode, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, sparql_iri
from domain.rules_paths import rules_path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NAMESPACES_FILE = _REPO_ROOT / "domain" / "namespaces.py"
_CROSS_STORE_FILE = _REPO_ROOT / "tools" / "cross_store_validation.py"

#: 질의 구조를 바꾸는 주입 표식. 이 문자열이 그래프에 전달되면 주입이 실행된 것이다.
_MARKER = "FILTER(false)"


def _exec_isolated(path: Path, name: str):
    """모듈 파일을 ``sys.modules`` 에 등록하지 않고 새로 실행한다."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingGraph:
    """실행된 질의와 갱신 문자열을 기록하고 메모리 그래프에 위임한다."""

    def __init__(self, graph: Graph):
        self._graph = graph
        self.queries: list[str] = []

    def query(self, query, *args, **kwargs):
        self.queries.append(str(query))
        return self._graph.query(query, *args, **kwargs)

    def update(self, update, *args, **kwargs):
        self.queries.append(str(update))
        return self._graph.update(update, *args, **kwargs)

    def __len__(self) -> int:
        return len(self._graph)

    def __getattr__(self, name):
        return getattr(self._graph, name)


# ── 로드 시점 네임스페이스 검증 ─────────────────────────


_VALID_NAMESPACE = {
    "ontology_uri": "https://example.org/onto",
    "class_ns": "https://example.org/onto#",
    "instance_ns": "https://example.org/onto/instances#",
    "prefix": "ex",
    "instance_prefix": "ex-inst",
}


def _load_namespaces_with(monkeypatch, tmp_path: Path, namespace: dict):
    config_path = tmp_path / "domain_config.json"
    config_path.write_text(json.dumps({"namespace": namespace}), encoding="utf-8")
    monkeypatch.setenv("DOMAIN_CONFIG_PATH", str(config_path))
    return _exec_isolated(_NAMESPACES_FILE, "_isolated_domain_namespaces")


@pytest.mark.parametrize("config_name", ["domain_config.json", "domain_config.example.json"])
def test_shipped_domain_configs_load_unchanged(monkeypatch, config_name):
    """동봉 설정은 그대로 로드되고 값이 바뀌지 않는다."""
    path = rules_path(config_name)
    monkeypatch.setenv("DOMAIN_CONFIG_PATH", path)
    module = _exec_isolated(_NAMESPACES_FILE, "_isolated_domain_namespaces")
    with open(path, encoding="utf-8") as fh:
        namespace = json.load(fh)["namespace"]
    loaded = {
        "class_ns": module.DOMAIN_NS,
        "instance_ns": module.DOMAIN_INST_NS,
        "ontology_uri": module.ONTOLOGY_URI,
        "prefix": module.NS_PREFIX,
        "instance_prefix": module.NS_INST_PREFIX,
    }
    assert loaded == {key: namespace[key] for key in loaded}


def test_valid_custom_namespace_loads(monkeypatch, tmp_path):
    module = _load_namespaces_with(monkeypatch, tmp_path, dict(_VALID_NAMESPACE))
    assert module.DOMAIN_NS == "https://example.org/onto#"
    assert "PREFIX ex-inst: <https://example.org/onto/instances#>" in module.SPARQL_PREFIXES


_MALICIOUS_NAMESPACE_VALUES = [
    pytest.param("class_ns", 'https://example.org/onto#") || true || ("', id="class_ns-quote"),
    pytest.param(
        "class_ns",
        "https://example.org/onto> } SERVICE <http://127.0.0.1:65530/> { ?s ?p ?o } #",
        id="class_ns-iri-close",
    ),
    pytest.param("class_ns", "https://example.org/onto#\n", id="class_ns-newline"),
    pytest.param("class_ns", "https://example.org/on to#", id="class_ns-space"),
    pytest.param("class_ns", "https://example.org/onto\\u0022#", id="class_ns-backslash"),
    pytest.param("class_ns", "example.org/onto#", id="class_ns-relative"),
    pytest.param("class_ns", 42, id="class_ns-not-string"),
    pytest.param("instance_ns", "https://example.org/inst{x}#", id="instance_ns-brace"),
    pytest.param("instance_ns", "https://example.org/inst\x85#", id="instance_ns-c1-control"),
    pytest.param("ontology_uri", "https://example.org/onto|x", id="ontology_uri-pipe"),
    pytest.param("prefix", "ex: <urn:x> PREFIX y", id="prefix-injection"),
    pytest.param("instance_prefix", "ex inst", id="instance_prefix-space"),
]


@pytest.mark.parametrize(("key", "value"), _MALICIOUS_NAMESPACE_VALUES)
def test_malicious_namespace_config_is_rejected_at_load(monkeypatch, tmp_path, key, value):
    namespace = dict(_VALID_NAMESPACE, **{key: value})
    with pytest.raises(RuntimeError, match=rf"namespace\.{key}"):
        _load_namespaces_with(monkeypatch, tmp_path, namespace)


# ── sparql_iri 헬퍼 ───────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        URIRef("https://example.org/onto#Class"),
        "urn:uuid:6f1c0c2e-0000-4000-8000-00000000abcd",
        "https://example.org/경로#클래스",
    ],
)
def test_sparql_iri_wraps_valid_iri(value):
    assert sparql_iri(value) == f"<{value}>"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org/a>b",
        "https://example.org/a<b",
        'https://example.org/a"b',
        "https://example.org/a{b",
        "https://example.org/a}b",
        "https://example.org/a|b",
        "https://example.org/a^b",
        "https://example.org/a`b",
        "https://example.org/a\\b",
        "https://example.org/a b",
        "https://example.org/a\nb",
        "https://example.org/a\x00b",
        "https://example.org/a\x7fb",
        "",
    ],
)
def test_sparql_iri_rejects_forbidden_characters(value):
    with pytest.raises(ValueError):
        sparql_iri(value)


@pytest.mark.parametrize(
    "value", [BNode("b0"), Literal("https://example.org/a"), None, 7],
)
def test_sparql_iri_rejects_non_iri_terms(value):
    with pytest.raises(ValueError):
        sparql_iri(value)


# ── 검증 check 실행 지점 ──────────────────────────────


def _injected_iri(base: str, local: str) -> URIRef:
    """``<base+local>`` 자리에서 IRI 를 닫고 ``FILTER(false)`` 를 끼워 넣는 IRI."""
    return URIRef(f"{base}{local}> }} {_MARKER} VALUES ?q {{ <urn:q")


def test_fk_referential_integrity_rejects_injected_op_before_query():
    from tools.validation_support.checks.referential import check_fk_referential_integrity

    tbox = Graph()
    tbox.add((_injected_iri(DOMAIN_NS, "hasX"), RDF.type, OWL.ObjectProperty))
    data = _RecordingGraph(Graph())

    with pytest.raises(ValueError):
        check_fk_referential_integrity(data, tbox)
    assert data.queries == []


def test_closed_world_fk_unresolved_rejects_injected_op_before_query(monkeypatch):
    from tools.validation_support.checks import referential

    monkeypatch.setattr(
        referential, "load_master_instance_uris",
        lambda _path=None: {URIRef(f"{DOMAIN_INST_NS}Master_1")},
    )
    tbox = Graph()
    tbox.add((_injected_iri(DOMAIN_NS, "hasX"), RDF.type, OWL.ObjectProperty))
    data = _RecordingGraph(Graph())

    with pytest.raises(ValueError):
        referential.check_closed_world_fk_unresolved(data, tbox)
    assert not any(_MARKER in q for q in data.queries)


def test_closed_world_master_orphan_falls_back_without_sending_bad_iri(monkeypatch):
    """질의에 넣을 수 없는 master IRI 는 기존 Python 순회 경로로 판정한다."""
    from tools.validation_support.checks import referential

    good = URIRef(f"{DOMAIN_INST_NS}Master_1")
    bad = _injected_iri(DOMAIN_INST_NS, "Master_2")
    graph = Graph()
    graph.add((URIRef(f"{DOMAIN_INST_NS}Tx_1"), URIRef(f"{DOMAIN_NS}refersTo"), good))
    data = _RecordingGraph(graph)
    monkeypatch.setattr(referential, "load_master_instance_uris", lambda _path=None: {good, bad})
    monkeypatch.setenv("CW_MASTER_ORPHAN_THRESHOLD", "1.0")

    result = referential.check_closed_world_master_orphan(data, None)

    assert not any(_MARKER in q for q in data.queries)
    assert result["master_total"] == 2
    assert result["orphan_count"] == 1


def test_functional_violations_rejects_injected_property_before_query():
    from tools.validation_support.checks.temporal_cardinality import check_functional_violations

    tbox = Graph()
    tbox.add((_injected_iri(DOMAIN_NS, "fp"), RDF.type, OWL.FunctionalProperty))
    data = _RecordingGraph(Graph())

    with pytest.raises(ValueError):
        check_functional_violations(data, tbox)
    assert data.queries == []


#: A-Box 술어 local name 에서 IRI 를 닫고 ``FILTER(false)`` 를 끼운 뒤 남은 ``>`` 를
#: 새 IRI 로 흡수한다. 검증이 없으면 문법상 유효한 질의가 된다.
_INJECTED_LOCAL = f"r> ?val . {_MARKER} ?s <urn:q"


def test_numeric_outliers_rejects_injected_predicate_before_query():
    from tools.validation_support.checks.statistical import check_numeric_outliers

    graph = Graph()
    pred = URIRef(f"{DOMAIN_NS}{_INJECTED_LOCAL}")
    for i, value in enumerate((1, 2, 3, 4)):
        graph.add((URIRef(f"{DOMAIN_INST_NS}S_{i}"), pred, Literal(value, datatype=XSD.integer)))
    data = _RecordingGraph(graph)

    with pytest.raises(ValueError):
        check_numeric_outliers(data)
    assert not any(_MARKER in q for q in data.queries)


def test_string_patterns_rejects_injected_predicate_before_query():
    from tools.validation_support.checks.statistical import check_string_patterns

    graph = Graph()
    pred = URIRef(f"{DOMAIN_NS}{_INJECTED_LOCAL}")
    for i in range(6):
        graph.add((URIRef(f"{DOMAIN_INST_NS}S_{i}"), pred, Literal("same")))
    data = _RecordingGraph(graph)

    with pytest.raises(ValueError):
        check_string_patterns(data)
    assert not any(_MARKER in q for q in data.queries)


# ── 추론 후처리 ───────────────────────────────────────


def test_owl_restrictions_reject_bad_class_iri_before_update():
    """``n3()`` 가 통과시키는 제어 문자 IRI 도 갱신 문자열에 들어가지 않는다."""
    from tools.inference import _apply_owl_restrictions

    cls = URIRef(f"{DOMAIN_NS}Bad\nClass")
    restriction = BNode()
    tbox = Graph()
    tbox.add((cls, RDF.type, OWL.Class))
    tbox.add((cls, OWL.equivalentClass, restriction))
    tbox.add((restriction, RDF.type, OWL.Restriction))
    tbox.add((restriction, OWL.onProperty, URIRef(f"{DOMAIN_NS}hasPart")))
    tbox.add((restriction, OWL.someValuesFrom, URIRef(f"{DOMAIN_NS}Part")))
    data = _RecordingGraph(Graph())

    with pytest.raises(ValueError):
        _apply_owl_restrictions(data, tbox)
    assert data.queries == []


def test_owl_restrictions_keep_valid_iri_behaviour():
    from tools.inference import _apply_owl_restrictions

    cls = URIRef(f"{DOMAIN_NS}Assembly")
    part_cls = URIRef(f"{DOMAIN_NS}Part")
    has_part = URIRef(f"{DOMAIN_NS}hasPart")
    restriction = BNode()
    tbox = Graph()
    tbox.add((cls, RDF.type, OWL.Class))
    tbox.add((cls, OWL.equivalentClass, restriction))
    tbox.add((restriction, RDF.type, OWL.Restriction))
    tbox.add((restriction, OWL.onProperty, has_part))
    tbox.add((restriction, OWL.someValuesFrom, part_cls))
    graph = Graph()
    inst = URIRef(f"{DOMAIN_INST_NS}Assembly_1")
    part = URIRef(f"{DOMAIN_INST_NS}Part_1")
    graph.add((inst, has_part, part))
    graph.add((part, RDF.type, part_cls))

    stats = _apply_owl_restrictions(graph, tbox)

    assert stats["someValuesFrom"] == 1
    assert (inst, RDF.type, cls) in graph


# ── 딕셔너리 통계 ─────────────────────────────────────


def test_abox_stats_namespace_stays_inside_string_literal():
    """네임스페이스 인자는 문자열 리터럴 밖으로 나가 FILTER 를 바꾸지 못한다."""
    from tools.semantic_dictionary import _single_pass_abox_stats

    graph = Graph()
    graph.add((
        URIRef("urn:other:s"), URIRef("urn:other#hasX"), URIRef("urn:other:o"),
    ))
    data = _RecordingGraph(graph)

    _ic, _pv, op_counts = _single_pass_abox_stats(
        data, 'urn:nomatch#") || true || STRSTARTS("x", "', {}, {"hasX"},
    )

    assert op_counts == {}


# ── 교차 저장소 비교 ──────────────────────────────────


def test_cross_store_instance_filter_uses_validated_namespace(monkeypatch):
    """인스턴스 수 질의는 검증된 DOMAIN_INST_NS 만 쓰고 임의 환경변수를 읽지 않는다."""
    payload = 'urn:x") || true || ("'
    monkeypatch.setenv("DOMAIN_INST_NS_OBJ", payload)
    module = _exec_isolated(_CROSS_STORE_FILE, "_isolated_cross_store_validation")

    query = next(q for q in module._QUERIES if q["id"] == "typed_instance_count")["sparql"]

    assert payload not in query
    assert f'STRSTARTS(STR(?i), "{DOMAIN_INST_NS}")' in query
