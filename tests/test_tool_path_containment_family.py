"""경로를 받는 공개 도구 family 의 쓰기 대상, 가드 조건, 정상 입력 회귀 테스트.

``test_input_path_boundaries.py`` 의 표는 "경계 밖 입력을 읽지 않는다" 만 확인한다.
이 파일은 표로 표현하지 못하는 세 가지를 고정한다.

1. 쓰기 대상 (``export_quality_dqv``, ``graphdb_export_inferred``,
   ``annotate_lpg_graph_context``): 경계 밖 경로는 파일을 만들거나 바꾸지 않고,
   측정이나 네트워크 호출 같은 본 작업보다 먼저 거부된다.
2. ``ttl_content`` 와 함께 온 ``ttl_path`` 도 형제 도구와 같은 조건으로 검사된다.
3. 기본값, 경계 안 파일명, 경계 안 절대경로와 상대경로는 그대로 동작한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_VALID_TTL = """\
@prefix ex: <https://example.com/test#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Thing a owl:Class .
"""

_ORIGINAL = "ORIGINAL-CONTENT\n"

_REJECT_PHRASES = ("디렉터리 구분자", "허용된 디렉터리 밖", "허용된 데이터 디렉터리 밖")


def _rejected(message: str) -> bool:
    return any(phrase in message for phrase in _REJECT_PHRASES)


def _outside_file(tmp_path: Path, name: str) -> Path:
    """허용 디렉터리 밖에 원본 내용을 가진 파일을 만든다."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(exist_ok=True)
    target = outside_dir / name
    target.write_text(_ORIGINAL, encoding="utf-8")
    return target


def _write_attack(attack: str, base: Path, tmp_path: Path, name: str) -> tuple[str, Path]:
    """쓰기 대상 공격값과, 쓰기가 일어나면 바뀌어야 할 파일 경로를 돌려준다."""
    if attack == "absolute":
        target = _outside_file(tmp_path, name)
        return str(target), target
    if attack == "absolute_new_file":
        target = tmp_path / "outside" / f"new-{name}"
        target.parent.mkdir(exist_ok=True)
        return str(target), target
    if attack == "traversal":
        target = _outside_file(tmp_path, name)
        value = os.path.relpath(target, Path.cwd())
        assert value.startswith(".."), value
        return value, target
    if attack == "symlink":
        target = _outside_file(tmp_path, name)
        link = base / f"link-{name}"
        link.symlink_to(target)
        return str(link), target
    # wrong_suffix: 경계 안이지만 허용 확장자가 아닌 파일
    target = base / "inside.json"
    target.write_text(_ORIGINAL, encoding="utf-8")
    return str(target), target


def _assert_untouched(target: Path) -> None:
    if target.name.startswith("new-"):
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == _ORIGINAL


_WRITE_ATTACKS = ["absolute", "absolute_new_file", "traversal", "symlink", "wrong_suffix"]


# ── export_quality_dqv ───────────────────────────────────────────────


@pytest.fixture
def dqv_env(tmp_path, monkeypatch):
    """DQV 기본 출력 디렉터리를 tmp 로 옮기고 상류 측정 도구를 기록 stub 으로 바꾼다."""
    import tools.dqv_sidecar as dqv

    tbox = tmp_path / "generated" / "tbox" / "t_box.ttl"
    tbox.parent.mkdir(parents=True)
    tbox.write_text(_VALID_TTL, encoding="utf-8")
    monkeypatch.setattr(dqv, "TBOX_PATH", str(tbox))
    quality_dir = tmp_path / "generated" / "quality"
    quality_dir.mkdir()

    calls: list[str] = []

    def _metrics() -> str:
        calls.append("measure_tbox_metrics")
        return json.dumps({"success": True, "metrics": {"dit": {"value": 1}}})

    monkeypatch.setattr("tools.tbox_metrics.measure_tbox_metrics", _metrics)
    for target in (
        "tools.foops_fair.evaluate_fair_score",
        "tools.farber_dimensions.evaluate_farber_dimensions",
        "tools.ontology_quality.evaluate_oquare",
    ):
        monkeypatch.setattr(target, lambda: json.dumps({"success": False}))
    return dqv, quality_dir, calls


@pytest.mark.parametrize("attack", _WRITE_ATTACKS)
def test_export_quality_dqv_rejects_outside_target_before_measuring(
    attack, dqv_env, tmp_path
):
    """경계 밖 output_path 는 측정 전에 거부되고 어떤 파일도 쓰지 않는다."""
    dqv, quality_dir, calls = dqv_env
    value, target = _write_attack(attack, quality_dir, tmp_path, "dqv.ttl")

    result = json.loads(dqv.export_quality_dqv(output_path=value))

    assert result["success"] is False
    assert _rejected(result["error"]) or "확장자" in result["error"], result["error"]
    assert calls == []
    _assert_untouched(target)


def test_export_quality_dqv_default_and_inside_targets_are_written(
    dqv_env, tmp_path, monkeypatch
):
    """기본 경로, 경계 안 절대경로, 작업 디렉터리 기준 상대경로는 그대로 쓴다."""
    dqv, quality_dir, _calls = dqv_env

    default = json.loads(dqv.export_quality_dqv())
    assert default["success"] is True
    assert Path(default["output_path"]) == (quality_dir / "dqv_sidecar.ttl").resolve()
    assert (quality_dir / "dqv_sidecar.ttl").exists()

    inside = quality_dir / "nested" / "custom.ttl"
    absolute = json.loads(dqv.export_quality_dqv(output_path=str(inside)))
    assert absolute["success"] is True
    assert inside.exists()

    monkeypatch.chdir(tmp_path)
    relative = json.loads(dqv.export_quality_dqv(output_path="generated/quality/rel.ttl"))
    assert relative["success"] is True
    assert (quality_dir / "rel.ttl").exists()


# ── graphdb_export_inferred / graphdb_run_inference ─────────────────


@pytest.fixture
def gdb_env(tmp_path, monkeypatch):
    """INFERRED_PATH 를 tmp 로 옮기고 GraphDB HTTP 호출을 기록 stub 으로 바꾼다."""
    import config
    from tools.remote import graphdb as gdb

    inferred = tmp_path / "generated" / "inferred"
    inferred.mkdir(parents=True)
    monkeypatch.setattr(config, "INFERRED_PATH", str(inferred / "all_inferred.ttl"))
    requests_made: list[str] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size):
            yield b"<https://example.com/a> <https://example.com/b> <https://example.com/c> .\n"

    def _get(url, **_kwargs):
        requests_made.append(url)
        return _Resp()

    monkeypatch.setattr(gdb.requests, "get", _get)
    return gdb, inferred, requests_made


@pytest.mark.parametrize("attack", _WRITE_ATTACKS)
def test_graphdb_export_inferred_rejects_outside_target_before_request(
    attack, gdb_env, tmp_path
):
    """경계 밖 output_path 는 GraphDB 요청 전에 거부되고 어떤 파일도 쓰지 않는다."""
    gdb, inferred, requests_made = gdb_env
    value, target = _write_attack(attack, inferred, tmp_path, "export.ttl")

    result = gdb.graphdb_export_inferred(output_path=value)

    assert result.startswith("파일 export 거부"), result
    assert requests_made == []
    _assert_untouched(target)
    # 거부 응답은 입력 경로나 해석 경로를 되돌려 주지 않는다.
    assert str(target) not in result


def test_graphdb_export_inferred_default_and_inside_targets_are_written(
    gdb_env, tmp_path, monkeypatch
):
    """기본 INFERRED_PATH, 경계 안 절대경로(.nt), 상대경로는 그대로 쓴다."""
    gdb, inferred, requests_made = gdb_env

    assert "export 완료" in gdb.graphdb_export_inferred()
    assert (inferred / "all_inferred.ttl").exists()

    nt = inferred / "custom.nt"
    assert "export 완료" in gdb.graphdb_export_inferred(output_path=str(nt), format="nt")
    assert nt.exists()

    monkeypatch.chdir(tmp_path)
    rel = gdb.graphdb_export_inferred(output_path="generated/inferred/rel.ttl")
    assert "export 완료" in rel
    assert (inferred / "rel.ttl").exists()
    assert len(requests_made) == 3


def test_graphdb_run_inference_rejects_outside_output_before_any_stage(
    gdb_env, tmp_path, monkeypatch
):
    """추론 잡은 export 경로가 경계 밖이면 health check 와 import 를 시작하지 않는다."""
    gdb, _inferred, requests_made = gdb_env
    target = _outside_file(tmp_path, "inferred.ttl")
    stages: list[str] = []
    monkeypatch.setattr(gdb, "graphdb_health", lambda: stages.append("health") or "GraphDB OK")

    result = json.loads(gdb._graphdb_run_inference_sync(output_path=str(target)))

    assert result["success"] is False
    assert "export 경로 거부" in result["error"]
    assert _rejected(result["error"])
    assert stages == []
    assert requests_made == []
    _assert_untouched(target)


# ── annotate_lpg_graph_context ──────────────────────────────────────


_NODES_CSV = "id:ID,:LABEL\nn1,A\n"
_RELS_CSV = ":START_ID,:END_ID,:TYPE\nn1,n1,t\n"


@pytest.fixture
def lpg_env(tmp_path, monkeypatch):
    """LPG CSV 디렉터리를 tmp 로 옮기고 정상 CSV 한 쌍을 만든다."""
    import tools.lpg_graph_context as lgc

    inferred = tmp_path / "generated" / "inferred"
    lpg = inferred / "neo4j"
    lpg.mkdir(parents=True)
    monkeypatch.setattr(lgc, "INFERRED_PATH", str(inferred / "all_inferred.ttl"), raising=False)
    nodes = lpg / "nodes.csv"
    rels = lpg / "relationships.csv"
    nodes.write_text(_NODES_CSV, encoding="utf-8")
    rels.write_text(_RELS_CSV, encoding="utf-8")
    return lgc, lpg, nodes, rels


@pytest.mark.parametrize("attack", _WRITE_ATTACKS)
@pytest.mark.parametrize("which", ["nodes_csv", "relationships_csv"])
def test_annotate_lpg_graph_context_rejects_outside_csv_without_partial_write(
    which, attack, lpg_env, tmp_path
):
    """한쪽 CSV 만 경계 밖이어도 거부하고, 경계 안 CSV 도 다시 쓰지 않는다."""
    lgc, lpg, nodes, rels = lpg_env
    value, target = _write_attack(attack, lpg, tmp_path, "victim.csv")
    kwargs = {"nodes_csv": str(nodes), "relationships_csv": str(rels), which: value}

    result = json.loads(lgc.annotate_lpg_graph_context(**kwargs))

    assert result["success"] is False
    assert _rejected(result["error"]) or "확장자" in result["error"], result["error"]
    _assert_untouched(target)
    assert nodes.read_text(encoding="utf-8") == _NODES_CSV
    assert rels.read_text(encoding="utf-8") == _RELS_CSV


def test_annotate_lpg_graph_context_accepts_inside_absolute_and_relative(
    lpg_env, tmp_path, monkeypatch
):
    """convert_rdf_to_lpg 가 돌려주는 절대경로와 작업 디렉터리 기준 상대경로는 동작한다."""
    lgc, _lpg, nodes, rels = lpg_env

    result = json.loads(lgc.annotate_lpg_graph_context(str(nodes), str(rels)))
    assert result["success"] is True
    assert lgc.CONTEXT_COLUMN in nodes.read_text(encoding="utf-8").splitlines()[0]

    monkeypatch.chdir(tmp_path)
    again = json.loads(lgc.annotate_lpg_graph_context(
        "generated/inferred/neo4j/nodes.csv",
        "generated/inferred/neo4j/relationships.csv",
    ))
    assert again["success"] is True
    assert again["nodes_updated"] == 0  # 이미 컬럼이 있어 무변경


# ── ttl_content 와 함께 온 ttl_path 의 가드 조건 ──────────────────────


_TTL_PATH_SIBLINGS = [
    ("tools.validation_core", "validate_ttl_syntax"),
    ("tools.validation_core", "check_quality_rules"),
    ("tools.validation_core", "validate_tbox_shacl"),
    ("tools.validation_core", "validate_shacl"),
    ("tools.tbox_metrics", "detect_tbox_antipatterns"),
]


@pytest.mark.parametrize(("module_name", "function_name"), _TTL_PATH_SIBLINGS)
def test_ttl_path_is_checked_even_when_ttl_content_is_given(
    module_name, function_name, tmp_path, monkeypatch
):
    """형제 도구는 ttl_content 유무와 무관하게 ttl_path 를 경계 검사한다."""
    import importlib

    module = importlib.import_module(module_name)
    tbox = tmp_path / "generated" / "tbox"
    tbox.mkdir(parents=True)
    monkeypatch.setattr(module, "GENERATED_TBOX_DIR", str(tbox), raising=False)
    outside = tmp_path / "outside.ttl"
    outside.write_text(_VALID_TTL, encoding="utf-8")

    result = json.loads(
        getattr(module, function_name)(ttl_content=_VALID_TTL, ttl_path=str(outside))
    )

    assert result["success"] is False
    assert _rejected(result["error"]), result["error"]


# ── 정상 입력은 그대로 동작한다 ─────────────────────────────────────


@pytest.fixture
def tbox_case(tmp_path):
    tbox = tmp_path / "generated" / "tbox"
    tbox.mkdir(parents=True)
    (tbox / "t_box.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (tbox / "case.ttl").write_text(_VALID_TTL, encoding="utf-8")
    return tbox


def test_detect_tbox_antipatterns_reads_name_inside_tbox_dir(tbox_case, monkeypatch):
    import tools.tbox_metrics as tbox_metrics

    monkeypatch.setattr(tbox_metrics, "GENERATED_TBOX_DIR", str(tbox_case), raising=False)

    result = json.loads(tbox_metrics.detect_tbox_antipatterns(ttl_path="case.ttl"))

    assert result["success"] is True


def test_trace_quality_issues_reads_name_inside_tbox_dir(tbox_case, monkeypatch):
    import tools.quality_dashboard as quality_dashboard
    import tools.validation_core as validation_core

    monkeypatch.setattr(quality_dashboard, "GENERATED_TBOX_DIR", str(tbox_case), raising=False)
    loaded: list[str] = []
    original = validation_core.load_ttl_content

    def _spy(ttl_content, ttl_path, default_path=""):
        loaded.append(ttl_path)
        return original(ttl_content, ttl_path, default_path)

    monkeypatch.setattr(validation_core, "load_ttl_content", _spy)
    monkeypatch.setattr(
        "tools.tbox_metrics.measure_tbox_metrics", lambda: json.dumps({"metrics": {}})
    )

    result = json.loads(quality_dashboard.trace_quality_issues(ttl_path="case.ttl"))

    assert result["success"] is True
    assert loaded == [str((tbox_case / "case.ttl").resolve())]


@pytest.mark.parametrize("function_name", ["evaluate_fair_score", "evaluate_miro"])
def test_fair_and_miro_read_name_inside_tbox_dir(
    function_name, tbox_case, tmp_path, monkeypatch
):
    import tools.foops_fair as foops_fair

    monkeypatch.setattr(foops_fair, "GENERATED_TBOX_DIR", str(tbox_case), raising=False)
    monkeypatch.setattr(foops_fair, "TBOX_PATH", str(tbox_case / "t_box.ttl"))
    monkeypatch.setattr(foops_fair, "GENERATED_REPORTS_DIR", str(tmp_path / "reports"))

    result = json.loads(getattr(foops_fair, function_name)(tbox_path="case.ttl"))

    assert result["success"] is True
    assert result["tbox_path"] == str((tbox_case / "case.ttl").resolve())


def test_resolve_entity_references_reads_names_inside_abox_dir(tmp_path, monkeypatch):
    import tools.entity_resolution as entity_resolution

    abox_dir = tmp_path / "generated" / "abox"
    abox_dir.mkdir(parents=True)
    (abox_dir / "abox.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (abox_dir / "master.ttl").write_text(_VALID_TTL, encoding="utf-8")
    monkeypatch.setattr(entity_resolution, "GENERATED_ABOX_DIR", str(abox_dir), raising=False)

    result = entity_resolution.resolve_entity_references(
        abox_path="abox.ttl", master_path="master.ttl",
    )

    assert isinstance(result, dict), result
    assert result["abox_path"] == str((abox_dir / "abox.ttl").resolve())
    assert result["master_path"] == str((abox_dir / "master.ttl").resolve())


@pytest.fixture
def generated_graphs(tmp_path, monkeypatch):
    """data/generated 대체 디렉터리에 그래프 두 개를 두고 canonical 경로 상수를 옮긴다."""
    import tools.canonical as canonical

    generated = tmp_path / "generated"
    (generated / "tbox").mkdir(parents=True)
    a = generated / "tbox" / "a.ttl"
    b = generated / "b.ttl"
    a.write_text(_VALID_TTL, encoding="utf-8")
    b.write_text(_VALID_TTL, encoding="utf-8")
    monkeypatch.setattr(canonical, "GENERATED_DIR", str(generated), raising=False)
    monkeypatch.setattr(canonical, "GENERATED_REPORTS_DIR", str(generated / "reports"))
    monkeypatch.setattr(canonical, "TBOX_PATH", str(generated / "tbox" / "t_box.ttl"))
    monkeypatch.setattr(canonical, "INFERRED_PATH", str(generated / "inferred.ttl"))
    return canonical, a, b


def test_canonical_tools_accept_absolute_and_relative_paths_inside_generated(
    generated_graphs, tmp_path, monkeypatch
):
    canonical, a, b = generated_graphs

    single = json.loads(canonical.canonicalize_graph(str(a)))
    assert single["success"] is True
    assert "sha256" in single

    pair = json.loads(canonical.compare_canonical(str(a), str(b)))
    assert pair["success"] is True
    assert pair["hash_equal"] is True

    monkeypatch.chdir(tmp_path)
    relative = json.loads(canonical.canonicalize_graph("generated/tbox/a.ttl"))
    assert relative["success"] is True
    assert relative["sha256"] == single["sha256"]


def test_summarize_confidence_sidecar_reads_path_inside_generated(tmp_path, monkeypatch):
    from rdflib import URIRef

    import tools.triple_confidence as triple_confidence
    from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS

    generated = tmp_path / "generated"
    generated.mkdir()
    sidecar = generated / "inferred_confidence.ttl"
    triple_confidence.write_confidence_sidecar(
        [{
            "s": URIRef(f"{DOMAIN_INST_NS}A"),
            "p": URIRef(f"{DOMAIN_NS}p"),
            "o": URIRef(f"{DOMAIN_INST_NS}B"),
            "provenance": "csv_direct",
        }],
        str(sidecar),
    )
    monkeypatch.setattr(triple_confidence, "GENERATED_DIR", str(generated), raising=False)

    result = json.loads(triple_confidence.summarize_confidence_sidecar(str(sidecar)))

    assert result["success"] is True
    assert result["total_statements"] == 1


@pytest.fixture
def neo4j_env(tmp_path, monkeypatch):
    """neo4j 모듈의 추론·LPG 경로를 tmp 로 옮기고 본 작업을 기록 stub 으로 바꾼다."""
    from tools.remote import neo4j

    inferred = tmp_path / "generated" / "inferred"
    lpg = inferred / "neo4j"
    lpg.mkdir(parents=True)
    (inferred / "custom.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (lpg / "nodes.csv").write_text("uri:ID,:LABEL\n", encoding="utf-8")
    (lpg / "relationships.csv").write_text(":START_ID,:END_ID,:TYPE\n", encoding="utf-8")
    monkeypatch.setattr(neo4j, "INFERRED_PATH", str(inferred / "all_inferred.ttl"))
    monkeypatch.setattr(
        neo4j, "LPG_SEMANTIC_DICT_PATH", str(tmp_path / "generated" / "neo4j" / "dict.json")
    )
    monkeypatch.setattr(neo4j, "NEO4J_URI", "bolt://localhost:7687")
    seen: dict[str, tuple] = {}

    def _convert(ttl_file, sample_count, open_browser):
        seen["convert"] = (ttl_file,)
        return json.dumps({"success": True})

    def _deploy(nodes_csv, relationships_csv, replace):
        seen["deploy"] = (nodes_csv, relationships_csv)
        return json.dumps({"success": True})

    def _build(nodes_path, rels_path):
        seen["dict"] = (nodes_path, rels_path)
        return {
            "labels": {},
            "relationship_types": {},
            "metadata": {"total_nodes": 0, "total_relationships": 0},
        }

    monkeypatch.setattr(neo4j, "_convert_rdf_to_lpg_impl", _convert)
    monkeypatch.setattr(neo4j, "_neo4j_deploy_lpg", _deploy)
    monkeypatch.setattr(neo4j, "_build_lpg_semantic_dict", _build)
    return neo4j, inferred, lpg, seen


def test_neo4j_lpg_tools_pass_resolved_inside_paths(neo4j_env):
    """경계 안 입력은 해석된 경로로 본 작업에 전달되고 기본값은 그대로 비어 있다."""
    neo4j, inferred, lpg, seen = neo4j_env

    assert json.loads(neo4j.convert_rdf_to_lpg(ttl_file="custom.ttl", open_browser=False))[
        "success"
    ]
    assert seen["convert"] == (str((inferred / "custom.ttl").resolve()),)
    assert json.loads(neo4j.convert_rdf_to_lpg(open_browser=False))["success"]
    assert seen["convert"] == ("",)

    nodes, rels = str(lpg / "nodes.csv"), str(lpg / "relationships.csv")
    assert json.loads(neo4j.neo4j_deploy_lpg(nodes_csv=nodes, relationships_csv=rels))[
        "success"
    ]
    assert seen["deploy"] == (str(Path(nodes).resolve()), str(Path(rels).resolve()))

    result = json.loads(
        neo4j.generate_lpg_semantic_dictionary(nodes_csv=nodes, relationships_csv=rels)
    )
    assert result["success"] is True
    assert seen["dict"] == (str(Path(nodes).resolve()), str(Path(rels).resolve()))
