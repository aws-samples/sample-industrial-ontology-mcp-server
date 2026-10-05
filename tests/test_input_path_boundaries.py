"""유지 도구 입력 경로의 프로젝트 경계 회귀 테스트."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

_VALID_TTL = """\
@prefix ex: <https://example.com/test#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Thing a owl:Class .
"""

_VALID_CSV = "uri:ID,:LABEL\nex:a,Thing\n"

#: ``resolve_child_path`` 와 ``resolve_path_within`` 가 경계 밖 입력에 내는 메시지 조각.
_REJECT_PHRASES = ("디렉터리 구분자", "허용된 디렉터리 밖", "허용된 데이터 디렉터리 밖")


# 기준 디렉터리 바로 아래 파일명만 받는 파라미터 (``resolve_child_path``).
_PATH_TARGETS = [
    ("tools.validation_core", "validate_ttl_syntax", "ttl_path", "tbox", ".ttl"),
    ("tools.validation_core", "check_quality_rules", "ttl_path", "tbox", ".ttl"),
    ("tools.validation_core", "validate_shacl", "ttl_path", "tbox", ".ttl"),
    ("tools.owl_reasoner", "check_owl2_profile", "ttl_path", "tbox", ".ttl"),
    ("tools.owl_reasoner", "validate_owl_consistency", "ttl_path", "tbox", ".ttl"),
    ("tools.owl_reasoner", "classify_tbox", "ttl_path", "tbox", ".ttl"),
    ("tools.owl_reasoner", "validate_owl_justification", "ttl_path", "tbox", ".ttl"),
    ("tools.validation_core", "validate_tbox_shacl", "ttl_path", "tbox", ".ttl"),
    ("tools.visualization", "visualize_tbox", "tbox_path", "tbox", ".ttl"),
    (
        "tools.semantic_dict_validation",
        "validate_semantic_dictionary",
        "dict_path",
        "generated",
        ".json",
    ),
    (
        "tools.semantic_dict_validation",
        "validate_semantic_dictionary",
        "tbox_path",
        "tbox",
        ".ttl",
    ),
    (
        "tools.semantic_dict_validation",
        "validate_semantic_dictionary",
        "abox_path",
        "abox",
        ".ttl",
    ),
    ("tools.inference", "run_owl_rl_inference", "tbox_path", "tbox", ".ttl"),
    ("tools.inference", "run_owl_rl_inference", "abox_path", "abox", ".ttl"),
    (
        "tools.query_test",
        "test_domain_queries",
        "test_cases_path",
        "query_tests",
        ".json",
    ),
    ("tools.quality_dashboard", "trace_quality_issues", "ttl_path", "tbox", ".ttl"),
    ("tools.tbox_metrics", "detect_tbox_antipatterns", "ttl_path", "tbox", ".ttl"),
    ("tools.foops_fair", "evaluate_fair_score", "tbox_path", "tbox", ".ttl"),
    ("tools.foops_fair", "evaluate_miro", "tbox_path", "tbox", ".ttl"),
    (
        "tools.entity_resolution",
        "resolve_entity_references",
        "abox_path",
        "abox",
        ".ttl",
    ),
    (
        "tools.entity_resolution",
        "resolve_entity_references",
        "master_path",
        "abox",
        ".ttl",
    ),
    ("tools.remote.neo4j", "convert_rdf_to_lpg", "ttl_file", "inferred", ".ttl"),
]

# 기준 디렉터리 아래 절대·상대 경로를 받는 파라미터 (``resolve_path_within``).
_PATH_WITHIN_TARGETS = [
    ("tools.canonical", "canonicalize_graph", "path", "generated", ".ttl"),
    ("tools.canonical", "compare_canonical", "path_a", "generated", ".ttl"),
    ("tools.canonical", "compare_canonical", "path_b", "generated", ".ttl"),
    (
        "tools.triple_confidence",
        "summarize_confidence_sidecar",
        "sidecar_path",
        "generated",
        ".ttl",
    ),
    ("tools.remote.neo4j", "neo4j_deploy_lpg", "nodes_csv", "lpg", ".csv"),
    ("tools.remote.neo4j", "neo4j_deploy_lpg", "relationships_csv", "lpg", ".csv"),
    (
        "tools.remote.neo4j",
        "generate_lpg_semantic_dictionary",
        "nodes_csv",
        "lpg",
        ".csv",
    ),
    (
        "tools.remote.neo4j",
        "generate_lpg_semantic_dictionary",
        "relationships_csv",
        "lpg",
        ".csv",
    ),
]

_ALL_TARGETS = [(*row, "name") for row in _PATH_TARGETS] + [
    (*row, "path") for row in _PATH_WITHIN_TARGETS
]


def _fail_before_guard(*_args, **_kwargs):
    raise RuntimeError("입력 경로 가드보다 파일 로드가 먼저 실행됨")


def _configure_safe_paths(module, tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """도구별 기본 입력과 출력 경로를 테스트 디렉터리로 격리한다."""
    generated = tmp_path / "generated"
    tbox = generated / "tbox"
    abox = generated / "abox"
    inferred = generated / "inferred"
    lpg = inferred / "neo4j"
    reports = generated / "reports"
    query_tests = tmp_path / "source" / "query_tests"
    for directory in (generated, tbox, abox, inferred, lpg, reports, query_tests):
        directory.mkdir(parents=True, exist_ok=True)

    (tbox / "t_box.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (abox / "a_box.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (abox / "master_data.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (inferred / "all_inferred.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (lpg / "nodes.csv").write_text(_VALID_CSV, encoding="utf-8")
    (lpg / "relationships.csv").write_text(
        ":START_ID,:END_ID,:TYPE\n", encoding="utf-8"
    )
    (generated / "semantic_dictionary.json").write_text("{}", encoding="utf-8")
    (query_tests / "competency_questions.json").write_text("[]", encoding="utf-8")

    replacements = {
        "GENERATED_DIR": generated,
        "GENERATED_TBOX_DIR": tbox,
        "GENERATED_ABOX_DIR": abox,
        "GENERATED_INFERRED_DIR": inferred,
        "GENERATED_REPORTS_DIR": reports,
        "SOURCE_QUERY_TESTS_DIR": query_tests,
        "TBOX_PATH": tbox / "t_box.ttl",
        "ABOX_PATH": abox / "a_box.ttl",
        "MASTER_DATA_PATH": abox / "master_data.ttl",
        "INFERRED_PATH": inferred / "all_inferred.ttl",
        "SEMANTIC_DICT_PATH": generated / "semantic_dictionary.json",
        "LPG_SEMANTIC_DICT_PATH": generated / "neo4j" / "semantic_dictionary.json",
        "_CQ_PATH": query_tests / "competency_questions.json",
    }
    for name, value in replacements.items():
        monkeypatch.setattr(module, name, str(value), raising=False)

    loader_by_module = {
        "tools.validation_core": "_load_ttl",
        "tools.quality_dashboard": "_load_ttl",
        "tools.tbox_metrics": "_load_ttl",
        "tools.owl_reasoner": "_load_ttl_content",
        "tools.canonical": "_load",
        "tools.foops_fair": "evaluate_fair",
    }
    if module.__name__ in loader_by_module:
        monkeypatch.setattr(module, loader_by_module[module.__name__], _fail_before_guard)
    elif module.__name__ == "tools.inference":
        monkeypatch.setattr(
            module._INFERENCE_JOBS,
            "dispatch",
            lambda **_kwargs: json.dumps({"success": True, "started": True}),
        )
    elif module.__name__ == "tools.query_test":
        monkeypatch.setattr(module, "_load_semantic_dict", lambda: {})
        monkeypatch.setattr(module, "_load_abox_stats", lambda: {"per_class": {}})
    elif module.__name__ == "tools.remote.neo4j":
        # 미설정 거부보다 경로 가드가 먼저 판정되도록 연결 설정만 채우고,
        # 가드를 통과하면 실제 변환·적재·사전 생성 대신 즉시 실패시킨다.
        monkeypatch.setattr(module, "NEO4J_URI", "bolt://localhost:7687")
        for loader in (
            "_convert_rdf_to_lpg_impl",
            "_neo4j_deploy_lpg",
            "_build_lpg_semantic_dict",
            "_get_driver",
        ):
            monkeypatch.setattr(module, loader, _fail_before_guard)

    return {
        "generated": generated,
        "tbox": tbox,
        "abox": abox,
        "inferred": inferred,
        "lpg": lpg,
        "query_tests": query_tests,
    }


def _attack_value(
    attack: str,
    suffix: str,
    base_dir: Path,
    tmp_path: Path,
    kind: str,
) -> str:
    """절대경로, 상위 경로, 외부 symlink 공격값을 만든다.

    ``kind == "name"`` 이면 symlink 를 기준 디렉터리 안 파일명으로, ``"path"`` 면
    기준 디렉터리 안의 절대경로로 넘긴다. 두 경우 모두 해석 결과는 경계 밖이다.
    """
    outside = tmp_path / f"outside{suffix}"
    outside.write_text(_VALID_TTL if suffix == ".ttl" else "{}", encoding="utf-8")

    if attack == "absolute":
        return str(outside)
    if attack == "traversal":
        return "../" * 7 + str(outside).lstrip("/")

    link = base_dir / f"outside-link{suffix}"
    link.symlink_to(outside)
    return link.name if kind == "name" else str(link)


def _companion_kwargs(function_name: str, parameter: str, bases: dict[str, Path]) -> dict:
    """공격 파라미터 외에 필수이거나 부작용을 끄는 인자를 채운다."""
    if function_name == "test_domain_queries":
        return {"open_report": False, "verify_joins": False}
    if function_name == "convert_rdf_to_lpg":
        return {"open_browser": False}
    if function_name == "compare_canonical":
        other = "path_b" if parameter == "path_a" else "path_a"
        return {other: str(bases["tbox"] / "t_box.ttl")}
    return {}


@pytest.mark.parametrize(
    ("module_name", "function_name", "parameter", "base_name", "suffix", "kind"),
    _ALL_TARGETS,
)
@pytest.mark.parametrize("attack", ["absolute", "traversal", "symlink"])
def test_public_path_parameters_reject_escape_attempts(
    module_name,
    function_name,
    parameter,
    base_name,
    suffix,
    kind,
    attack,
    tmp_path,
    monkeypatch,
):
    """공개 경로 파라미터가 허용 디렉터리 밖 입력을 읽지 않는다."""
    module = importlib.import_module(module_name)
    bases = _configure_safe_paths(module, tmp_path, monkeypatch)
    value = _attack_value(attack, suffix, bases[base_name], tmp_path, kind)
    kwargs = {parameter: value, **_companion_kwargs(function_name, parameter, bases)}

    result = json.loads(getattr(module, function_name)(**kwargs))

    assert result["success"] is False
    assert any(phrase in result["error"] for phrase in _REJECT_PHRASES), result["error"]
