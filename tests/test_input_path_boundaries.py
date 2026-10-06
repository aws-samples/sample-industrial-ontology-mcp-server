"""유지 도구 입력의 프로젝트 경계 회귀 테스트.

경로, 디렉터리, 파일 이름이 되는 키(timestamp, cache key) 를 받는 공개 도구가
허용 디렉터리 밖 입력을 읽지 않는지 표 하나로 고정한다. 표에 없는 경로형
파라미터는 ``tests/test_path_argument_coverage.py`` 가 실패로 잡는다.

같은 경계 계열로, 추론기 로더의 ``owl:imports`` 해석과 tacit 규칙이 CSV 값을
IRI 로 바꾸는 지점도 여기서 확인한다.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

_VALID_TTL = """\
@prefix ex: <https://example.com/test#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Thing a owl:Class .
"""

_VALID_CSV = "uri:ID,:LABEL\nex:a,Thing\n"

#: 경계 가드가 거부할 때 내는 메시지 조각. ``resolve_child_path`` /
#: ``resolve_path_within`` 의 경계 밖 메시지와 파일 이름, 키 형식 검사 메시지다.
_REJECT_PHRASES = (
    "디렉터리 구분자",
    "허용된 디렉터리 밖",
    "허용된 데이터 디렉터리 밖",
    "영숫자/언더스코어/하이픈만 허용",
    "형식이어야 합니다",
    "SHA-256 hex",
)


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
    (
        "tools.cardinality_sync",
        "check_shacl_owl_cardinality_sync",
        "tbox_path",
        "tbox",
        ".ttl",
    ),
    (
        "tools.inference_classify",
        "classify_inference_triples",
        "inferred_path",
        "inferred",
        ".ttl",
    ),
    ("tools.ontoclean", "validate_ontoclean", "tbox_path", "tbox", ".ttl"),
    (
        "tools.tacit_input",
        "add_tacit_from_natural_language",
        "filename",
        "tacit",
        ".ttl",
    ),
    ("tools.tacit_input", "generate_tacit_from_data", "filename", "tacit", ".ttl"),
    ("tools.local_artifacts", "read_tacit", "filename", "tacit", ".ttl"),
    ("tools.tacit_rules", "augment_csv_fk", "source_csv", "rawdata", ".csv"),
    ("tools.tacit_rules", "augment_csv_fk", "target_csv", "rawdata", ".csv"),
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
    (
        "tools.cardinality_sync",
        "check_shacl_owl_cardinality_sync",
        "shapes_path",
        "rules",
        ".ttl",
    ),
    (
        "tools.cwa_owa_tagging",
        "summarize_origin_sidecar",
        "sidecar_path",
        "generated",
        ".ttl",
    ),
    (
        "tools.farber_dimensions",
        "evaluate_farber_dimensions",
        "confidence_sidecar_path",
        "generated",
        ".ttl",
    ),
    (
        "tools.farber_dimensions",
        "evaluate_farber_dimensions",
        "provenance_sidecar_path",
        "generated",
        ".ttl",
    ),
    ("tools.provenance", "trace_provenance", "prov_path", "generated", ".ttl"),
    (
        "tools.provenance",
        "query_inference_justification",
        "prov_path",
        "generated",
        ".ttl",
    ),
    ("tools.tacit_rules", "generate_tacit_from_rules", "rules_path", "rules", ".json"),
]

# 기준 디렉터리 자신 또는 그 하위 디렉터리를 받는 파라미터.
_DIR_TARGETS = [
    ("tools.drift_monitor", "monitor_csv_drift", "rawdata_dir", "rawdata", ""),
    (
        "tools.drift_partial_pipeline",
        "run_partial_pipeline_on_drift",
        "rawdata_dir",
        "rawdata",
        "",
    ),
    ("tools.swrl_inference", "run_swrl_inference", "swrl_dir", "swrl", ""),
]

# 파일 이름의 일부가 되는 키. 형식 검사 뒤 기준 디렉터리 바로 아래로 해석한다.
_KEY_TARGETS = [
    ("tools.meta_audit", "read_meta_audit", "timestamp", "meta_audit", ".json"),
    (
        "tools.prompt_audit",
        "verify_prompt_reproducibility",
        "cache_key",
        "prompt_cache",
        ".json",
    ),
]

#: 키 파라미터별로 형식은 맞는 값. symlink 공격에서 링크 이름으로 쓴다.
_VALID_KEYS = {"timestamp": "20260101T000000Z", "cache_key": "a" * 64}

_ALL_TARGETS = (
    [(*row, "name") for row in _PATH_TARGETS]
    + [(*row, "path") for row in _PATH_WITHIN_TARGETS]
    + [(*row, "dir") for row in _DIR_TARGETS]
    + [(*row, "key") for row in _KEY_TARGETS]
)


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
    meta_audit = generated / "meta_audit"
    prompt_cache = generated / "prompt_cache"
    source = tmp_path / "source"
    query_tests = source / "query_tests"
    rawdata = source / "rawdata"
    tacit = source / "tacit"
    rules = tmp_path / "rules"
    swrl = rules / "swrl"
    for directory in (
        generated, tbox, abox, inferred, lpg, reports, meta_audit, prompt_cache,
        query_tests, rawdata, tacit, rules / "domain", rules / "policy", swrl,
    ):
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
    (rules / "policy" / "tbox_shapes.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (rawdata / "Source.csv").write_text("id\nS1\n", encoding="utf-8")
    (rawdata / "Target.csv").write_text("code\nT1\n", encoding="utf-8")

    replacements = {
        "GENERATED_DIR": generated,
        "GENERATED_TBOX_DIR": tbox,
        "GENERATED_ABOX_DIR": abox,
        "GENERATED_INFERRED_DIR": inferred,
        "GENERATED_REPORTS_DIR": reports,
        "SOURCE_QUERY_TESTS_DIR": query_tests,
        "SOURCE_RAWDATA_DIR": rawdata,
        "SOURCE_TACIT_DIR": tacit,
        "RULES_ROOT": rules,
        "SWRL_DIR_DEFAULT": swrl,
        "CACHE_DIR": prompt_cache,
        "TBOX_PATH": tbox / "t_box.ttl",
        "ABOX_PATH": abox / "a_box.ttl",
        "MASTER_DATA_PATH": abox / "master_data.ttl",
        "INFERRED_PATH": inferred / "all_inferred.ttl",
        "SEMANTIC_DICT_PATH": generated / "semantic_dictionary.json",
        "LPG_SEMANTIC_DICT_PATH": generated / "neo4j" / "semantic_dictionary.json",
        "_CQ_PATH": query_tests / "competency_questions.json",
        "_DEFAULT_SHAPES": rules / "policy" / "tbox_shapes.ttl",
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
        "tools.cardinality_sync": "check_cardinality_sync",
        "tools.cwa_owa_tagging": "summarize_origin_distribution",
        "tools.inference_classify": "Graph",
        "tools.ontoclean": "analyze_ontoclean",
        "tools.farber_dimensions": "evaluate_farber",
        "tools.provenance": "_new_graph",
        "tools.drift_monitor": "monitor_all_csvs",
        "tools.drift_partial_pipeline": "monitor_all_csvs",
        "tools.swrl_inference": "_load_swrl_rules",
    }
    if module.__name__ in loader_by_module:
        monkeypatch.setattr(module, loader_by_module[module.__name__], _fail_before_guard)
    if module.__name__ == "tools.inference":
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
    elif module.__name__ == "tools.drift_partial_pipeline":
        # 이 도구의 경계 기준은 drift_monitor 의 SOURCE_RAWDATA_DIR 이다.
        monkeypatch.setattr(
            "tools.drift_monitor.SOURCE_RAWDATA_DIR", str(rawdata), raising=False,
        )
        monkeypatch.setattr(module, "update_tbox_incremental", _fail_before_guard)
    elif module.__name__ == "tools.swrl_inference":
        monkeypatch.setenv("SWRL_ENABLED", "true")
    elif module.__name__ == "tools.tacit_input":
        # 가드를 지나면 Bedrock 호출 전에 실패시킨다.
        for loader in ("invoke_bedrock_text", "_load_tbox_context", "_load_csv_summary"):
            monkeypatch.setattr(module, loader, _fail_before_guard)

    return {
        "generated": generated,
        "tbox": tbox,
        "abox": abox,
        "inferred": inferred,
        "lpg": lpg,
        "query_tests": query_tests,
        "rawdata": rawdata,
        "tacit": tacit,
        "rules": rules,
        "swrl": swrl,
        "meta_audit": meta_audit,
        "prompt_cache": prompt_cache,
    }


def _attack_value(
    attack: str,
    suffix: str,
    base_dir: Path,
    tmp_path: Path,
    kind: str,
    parameter: str,
) -> str:
    """절대경로, 상위 경로, 외부 symlink 공격값을 만든다.

    ``kind`` 별로 넘기는 값이 다르다. ``name`` 은 기준 디렉터리 안 파일명, ``path``
    는 기준 디렉터리 안 절대경로, ``dir`` 은 디렉터리 경로, ``key`` 는 ``.json`` 을
    뺀 키 값이다. 모든 경우 해석 결과는 경계 밖이다.
    """
    if kind == "dir":
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "T.csv").write_text("ID\nR1\n", encoding="utf-8")
        (outside_dir / "rule.swrl").write_text("r: A(?x) -> B(?x)\n", encoding="utf-8")
        if attack == "absolute":
            return str(outside_dir)
        if attack == "traversal":
            return "../" * 7 + str(outside_dir).lstrip("/")
        link = base_dir / "outside-link"
        link.symlink_to(outside_dir, target_is_directory=True)
        return str(link)

    outside = tmp_path / f"outside{suffix}"
    outside.write_text(_VALID_TTL if suffix == ".ttl" else "{}", encoding="utf-8")

    if kind == "key":
        stem = outside.with_suffix("")
        if attack == "absolute":
            return str(stem)
        if attack == "traversal":
            return os.path.relpath(stem, base_dir)
        key = _VALID_KEYS[parameter]
        (base_dir / f"{key}{suffix}").symlink_to(outside)
        return key

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
    if function_name == "trace_provenance":
        return {"instance_uri": "https://example.com/test#a"}
    if function_name == "query_inference_justification":
        return {
            "s": "https://example.com/test#a",
            "p": "https://example.com/test#p",
            "o": "https://example.com/test#b",
        }
    if function_name == "add_tacit_from_natural_language":
        return {"text": "설비 A 다음 공정은 설비 B 다.", "overwrite": True}
    if function_name == "generate_tacit_from_data":
        return {"overwrite": True}
    if function_name in {"monitor_csv_drift", "run_partial_pipeline_on_drift"}:
        return {"save_snapshot": False} if function_name == "monitor_csv_drift" else {
            "dry_run": True,
        }
    if function_name == "run_swrl_inference":
        return {"append_to_inferred": False}
    if function_name == "augment_csv_fk":
        other = (
            {"target_csv": "Target.csv"} if parameter == "source_csv"
            else {"source_csv": "Source.csv"}
        )
        return {"new_column": "target_fk", "target_pk_column": "code", **other}
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
    value = _attack_value(attack, suffix, bases[base_name], tmp_path, kind, parameter)
    kwargs = {parameter: value, **_companion_kwargs(function_name, parameter, bases)}

    result = json.loads(getattr(module, function_name)(**kwargs))

    assert result.get("success") is False, result
    assert any(phrase in result.get("error", "") for phrase in _REJECT_PHRASES), result


# ── 경계 안 입력은 그대로 동작한다 ─────────────────────────────────


def test_inside_paths_still_reach_the_loader(tmp_path, monkeypatch):
    """새로 경계를 둔 파라미터도 기준 디렉터리 안 값은 본 작업까지 넘긴다."""
    import tools.cardinality_sync as cardinality_sync
    import tools.cwa_owa_tagging as cwa
    import tools.farber_dimensions as farber
    import tools.inference_classify as classify
    import tools.ontoclean as ontoclean
    import tools.provenance as provenance

    seen: dict[str, tuple] = {}

    def _record(name, value):
        def _inner(*args, **kwargs):
            seen[name] = (args, kwargs)
            return value
        return _inner

    bases = {}
    for module in (cardinality_sync, cwa, farber, classify, ontoclean, provenance):
        bases = _configure_safe_paths(module, tmp_path, monkeypatch)
    generated, tbox, inferred, rules = (
        bases["generated"], bases["tbox"], bases["inferred"], bases["rules"],
    )
    (tbox / "case.ttl").write_text(_VALID_TTL, encoding="utf-8")
    (inferred / "case.ttl").write_text(_VALID_TTL, encoding="utf-8")
    sidecar = generated / "sidecar.ttl"
    sidecar.write_text(_VALID_TTL, encoding="utf-8")
    shapes = rules / "policy" / "custom_shapes.ttl"
    shapes.write_text(_VALID_TTL, encoding="utf-8")

    monkeypatch.setattr(
        cardinality_sync, "check_cardinality_sync",
        _record("cardinality", {"owl_only": [], "shacl_only": [], "inconsistent": []}),
    )
    result = json.loads(cardinality_sync.check_shacl_owl_cardinality_sync(
        tbox_path="case.ttl", shapes_path=str(shapes),
    ))
    assert result["success"] is True, result
    assert seen["cardinality"][0] == (
        str((tbox / "case.ttl").resolve()), str(shapes.resolve()),
    )

    monkeypatch.setattr(cwa, "summarize_origin_distribution", _record("origin", {}))
    assert json.loads(cwa.summarize_origin_sidecar(str(sidecar)))["success"] is True
    assert seen["origin"][0] == (str(sidecar.resolve()),)

    monkeypatch.setattr(farber, "evaluate_farber", _record("farber", {"overall_score": 0}))
    result = json.loads(farber.evaluate_farber_dimensions(
        confidence_sidecar_path=str(sidecar), provenance_sidecar_path=str(sidecar),
    ))
    assert result["success"] is True, result
    assert seen["farber"][1] == {
        "confidence_sidecar_path": str(sidecar.resolve()),
        "provenance_sidecar_path": str(sidecar.resolve()),
    }

    from rdflib import Graph

    monkeypatch.setattr(classify, "Graph", Graph)
    result = json.loads(classify.classify_inference_triples(inferred_path="case.ttl"))
    assert result["success"] is True, result
    assert result["source"] == str((inferred / "case.ttl").resolve())

    monkeypatch.setattr(ontoclean, "analyze_ontoclean", _record("ontoclean", {"violations": []}))
    monkeypatch.setattr(ontoclean, "write_deployed_sidecar", lambda *a, **k: False)
    assert json.loads(ontoclean.validate_ontoclean("case.ttl"))["success"] is True
    assert seen["ontoclean"][0] == (str((tbox / "case.ttl").resolve()),)

    from domain.tbox_utils import _new_graph

    monkeypatch.setattr(provenance, "_new_graph", _new_graph)
    result = json.loads(provenance.trace_provenance(
        "https://example.com/test#Thing", prov_path=str(sidecar),
    ))
    assert result["success"] is True, result
    result = json.loads(provenance.query_inference_justification(
        "https://example.com/test#a", "https://example.com/test#p",
        "https://example.com/test#b", prov_path=str(sidecar),
    ))
    assert result["success"] is True, result
    assert result["found"] is False


def test_rawdata_and_swrl_dirs_accept_base_and_subdirectory(tmp_path, monkeypatch):
    """CSV·SWRL 디렉터리는 기준 디렉터리 자신과 그 하위 디렉터리를 받는다."""
    import tools.drift_monitor as drift_monitor
    import tools.swrl_inference as swrl

    bases = _configure_safe_paths(drift_monitor, tmp_path, monkeypatch)
    _configure_safe_paths(swrl, tmp_path, monkeypatch)
    monkeypatch.setattr(swrl, "_load_swrl_rules", lambda _d: [])
    rawdata, swrl_dir = bases["rawdata"], bases["swrl"]
    sub = rawdata / "batch"
    sub.mkdir()
    calls: list = []
    monkeypatch.setattr(
        drift_monitor, "monitor_all_csvs",
        lambda rd, save: calls.append(rd) or {"tables": 0, "per_table": []},
    )
    monkeypatch.setattr(drift_monitor, "GENERATED_REPORTS_DIR", str(tmp_path / "reports"))

    for value in (str(rawdata), str(sub)):
        result = json.loads(drift_monitor.monitor_csv_drift(value, save_snapshot=False))
        assert result["success"] is True, result
    assert calls == [str(rawdata.resolve()), str(sub.resolve())]

    result = json.loads(swrl.run_swrl_inference(swrl_dir=str(swrl_dir)))
    assert result["success"] is True, result
    assert result["rules_loaded"] == 0


def test_glob_results_that_escape_the_directory_are_not_read(tmp_path, monkeypatch):
    """디렉터리 안의 symlink 가 밖 파일을 가리키면 CSV·SWRL 로 읽지 않는다."""
    import tools.drift_monitor as drift_monitor
    from tools.swrl_inference import _load_swrl_rules

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("ID\nS1\n", encoding="utf-8")
    (outside / "secret.swrl").write_text("leak: A(?x) -> B(?x)\n", encoding="utf-8")
    rawdata = tmp_path / "rawdata"
    rawdata.mkdir()
    (rawdata / "T.csv").write_text("ID\nR1\n", encoding="utf-8")
    (rawdata / "linked.csv").symlink_to(outside / "secret.csv")
    swrl_dir = tmp_path / "swrl"
    swrl_dir.mkdir()
    (swrl_dir / "ok.swrl").write_text("ok: A(?x) -> C(?x)\n", encoding="utf-8")
    (swrl_dir / "linked.swrl").symlink_to(outside / "secret.swrl")
    monkeypatch.setattr(drift_monitor, "DRIFT_DIR", str(tmp_path / "drift"))

    report = drift_monitor.monitor_all_csvs(str(rawdata), save=False)

    assert [t["table"] for t in report["per_table"]] == ["T"]
    assert report["skipped_outside_files"] == ["linked.csv"]
    labels = {rule["label"] for rule in _load_swrl_rules(str(swrl_dir))}
    assert labels == {"ok"}


def test_meta_audit_and_prompt_cache_keys_read_inside_entries(tmp_path, monkeypatch):
    """형식이 맞는 키와 latest 는 기준 디렉터리 안 파일을 그대로 읽는다."""
    import tools.meta_audit as meta_audit
    import tools.prompt_audit as prompt_audit

    bases = _configure_safe_paths(meta_audit, tmp_path, monkeypatch)
    _configure_safe_paths(prompt_audit, tmp_path, monkeypatch)
    audit_dir = bases["meta_audit"]
    (audit_dir / "20260101T000000Z.json").write_text('{"run": 1}', encoding="utf-8")
    (audit_dir / "latest.json").symlink_to("20260101T000000Z.json")

    for value in ("latest", "20260101T000000Z", "20260101T000000Z.json"):
        assert json.loads(meta_audit.read_meta_audit(value)) == {"run": 1}
    assert json.loads(meta_audit.read_meta_audit("20260102T000000Z"))["error"] == "not_found"

    key = "b" * 64
    (bases["prompt_cache"] / f"{key}.json").write_text(
        json.dumps({"request": {"model_id": "m"}, "response": {"text": "hi"}}),
        encoding="utf-8",
    )
    result = json.loads(prompt_audit.verify_prompt_reproducibility(key))
    assert result["success"] is True
    assert result["cached_request"] == {"model_id": "m"}


def test_tacit_rules_path_inside_rules_root_is_read(tmp_path, monkeypatch):
    """rules/ 아래 규칙 파일은 절대경로로도 읽고, 비어 있으면 실제 경로로 안내한다."""
    import tools.tacit_rules as tacit_rules

    bases = _configure_safe_paths(tacit_rules, tmp_path, monkeypatch)
    rules_file = bases["rules"] / "domain" / "custom_rules.json"
    rules_file.write_text('{"mappings": []}', encoding="utf-8")

    result = json.loads(tacit_rules.generate_tacit_from_rules(str(rules_file)))

    assert result["success"] is True, result
    assert result["rules_applied"] == 0
    assert "rules/domain/tacit_rules.example.steel.json" in result["note"]


# ── owl:imports 는 로컬 파일로만 해석한다 ─────────────────────────────


_LOCAL_ONTOLOGY_RDFXML = """\
<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/onto/Local/">
    <owl:imports rdf:resource="http://127.0.0.1:65530/nested"/>
  </owl:Ontology>
  <owl:Class rdf:about="http://example.org/onto/Local/LocalThing"/>
</rdf:RDF>
"""


@pytest.fixture
def owl_loader(tmp_path, monkeypatch):
    """owlready2 의 import 디렉터리를 tmp 로 바꾸고 모든 HTTP 요청을 기록 후 거부한다."""
    import urllib.request

    import owlready2

    from tools import owl_reasoner

    local_dir = tmp_path / "imports"
    local_dir.mkdir()
    (local_dir / "Local.rdf").write_text(_LOCAL_ONTOLOGY_RDFXML, encoding="utf-8")
    calls: list[str] = []

    def _blocked(url, *args, **kwargs):
        calls.append(str(getattr(url, "full_url", url)))
        raise OSError("테스트는 네트워크 요청을 허용하지 않는다")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    saved = list(owlready2.onto_path)
    owlready2.onto_path[:] = [str(local_dir)]
    owl_reasoner._cleanup_world()
    yield owl_reasoner, calls
    owl_reasoner._cleanup_world()
    owlready2.onto_path[:] = saved


def _ttl_importing(iri: str) -> str:
    return (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<http://example.org/onto/Top> a owl:Ontology ; owl:imports <{iri}> .\n"
        "<http://example.org/onto/Top#C> a owl:Class .\n"
    )


@pytest.mark.parametrize(
    "iri",
    ["http://127.0.0.1:65530/collect?d=local-data", "file:///nonexistent/onto.owl"],
)
def test_import_without_local_copy_is_rejected_without_request(owl_loader, iri):
    """입력이 직접 선언한 import 에 로컬 사본이 없으면 요청 없이 거부한다."""
    owl_reasoner, calls = owl_loader

    with pytest.raises(ValueError, match="로컬 사본이 없어"):
        owl_reasoner._ttl_to_owlready(_ttl_importing(iri))
    result = json.loads(owl_reasoner.validate_owl_consistency(ttl_content=_ttl_importing(iri)))

    assert result["success"] is False
    assert calls == []


def test_local_import_loads_and_unresolved_nested_import_is_not_requested(owl_loader):
    """로컬 사본은 읽고, 그 파일이 선언한 미해석 import 는 요청 없이 빈 온톨로지가 된다."""
    owl_reasoner, calls = owl_loader

    onto, tmp = owl_reasoner._ttl_to_owlready(_ttl_importing("http://example.org/onto/Local/"))
    try:
        imported = {o.base_iri for o in onto.imported_ontologies}
        world_classes = {c.iri for c in owl_reasoner.default_world.classes()}
    finally:
        owl_reasoner._safe_unlink(tmp)

    assert calls == []
    assert any(iri.startswith("http://example.org/onto/Local") for iri in imported), imported
    assert "http://example.org/onto/Local/LocalThing" in world_classes


@pytest.mark.parametrize(
    ("iri", "files"),
    [
        ("http://example.org/onto.owl", ("onto.rdf",)),
        ("http://example.org/onto.owl", ("onto",)),
        ("http://example.org/onto.owl", ("onto.rdf", "onto.owl")),
        ("http://example.org/onto.rdf", ("onto.nt", "onto.owl")),
        ("http://example.org/onto/Local/", ("Local.ntriples",)),
        ("http://example.org/vocab#", ("vocab.owl", "vocab")),
        ("http://example.org/onto.owl", ("other.owl",)),
    ],
)
def test_local_import_lookup_matches_owlready2_order(owl_loader, iri, files):
    """onto_path 후보와 순서는 owlready2 ``_get_onto_file`` 의 오프라인 해석과 같다."""
    import owlready2

    owl_reasoner, calls = owl_loader
    local_dir = Path(owlready2.onto_path[0])
    for name in files:
        (local_dir / name).write_bytes(owl_reasoner._EMPTY_ONTOLOGY_RDFXML)

    onto = owl_reasoner.default_world.get_ontology(iri)
    try:
        expected = owlready2.namespace._get_onto_file(
            onto._orig_base_iri, onto.name, "r", only_local=True,
        )
    except FileNotFoundError:
        expected = None
    actual = owl_reasoner._local_import_file(iri)

    assert (actual and os.path.realpath(actual)) == (expected and os.path.realpath(expected))
    assert calls == []


def test_import_iri_with_owl_suffix_loads_rdf_copy_without_request(owl_loader):
    """``.owl`` IRI 의 로컬 사본이 ``.rdf`` 이름이어도 요청 없이 그 사본을 읽는다."""
    import owlready2

    owl_reasoner, calls = owl_loader
    (Path(owlready2.onto_path[0]) / "onto.rdf").write_text(
        '<?xml version="1.0"?>\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        '         xmlns:owl="http://www.w3.org/2002/07/owl#">\n'
        '  <owl:Ontology rdf:about="http://example.org/onto.owl"/>\n'
        '  <owl:Class rdf:about="http://example.org/onto.owl#RdfCopyThing"/>\n'
        "</rdf:RDF>\n",
        encoding="utf-8",
    )

    onto, tmp = owl_reasoner._ttl_to_owlready(_ttl_importing("http://example.org/onto.owl"))
    try:
        world_classes = {c.iri for c in owl_reasoner.default_world.classes()}
    finally:
        owl_reasoner._safe_unlink(tmp)

    assert calls == []
    assert "http://example.org/onto.owl#RdfCopyThing" in world_classes


# ── tacit 규칙의 CSV 값과 규칙 필드는 Turtle 문장을 만들지 못한다 ─────────


@pytest.fixture
def tacit_sandbox(tmp_path, monkeypatch):
    import config
    import tools.tacit_rules as tacit_rules

    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    rules = tmp_path / "rules"
    for directory in (rawdata, tacit, rules):
        directory.mkdir()
    monkeypatch.setattr(tacit_rules, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tacit_rules, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(tacit_rules, "RULES_ROOT", str(rules), raising=False)
    # T-Box 가 없으면 OP 방향 사전 검증을 건너뛴다.
    monkeypatch.setattr(config, "TBOX_PATH", str(tmp_path / "missing_t_box.ttl"))

    def _run(rows=(("AL1", "EQ001"),), **overrides):
        lines = ["Alarm_ID,Equipment_ID"] + [f'{pk},"{fk}"' for pk, fk in rows]
        (rawdata / "Alarm.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        rule = {
            "name": "alarm_equipment",
            "strategy": "simple_join",
            "source_csv": "Alarm.csv",
            "source_class": "AlarmEvents",
            "source_pk_column": "Alarm_ID",
            "source_fk_column": "Equipment_ID",
            "target_class": "EquipmentMaster",
            "op": "alarmOnEquipment",
            "output_file": "alarm.ttl",
            "_confidence": "medium",
            "_reasoning": "line1\nsteel-inst:Evil steel:p steel-inst:Z .",
            **overrides,
        }
        rules_file = rules / "rules.json"
        rules_file.write_text(json.dumps({"mappings": [rule]}), encoding="utf-8")
        result = json.loads(tacit_rules.generate_tacit_from_rules(str(rules_file)))
        out = tacit / "alarm.ttl"
        return result, (out.read_text(encoding="utf-8") if out.exists() else "")

    return _run


def _triples(ttl: str) -> set[tuple[str, str, str]]:
    from rdflib import Graph

    return {(str(s), str(p), str(o)) for s, p, o in Graph().parse(data=ttl, format="turtle")}


@pytest.mark.parametrize(
    "fk",
    [
        "EQ001 . steel-inst:Injected a steel:EquipmentMaster",
        "EQ 002",
        "설비_003",
    ],
)
def test_csv_values_are_encoded_like_the_abox(tacit_sandbox, fk):
    """CSV 값은 A-Box 와 같은 IRI 가 되고, 구두점·공백으로 트리플을 더하거나 파일을 깨지 못한다.

    규칙의 ``_reasoning`` 에 든 줄바꿈도 주석 밖으로 나가 트리플이 되지 못한다.
    """
    from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
    from tools.abox_generation import _pk_safe_local

    result, ttl = tacit_sandbox(rows=(("AL1", fk),))

    assert _triples(ttl) == {(
        f"{DOMAIN_INST_NS}AlarmEvents_AL1",
        f"{DOMAIN_NS}alarmOnEquipment",
        f"{DOMAIN_INST_NS}EquipmentMaster_{_pk_safe_local(fk)}",
    )}
    assert result["success"] is True, result
    assert result["files_rejected"] == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"target_class": "EquipmentMaster_X a steel:Evil . steel-inst:Y"}, "IRI local name"),
        ({"op": "p . steel-inst:A steel:q steel-inst:B"}, "IRI local name"),
    ],
)
def test_rule_identifiers_that_would_end_the_statement_are_rejected(
    tacit_sandbox, override, message
):
    """규칙의 클래스·프로퍼티 이름이 문장을 끝내는 값이면 그 규칙을 쓰지 않는다."""
    result, ttl = tacit_sandbox(**override)

    assert result["success"] is True, result
    (rule,) = result["per_rule"]
    assert rule["triples"] == 0
    assert message in rule["error"], rule
    assert ttl == ""
