"""GraphDB·Neo4j 원격 저장소 도구의 기본값과 입력 경계 회귀 테스트.

실제 GraphDB·Neo4j 서버에 접속하지 않는다. HTTP 와 driver 는 모두 기록 대역이다.

1. ``graphdb_run_inference`` 는 기본값으로 기존 repository 를 지우거나 바꾸지 않는다.
   ``overwrite_repo=True`` 일 때만 삭제 후 재생성한다.
2. ``tbox_path``·``abox_path`` 는 존재 확인·크기 조회·GraphDB 요청보다 먼저 DATA_DIR
   안의 RDF 파일로 제한된다.
3. repository ID, ruleset, label 은 REST 경로와 SPARQL·Turtle 구문을 벗어나지 못한다.
4. Neo4j Cypher 힌트에는 식별자로 그대로 쓸 수 없는 이름을 넣지 않고, 쓰기 세션은
   명시적 적재 경로에만 있다.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from rdflib import Graph, Literal, URIRef
from rdflib.plugins.sparql.parser import parseUpdate

_REJECT_PHRASES = ("허용된 데이터 디렉터리 밖", "허용 확장자")
_REPO = "kg"


# ── GraphDB 대역 ────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload=None):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield b""


class _FakeGraphDB:
    """GraphDB REST 호출을 기록한다. ``/statements`` update 는 기록 뒤 중단시킨다.

    ruleset 전환 이후 단계(reinfer, export)는 이 파일의 범위가 아니므로 첫 update
    요청에서 연결 오류를 내 추론 잡을 끝낸다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.existing_ids: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        return _Resp(payload=[{"id": rid} for rid in self.existing_ids])

    def delete(self, url, **_kwargs):
        self.calls.append(("DELETE", url))
        return _Resp()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs.get("data"), kwargs.get("files")))
        if url.endswith("/statements"):
            raise requests.exceptions.ConnectionError("stop after first update")
        return _Resp()

    def request(self, method, url, **kwargs):
        # graphdb_import_file 의 파일 업로드. 열린 파일 이름으로 무엇을 보냈는지 기록한다.
        self.calls.append(("IMPORT", url, method, getattr(kwargs.get("data"), "name", None)))
        return _Resp()

    def remote_calls(self, *methods: str) -> list[tuple]:
        return [c for c in self.calls if c[0] in methods]


@pytest.fixture
def gdb_env(tmp_path, monkeypatch):
    """DATA_DIR·기본 경로를 tmp 로 옮기고 GraphDB HTTP 를 기록 대역으로 바꾼다."""
    import config
    from tools.remote import graphdb as gdb

    data_dir = tmp_path / "data"
    inferred = data_dir / "generated" / "inferred"
    inferred.mkdir(parents=True)
    monkeypatch.setattr(gdb, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(gdb, "GRAPHDB_REPOSITORY", _REPO)
    monkeypatch.setattr(config, "INFERRED_PATH", str(inferred / "all_inferred.ttl"))
    # 기본 T-Box·A-Box·tacit 은 없는 경로라 import 대상이 되지 않는다.
    monkeypatch.setattr(config, "TBOX_PATH", str(data_dir / "generated" / "tbox" / "t_box.ttl"))
    monkeypatch.setattr(config, "ABOX_PATH", str(data_dir / "generated" / "abox" / "a_box.ttl"))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(data_dir / "source" / "tacit"))

    fake = _FakeGraphDB()
    monkeypatch.setattr(gdb.requests, "get", fake.get)
    monkeypatch.setattr(gdb.requests, "delete", fake.delete)
    monkeypatch.setattr(gdb.requests, "post", fake.post)
    monkeypatch.setattr(gdb.requests, "request", fake.request)

    def _health() -> str:
        fake.calls.append(("HEALTH",))
        return "GraphDB OK"

    monkeypatch.setattr(gdb, "graphdb_health", _health)
    return gdb, fake, data_dir


# ── 1. overwrite_repo 기본값 ─────────────────────────────────────────


def test_overwrite_repo_defaults_to_false_on_both_entry_points():
    from tools.remote import graphdb as gdb

    for fn in (gdb.graphdb_run_inference, gdb._graphdb_run_inference_sync):
        assert inspect.signature(fn).parameters["overwrite_repo"].default is False
    assert inspect.signature(gdb.graphdb_create_repository).parameters["overwrite"].default is False


def test_default_run_keeps_existing_repository_untouched(gdb_env):
    """기본 호출은 같은 ID 의 기존 repository 를 지우지도, 데이터를 덧씌우지도 않는다."""
    gdb, fake, _ = gdb_env
    fake.existing_ids = [_REPO]

    result = json.loads(gdb._graphdb_run_inference_sync())

    assert result["success"] is False
    assert "overwrite_repo=True" in result["hint"]
    assert fake.remote_calls("DELETE", "POST", "IMPORT") == []


def test_public_default_dispatches_a_worker_that_does_not_delete(gdb_env, monkeypatch):
    """공개 진입점의 기본 인자로 만든 워커도 기존 repository 를 지우지 않는다."""
    gdb, fake, _ = gdb_env
    fake.existing_ids = [_REPO]
    captured = {}

    def _dispatch(*, key, worker):
        captured["worker"] = worker
        return json.dumps({"started": True, "job_id": "gdbinfer_test"})

    monkeypatch.setattr(gdb._GRAPHDB_JOBS, "dispatch", _dispatch)

    assert json.loads(gdb.graphdb_run_inference())["started"] is True
    result = json.loads(captured["worker"]())

    assert result["success"] is False
    assert fake.remote_calls("DELETE") == []


def test_explicit_overwrite_still_deletes_then_recreates(gdb_env):
    """overwrite_repo=True 는 기존 동작대로 삭제 후 재생성하고 다음 단계로 간다."""
    gdb, fake, _ = gdb_env
    fake.existing_ids = [_REPO]

    result = json.loads(gdb._graphdb_run_inference_sync(overwrite_repo=True))

    methods = [(c[0], c[1].rsplit("/", 2)[-2:]) for c in fake.remote_calls("DELETE", "POST")]
    assert methods[0] == ("DELETE", ["repositories", _REPO])
    assert methods[1] == ("POST", ["rest", "repositories"])
    # 대역이 첫 update 에서 연결을 끊으므로 ruleset 전환 단계까지 진행한 것이다.
    assert "ruleset 전환 실패" in result["error"]


def test_missing_repository_is_created_without_delete(gdb_env):
    gdb, fake, _ = gdb_env

    result = json.loads(gdb._graphdb_run_inference_sync())

    assert fake.remote_calls("DELETE") == []
    assert any(c[1].endswith("/rest/repositories") for c in fake.remote_calls("POST"))
    assert "ruleset 전환 실패" in result["error"]


# ── 2. tbox_path·abox_path 경계 ──────────────────────────────────────


def _attack(kind: str, data_dir: Path, tmp_path: Path) -> tuple[str, Path]:
    """공격 입력값과 그 입력이 가리키는 실제 파일을 돌려준다."""
    outside = tmp_path / "outside" / "secret.ttl"
    outside.parent.mkdir(exist_ok=True)
    outside.write_text("<urn:a> <urn:b> <urn:c> .\n", encoding="utf-8")
    if kind == "absolute":
        return str(outside), outside
    if kind == "traversal":
        value = os.path.relpath(outside, Path.cwd())
        assert value.startswith(".."), value
        return value, outside
    if kind == "symlink":
        link = data_dir / "link.ttl"
        link.symlink_to(outside)
        return str(link), outside
    inside = data_dir / "inside.json"
    inside.write_text("{}", encoding="utf-8")
    return str(inside), inside


@pytest.fixture
def metadata_probe(monkeypatch):
    """os.path.exists / getsize 가 받은 경로를 기록한다 (원래 동작은 유지)."""
    seen: list[str] = []
    real_exists, real_getsize = os.path.exists, os.path.getsize

    def _exists(path):
        seen.append(os.fspath(path))
        return real_exists(path)

    def _getsize(path):
        seen.append(os.fspath(path))
        return real_getsize(path)

    monkeypatch.setattr(os.path, "exists", _exists)
    monkeypatch.setattr(os.path, "getsize", _getsize)
    return seen


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlink", "wrong_suffix"])
@pytest.mark.parametrize("arg", ["tbox_path", "abox_path"])
def test_input_path_is_rejected_before_metadata_and_remote_calls(
    gdb_env, tmp_path, metadata_probe, arg, kind,
):
    gdb, fake, data_dir = gdb_env
    value, target = _attack(kind, data_dir, tmp_path)

    result = json.loads(gdb._graphdb_run_inference_sync(**{arg: value}))

    assert result["success"] is False
    assert result["error"].startswith(f"{arg} 경로 거부")
    assert any(p in result["error"] for p in _REJECT_PHRASES), result["error"]
    assert value not in result["error"] and str(target) not in result["error"]
    assert fake.calls == []
    assert str(target) not in metadata_probe and value not in metadata_probe


@pytest.mark.parametrize("arg", ["tbox_path", "abox_path"])
def test_public_entry_rejects_input_path_without_starting_a_job(
    gdb_env, tmp_path, monkeypatch, arg,
):
    gdb, fake, data_dir = gdb_env
    value, _ = _attack("absolute", data_dir, tmp_path)

    def _dispatch(**_kwargs):
        raise AssertionError("거부된 입력으로 잡을 띄우면 안 된다")

    monkeypatch.setattr(gdb._GRAPHDB_JOBS, "dispatch", _dispatch)

    result = json.loads(gdb.graphdb_run_inference(**{arg: value}))

    assert result["started"] is False
    assert result["error"].startswith(f"{arg} 경로 거부")
    assert fake.calls == []


@pytest.mark.parametrize("relative", [False, True])
def test_input_inside_data_dir_is_imported(gdb_env, tmp_path, monkeypatch, relative):
    """DATA_DIR 안의 절대경로·상대경로 입력은 그대로 import 된다."""
    gdb, fake, data_dir = gdb_env
    tbox = data_dir / "custom" / "t.ttl"
    tbox.parent.mkdir()
    tbox.write_text("<urn:a> <urn:b> <urn:c> .\n", encoding="utf-8")
    value = str(tbox)
    if relative:
        monkeypatch.chdir(tmp_path)
        value = "data/custom/t.ttl"

    result = json.loads(gdb._graphdb_run_inference_sync(tbox_path=value))

    imports = fake.remote_calls("IMPORT")
    assert [c[3] for c in imports] == [str(tbox.resolve())]
    assert imports[0][1].endswith(f"/repositories/{_REPO}/statements")
    assert "ruleset 전환 실패" in result["error"]


# ── 3. repository ID·ruleset·label 구문 경계 ─────────────────────────


_BAD_REPO = "kg/statements?update=DROP%20ALL&x="


def _graphdb_calls_with_bad_repo(gdb, data_dir):
    ttl = data_dir / "in.ttl"
    ttl.write_text("<urn:a> <urn:b> <urn:c> .\n", encoding="utf-8")
    return {
        "sparql_select": lambda: gdb.graphdb_sparql_select("SELECT * WHERE { ?s ?p ?o }", repo=_BAD_REPO),
        "sparql_construct": lambda: gdb.graphdb_sparql_construct(
            "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", repo=_BAD_REPO,
        ),
        "sparql_update": lambda: gdb.graphdb_sparql_update("INSERT DATA { <urn:a> <urn:b> <urn:c> }", repo=_BAD_REPO),
        "count_triples": lambda: gdb.graphdb_count_triples(repo=_BAD_REPO),
        "import_file": lambda: gdb.graphdb_import_file(str(ttl), repo=_BAD_REPO),
        "export_inferred": lambda: gdb.graphdb_export_inferred(repo=_BAD_REPO),
        "create_repository": lambda: gdb.graphdb_create_repository(repo_id=_BAD_REPO, overwrite=True),
        "run_inference_sync": lambda: gdb._graphdb_run_inference_sync(repo=_BAD_REPO, overwrite_repo=True),
    }


@pytest.mark.parametrize(
    "tool",
    [
        "sparql_select", "sparql_construct", "sparql_update", "count_triples",
        "import_file", "export_inferred", "create_repository", "run_inference_sync",
    ],
)
def test_repository_id_cannot_redirect_rest_requests(gdb_env, tool):
    """경로 구분자·질의 문자열이 든 repository ID 는 어떤 GraphDB 요청도 만들지 않는다."""
    gdb, fake, data_dir = gdb_env

    result = json.loads(_graphdb_calls_with_bad_repo(gdb, data_dir)[tool]())

    assert result["success"] is False
    assert "repository ID" in result["error"]
    assert fake.calls == []


def test_valid_repository_id_keeps_rest_path():
    from tools.remote import graphdb as gdb

    assert gdb._repo_url("steel-kg_2").endswith("/repositories/steel-kg_2")


def test_ruleset_cannot_append_sparql_update_operations(gdb_env):
    """ruleset 의 따옴표는 addRuleset 리터럴을 닫지 못한다. update 는 한 연산이다."""
    gdb, fake, _ = gdb_env
    payload = 'x" } ; DROP ALL ; INSERT DATA { _:c <urn:a> "y'

    gdb._graphdb_run_inference_sync(ruleset=payload)

    updates = [c[2]["update"] for c in fake.remote_calls("POST") if c[1].endswith("/statements")]
    assert updates, "ruleset 전환 update 가 전송돼야 한다"
    operations = parseUpdate(updates[0]).request
    assert [op.name for op in operations] == ["InsertData"]
    assert Literal(payload).n3() in updates[0]


def test_default_ruleset_update_text_is_unchanged(gdb_env):
    gdb, fake, _ = gdb_env

    gdb._graphdb_run_inference_sync(ruleset="owl2-rl-optimized")

    updates = [c[2]["update"] for c in fake.remote_calls("POST") if c[1].endswith("/statements")]
    assert updates[0] == (
        "INSERT DATA { _:b <http://www.ontotext.com/owlim/system#addRuleset> "
        '"owl2-rl-optimized" }'
    )


def test_repository_config_literals_cannot_add_config_triples(gdb_env):
    """label 의 따옴표가 저장소 설정 Turtle 에 graphdb:imports 같은 트리플을 덧붙이지 못한다."""
    gdb, fake, _ = gdb_env
    payload = 'a" ; <http://www.ontotext.com/config/graphdb#imports> "/etc/hosts'

    assert "생성됨" in gdb.graphdb_create_repository(repo_id=_REPO, label=payload, ruleset="empty")

    config_ttl = next(c[3]["config"][1] for c in fake.remote_calls("POST") if c[3])
    graph = Graph().parse(data=config_ttl.decode("utf-8"), format="turtle")
    labels = list(graph.objects(None, URIRef("http://www.w3.org/2000/01/rdf-schema#label")))
    imports = list(graph.objects(None, URIRef("http://www.ontotext.com/config/graphdb#imports")))
    assert labels == [Literal(payload)]
    assert imports == [Literal("")]


# ── 4. Neo4j Cypher 힌트와 세션 모드 ─────────────────────────────────

# Oxigraph 가 받는 IRI 문자만으로 식별자 자리를 벗어나는 local name 이다.
_UNSAFE_REL = "x),(m:Secret"
_UNSAFE_TBOX = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.com/onto#hasPart> a owl:ObjectProperty, owl:TransitiveProperty .
<https://example.com/onto#{_UNSAFE_REL}> a owl:ObjectProperty, owl:TransitiveProperty,
    owl:SymmetricProperty, owl:FunctionalProperty .
"""


def test_owl_semantics_hint_never_embeds_unsafe_relationship_name(tmp_path, monkeypatch):
    tbox = tmp_path / "t_box.ttl"
    tbox.write_text(_UNSAFE_TBOX, encoding="utf-8")
    monkeypatch.setattr("tools.remote.neo4j.TBOX_PATH", str(tbox))
    from tools.remote.neo4j import _extract_owl_semantics

    sem = _extract_owl_semantics({"hasPart", _UNSAFE_REL}, set())

    assert sem["hasPart"]["cypher_hint"] == "MATCH path=(a)-[:hasPart*]->(b)"
    unsafe = sem[_UNSAFE_REL]
    assert unsafe["transitive"] and unsafe["symmetric"] and unsafe["functional"]
    assert "cypher_hint" not in unsafe


def test_compensation_hint_keeps_placeholder_for_unsafe_relationship_name():
    from rdflib import OWL

    from tools.remote.neo4j import _generate_cypher_compensation

    payload = "x]->(b) DETACH DELETE b //"
    comp = _generate_cypher_compensation(str(OWL.TransitiveProperty), payload)

    assert payload not in comp
    assert "{rel}" in comp
    assert "[:hasChild*]" in _generate_cypher_compensation(str(OWL.TransitiveProperty), "hasChild")


def test_two_hop_path_hints_skip_unsafe_labels_and_types(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.remote.neo4j.TBOX_PATH", str(tmp_path / "missing.ttl"))
    from tools.remote.neo4j import _build_lpg_semantic_dict

    bad_rel = "R]->(x) DETACH DELETE x //"
    bad_label = "Bad)-[:x]-("
    nodes = tmp_path / "nodes.csv"
    rels = tmp_path / "relationships.csv"
    nodes.write_text(
        "uri:ID,:LABEL\nu:a,Pump\nu:b,Valve\nu:c,Plant\nu:d," + bad_label + "\n",
        encoding="utf-8",
    )
    rels.write_text(
        ":START_ID,:END_ID,:TYPE\n"
        "u:a,u:b,feeds\nu:b,u:c,locatedIn\nu:b,u:d," + bad_rel + "\n",
        encoding="utf-8",
    )

    paths = _build_lpg_semantic_dict(str(nodes), str(rels))["two_hop_paths"]

    assert "(:Pump)-[:feeds]->(:Valve)-[:locatedIn]->(:Plant)" in paths
    for path in paths:
        assert bad_rel not in path and bad_label not in path


def test_neo4j_stats_uses_read_session(monkeypatch):
    monkeypatch.setattr("tools.remote.neo4j.NEO4J_URI", "bolt://localhost:7687")
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.run.return_value.single.return_value = {"cnt": 0}
    session.run.return_value.data.return_value = []
    driver = MagicMock()
    driver.session.return_value = session
    monkeypatch.setattr("tools.remote.neo4j._get_driver", lambda: driver)
    from tools.remote.neo4j import neo4j_stats

    assert json.loads(neo4j_stats())["total_nodes"] == 0
    driver.session.assert_called_once_with(default_access_mode="READ")


def test_write_sessions_exist_only_in_explicit_deploy_path():
    """READ 가 아닌 driver.session() 은 명시적 적재 함수 안에만 있다."""
    import tools.remote.neo4j as neo4j_mod

    tree = ast.parse(Path(neo4j_mod.__file__).read_text(encoding="utf-8"))
    write_sessions: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "session"
            ):
                modes = [
                    kw.value.value for kw in node.keywords
                    if kw.arg == "default_access_mode" and isinstance(kw.value, ast.Constant)
                ]
                if modes != ["READ"]:
                    write_sessions.add(fn.name)

    assert write_sessions == {"_neo4j_deploy_lpg"}


def test_deploy_post_counts_read_back_in_the_load_session(tmp_path, monkeypatch):
    """적재 후 집계는 적재 세션에서 읽어 생성 수와 비교한다 (경고 없음)."""
    nodes = tmp_path / "nodes.csv"
    rels = tmp_path / "relationships.csv"
    nodes.write_text("uri:ID,:LABEL\nu:a,Pump\nu:b,Valve\n", encoding="utf-8")
    rels.write_text(":START_ID,:END_ID,:TYPE\nu:a,u:b,feeds\n", encoding="utf-8")

    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    def _run(query, *args, **kwargs):
        result = MagicMock()
        if "count(n)" in query:
            result.data.return_value = [{"cnt": 2}]
        elif "count(r)" in query:
            result.data.return_value = [{"cnt": 1}]
        else:
            result.data.return_value = []
        return result

    session.run.side_effect = _run
    driver = MagicMock()
    driver.session.return_value = session
    monkeypatch.setattr("tools.remote.neo4j._get_driver", lambda: driver)
    from tools.remote.neo4j import _neo4j_deploy_lpg

    result = json.loads(_neo4j_deploy_lpg(str(nodes), str(rels), replace=False))

    assert result["neo4j_totals"] == {"nodes": 2, "relationships": 1}
    assert "warnings" not in result
