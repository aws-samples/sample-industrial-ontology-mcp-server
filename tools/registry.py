"""manifest 기반 MCP 도구를 명시적으로 등록한다."""

from __future__ import annotations

import importlib
import inspect
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# domain 패키지 import 가 프로세스 전체 SPARQL egress 차단점을 설치한다. 도구 타깃을
# 해석하기 전이라도 이 모듈을 import 하면 차단점이 설치되도록 명시적으로 import 한다.
import domain  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "mcp-tools.toml"


class ToolRegistrationError(RuntimeError):
    """공개 도구 계약을 완전하게 등록할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class ToolSpec:
    """manifest 이름과 명시적 Python 타깃 및 MCP hint 계약."""

    name: str
    target: str
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


# 파일시스템 탐색 대신 이 타깃만 import한다. 공개 이름 집합은 이 목록이 아니라
# mcp-tools.toml의 [mcp].tools에서 읽고, 아래 타깃의 해석 결과와 동일한지 단정한다.
PUBLIC_TOOL_TARGETS: tuple[str, ...] = (
    "tools.golden_queries:add_golden_queries",
    "tools.tacit_input:add_tacit_from_natural_language",
    "tools.pattern_detection:analyze_causal_rules",
    "tools.topology:analyze_graph_topology",
    "tools.inference:analyze_inference_quality",
    "tools.tbox_metrics:analyze_tbox",
    "tools.lpg_graph_context:annotate_lpg_graph_context",
    "tools.bedrock:ask_neo4j",
    "tools.bedrock:ask_ontology",
    "tools.tacit_rules:augment_csv_fk",
    "tools.version_management:bump_tbox_version",
    "tools.canonical:canonicalize_graph",
    "tools.competency_questions:check_competency_questions_exist",
    "tools.competency_questions:check_cq_coverage",
    "tools.golden_queries:check_golden_queries_exist",
    "tools.owl_reasoner:check_owl2_profile",
    "tools.pipeline_state:check_pipeline_state",
    "tools.validation_core:check_quality_rules",
    "tools.cardinality_sync:check_shacl_owl_cardinality_sync",
    "tools.tacit_input:check_tacit_exist",
    "tools.inference_classify:classify_inference_triples",
    "tools.owl_reasoner:classify_tbox",
    "tools.canonical:compare_canonical",
    "tools.tbox_metrics:compare_tbox_baseline",
    "tools.remote.neo4j:convert_rdf_to_lpg",
    "tools.bedrock:convert_sensor_to_rdf",
    "tools.pattern_detection:detect_failure_patterns",
    "tools.tbox_metrics:detect_tbox_antipatterns",
    "tools.quality_dashboard:estimate_change_impact",
    "tools.foops_fair:evaluate_fair_score",
    "tools.farber_dimensions:evaluate_farber_dimensions",
    "tools.foops_fair:evaluate_miro",
    "tools.ontology_quality:evaluate_oquare",
    "tools.dqv_sidecar:export_quality_dqv",
    "tools.abox_generation:generate_abox",
    "tools.competency_questions:generate_competency_questions",
    "tools.local_artifacts:generate_csv_erd",
    "tools.remote.neo4j:generate_lpg_semantic_dictionary",
    "tools.report:generate_pipeline_report",
    "tools.quality_dashboard:generate_quality_dashboard",
    "tools.kg_validation:generate_schema_driven_tests",
    "tools.semantic_dictionary:generate_semantic_dictionary",
    "tools.local_artifacts:generate_table_class_map_report",
    "tools.tacit_input:generate_tacit_from_data",
    "tools.tacit_rules:generate_tacit_from_rules",
    "tools.tbox_generation:generate_tbox",
    "tools.multi_agent_tbox:generate_tbox_collaborative",
    "tools.golden_queries:get_golden_history",
    "tools.inference:get_inference_status",
    "tools.meta_audit:get_meta_audit_history",
    "tools.pipeline_state:get_pipeline_quality_history",
    "tools.kg_validation:get_quality_history",
    "tools.multi_agent_tbox:get_tbox_status",
    "tools.golden_queries:golden_queries_schema_example",
    "tools.remote.graphdb:graphdb_count_triples",
    "tools.remote.graphdb:graphdb_create_repository",
    "tools.remote.graphdb:graphdb_export_inferred",
    "tools.remote.graphdb:graphdb_health",
    "tools.remote.graphdb:graphdb_import_file",
    "tools.remote.graphdb:graphdb_run_inference",
    "tools.remote.graphdb:graphdb_sparql_construct",
    "tools.remote.graphdb:graphdb_sparql_select",
    "tools.remote.graphdb:graphdb_sparql_update",
    "tools.ontology_quality:improve_tbox_quality",
    "tools.rules_init:initialize_domain_rules",
    "tools.local_artifacts:list_csv_tables",
    "tools.prompt_audit:list_prompt_cache",
    "tools.local_artifacts:list_tacit_files",
    "tools.kg_validation:measure_instance_quality",
    "tools.tbox_metrics:measure_tbox_metrics",
    "tools.drift_monitor:monitor_csv_drift",
    "tools.remote.neo4j:neo4j_deploy_lpg",
    "tools.remote.neo4j:neo4j_query",
    "tools.remote.neo4j:neo4j_stats",
    "tools.pipeline_state:health_check",
    "tools.local_artifacts:profile_csv_data",
    "tools.provenance:query_inference_justification",
    "tools.local_artifacts:read_abox",
    "tools.competency_questions:read_competency_questions",
    "tools.local_artifacts:read_csv_schema",
    "tools.local_artifacts:read_inferred",
    "tools.inference:read_inferred_delta",
    "tools.local_artifacts:read_iof_mapping",
    "tools.semantic_dictionary:read_lpg_semantic_dictionary",
    "tools.meta_audit:read_meta_audit",
    "tools.semantic_dictionary:read_semantic_dictionary",
    "tools.local_artifacts:read_tacit",
    "tools.local_artifacts:read_tbox",
    "tools.pipeline_state:reset_pipeline_state",
    "tools.entity_resolution:resolve_entity_references",
    "tools.adversarial:run_adversarial_csv_suite",
    "tools.entailment_regression:run_entailment_regression",
    "tools.golden_queries:run_golden_queries",
    "tools.mutation_runner:run_kg_mutation_audit",
    "tools.meta_audit:run_meta_audit",
    "tools.inference:run_owl_rl_inference",
    "tools.drift_partial_pipeline:run_partial_pipeline_on_drift",
    "tools.swrl_inference:run_swrl_inference",
    "tools.mutation_runner:run_tbox_mutation_audit",
    "tools.pipeline_state:save_step",
    "tools.golden_queries:skip_golden_regression",
    "tools.tacit_input:skip_tacit_knowledge",
    "tools.sparql_local:sparql_local",
    "tools.sparql_local:sparql_local_reload",
    "tools.tacit_rules:suggest_tacit_rules",
    "tools.triple_confidence:summarize_confidence_sidecar",
    "tools.cwa_owa_tagging:summarize_origin_sidecar",
    "tools.query_test:test_domain_queries",
    "tools.provenance:trace_provenance",
    "tools.quality_dashboard:trace_quality_issues",
    "tools.tbox_generation:update_tbox_incremental",
    "tools.competency_questions:validate_competency_questions",
    "tools.query_test:validate_cq_tbox_only",
    "tools.kg_validation:validate_kg",
    "tools.ontoclean:validate_ontoclean",
    "tools.validation_core:validate_owl_cardinality",
    "tools.owl_reasoner:validate_owl_consistency",
    "tools.owl_reasoner:validate_owl_justification",
    "tools.owl_reasoner:validate_owl_realisation",
    "tools.validate_rules:validate_rules",
    "tools.semantic_dict_validation:validate_semantic_dictionary",
    "tools.validation_core:validate_shacl",
    "tools.validation_core:validate_tbox_shacl",
    "tools.validation_core:validate_ttl_syntax",
    "tools.cross_store_validation:verify_cross_store_parity",
    "tools.prompt_audit:verify_prompt_reproducibility",
    "tools.roundtrip:verify_roundtrip_fidelity",
    "tools.remote.neo4j:verify_sparql_cypher_parity",
    "tools.visualization:visualize_tbox",
)

_NAME_OVERRIDES = {
    "tools.cardinality_sync:check_shacl_owl_cardinality_sync":
        "check_shacl_owl_cardinality_consistency",
    "tools.pipeline_state:health_check": "ontology_agent_health_check",
}

_READ_ONLY_PREFIXES = (
    "analyze_",
    "ask_",
    "check_",
    "classify_",
    "compare_",
    "compute_",
    "detect_",
    "estimate_",
    "evaluate_",
    "get_",
    "golden_queries_schema_example",
    "list_",
    "measure_",
    "monitor_",
    "ontology_agent_health_check",
    "profile_",
    "query_",
    "read_",
    "sparql_local",
    "summarize_",
    "test_",
    "trace_",
    "validate_",
    "verify_",
)

# 아래 네 집합이 MCP hint 의 정본이다. 이름 접두사는 첫 근사일 뿐이고, 각 도구의
# 구현이 실제로 쓰는 대상과 호출하는 외부 서비스로 분류한다. 집합의 이름은 모두
# manifest 에 있어야 하며, 없으면 등록이 실패한다 (_resolved_tools).

# 읽기 접두사를 가졌지만 파일을 쓰는 도구. readOnlyHint 를 주지 않는다.
_WRITES_DESPITE_READ_PREFIX = frozenset({
    "analyze_graph_topology",      # reports/topology_report.json
    "compare_canonical",           # reports/canonical_compare.json
    "evaluate_fair_score",         # reports/fair_score.json
    "evaluate_farber_dimensions",  # reports/farber_dimensions.json
    "monitor_csv_drift",           # drift baseline 스냅샷 전진, reports/drift_report.json
    "test_domain_queries",         # reports/query_test_report.html, cq_feedback.json 누적
    "validate_kg",                 # quality_history.json 누적
    "validate_ontoclean",          # reports/ontoclean_report.json
    "verify_cross_store_parity",   # reports/cross_store_parity.json
    "verify_roundtrip_fidelity",   # reports/roundtrip_fidelity.json
})

# 읽기 접두사는 없지만 파일, 저장소, 원격 상태를 바꾸지 않는 도구.
_READ_ONLY_WITHOUT_PREFIX = frozenset({
    "canonicalize_graph",
    "convert_sensor_to_rdf",       # Bedrock 이 만든 TTL 을 응답으로만 돌려준다
    "generate_quality_dashboard",
    "generate_schema_driven_tests",
    "graphdb_count_triples",
    "graphdb_health",
    "graphdb_sparql_construct",    # query 엔드포인트만 호출한다 (update 는 별도 도구)
    "graphdb_sparql_select",
    "neo4j_query",                 # write/admin clause 를 거부한다
    "neo4j_stats",
    "resolve_entity_references",
    "run_entailment_regression",
})

# 읽기 전용이 아닌 도구는 destructiveHint=True 가 기본이다. 아래는 쓰기가 보고서와
# 감사 산출물, 누적 이력, skip 마커, 자기 단계의 체크포인트 항목에만 한정되어 기존
# 데이터를 지우거나 덮어쓰지 않는 도구다. 사용자 입력(CSV, tacit, rules, CQ, golden
# query), KG 산출물(T-Box, A-Box, 추론 그래프, 딕셔너리, LPG CSV), 파이프라인 상태와
# drift baseline, 원격 저장소, 호출자가 지정한 경로를 덮어쓰거나 지울 수 있는 도구는
# 여기에 넣지 않는다.
_ADDITIVE_WRITE_TOOLS = frozenset({
    "analyze_graph_topology",
    "compare_canonical",
    "evaluate_fair_score",
    "evaluate_farber_dimensions",
    "generate_csv_erd",
    "generate_pipeline_report",
    "generate_table_class_map_report",
    "run_adversarial_csv_suite",
    "run_golden_queries",
    "run_kg_mutation_audit",
    "run_meta_audit",
    "run_tbox_mutation_audit",
    "save_step",
    "skip_golden_regression",
    "skip_tacit_knowledge",
    "test_domain_queries",
    "validate_kg",
    "validate_ontoclean",
    "verify_cross_store_parity",
    "verify_roundtrip_fidelity",
    "visualize_tbox",
})

# Bedrock 또는 원격 저장소(Neo4j, GraphDB)를 호출할 수 있는 도구. 모듈 경로가 아니라
# 실제 호출 여부로 정한다. 조건부 호출도 포함한다.
_OPEN_WORLD_TOOLS = frozenset({
    "add_tacit_from_natural_language",
    "ask_neo4j",
    "ask_ontology",
    "convert_sensor_to_rdf",
    "generate_competency_questions",
    "generate_semantic_dictionary",
    "generate_tacit_from_data",
    "generate_tbox",
    "generate_tbox_collaborative",
    "graphdb_count_triples",
    "graphdb_create_repository",
    "graphdb_export_inferred",
    "graphdb_health",
    "graphdb_import_file",
    "graphdb_run_inference",
    "graphdb_sparql_construct",
    "graphdb_sparql_select",
    "graphdb_sparql_update",
    "initialize_domain_rules",
    "neo4j_deploy_lpg",
    "neo4j_query",
    "neo4j_stats",
    "ontology_agent_health_check",
    "run_partial_pipeline_on_drift",
    "suggest_tacit_rules",
    "update_tbox_incremental",
    "verify_cross_store_parity",
    "verify_sparql_cypher_parity",
})

_HINT_NAME_SETS = {
    "_WRITES_DESPITE_READ_PREFIX": _WRITES_DESPITE_READ_PREFIX,
    "_READ_ONLY_WITHOUT_PREFIX": _READ_ONLY_WITHOUT_PREFIX,
    "_ADDITIVE_WRITE_TOOLS": _ADDITIVE_WRITE_TOOLS,
    "_OPEN_WORLD_TOOLS": _OPEN_WORLD_TOOLS,
}


def load_public_tool_names(path: Path = MANIFEST_PATH) -> tuple[str, ...]:
    """mcp-tools.toml에서 중복 없는 공개 도구 이름을 읽는다."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolRegistrationError(
            f"tool manifest를 읽을 수 없습니다: {path}: {exc}"
        ) from exc
    if set(data) != {"schema_version", "mcp"}:
        raise ToolRegistrationError(
            "tool manifest의 top-level 키는 schema_version과 mcp여야 합니다."
        )
    if data["schema_version"] != 1:
        raise ToolRegistrationError(
            "지원하지 않는 tool manifest schema_version입니다: "
            f"{data['schema_version']!r}"
        )
    mcp = data.get("mcp")
    if not isinstance(mcp, dict) or set(mcp) != {"tools"}:
        raise ToolRegistrationError(
            "tool manifest의 [mcp] table은 tools 키만 가져야 합니다."
        )
    names = mcp["tools"]
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name for name in names)
    ):
        raise ToolRegistrationError(
            "tool manifest의 [mcp].tools는 빈 문자열이 없는 배열이어야 합니다."
        )
    if len(names) != len(set(names)):
        raise ToolRegistrationError(
            "tool manifest의 [mcp].tools에 중복 이름이 있습니다."
        )
    return tuple(names)


def _split_target(target: str) -> tuple[str, str]:
    """module:function 타깃을 검증해 두 부분으로 나눈다."""
    module_name, separator, function_name = target.partition(":")
    if (
        not separator
        or not module_name
        or not function_name
        or ":" in function_name
    ):
        raise ToolRegistrationError(f"잘못된 tool target 형식: {target}")
    return module_name, function_name


def _resolve_target(target: str) -> Callable[..., Any]:
    """명시적 타깃을 import하고 callable인지 검증한다."""
    module_name, function_name = _split_target(target)
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
    except Exception as exc:
        raise ToolRegistrationError(
            f"tool target을 해석할 수 없습니다: {target}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not callable(function):
        raise ToolRegistrationError(f"tool target이 callable이 아닙니다: {target}")
    return function


def _target_name(target: str) -> str:
    """타깃의 기본 함수명에 명시적 FastMCP override를 적용한다."""
    return _NAME_OVERRIDES.get(target, target.rsplit(":", 1)[1])


def _tool_spec(name: str, target: str) -> ToolSpec:
    """도구의 실제 쓰기, 삭제, 외부 호출 여부로 MCP hint 계약을 만든다.

    읽기 전용이 아닌 도구는 추가 전용으로 확인된 경우에만 destructiveHint=False 다.
    분류가 빠진 쓰기 도구는 파괴적으로 게시되어 클라이언트의 자동 승인을 받지 않는다.
    """
    read_only = name in _READ_ONLY_WITHOUT_PREFIX or (
        name.startswith(_READ_ONLY_PREFIXES)
        and name not in _WRITES_DESPITE_READ_PREFIX
    )
    return ToolSpec(
        name=name,
        target=target,
        read_only=read_only,
        destructive=not read_only and name not in _ADDITIVE_WRITE_TOOLS,
        idempotent=read_only,
        open_world=name in _OPEN_WORLD_TOOLS,
    )


def _english_first_description(
    spec: ToolSpec,
    function: Callable[..., Any],
) -> str:
    """한국어 docstring을 유지하면서 게시 설명은 영문으로 시작하게 만든다."""
    summary = spec.name.replace("_", " ")
    description = f"Use the {summary} tool in the ontology workflow."
    source_doc = inspect.getdoc(function)
    if source_doc:
        return f"{description}\n\n{source_doc}"
    return description


def _resolved_tools(
    tool_names: tuple[str, ...],
) -> list[tuple[ToolSpec, Callable[..., Any]]]:
    """모든 타깃을 먼저 해석하고 manifest와 이름 집합을 대조한다."""
    failures: list[str] = []
    functions_by_name: dict[str, tuple[str, Callable[..., Any]]] = {}
    for target in PUBLIC_TOOL_TARGETS:
        try:
            function = _resolve_target(target)
        except ToolRegistrationError as exc:
            failures.append(str(exc))
            continue
        name = _target_name(target)
        if name in functions_by_name:
            failures.append(
                f"중복 tool name: {name}: "
                f"{functions_by_name[name][0]}, {target}"
            )
            continue
        functions_by_name[name] = (target, function)
    if failures:
        raise ToolRegistrationError("; ".join(failures))

    manifest_names = set(tool_names)
    target_names = set(functions_by_name)
    if target_names != manifest_names:
        missing = sorted(manifest_names - target_names)
        unexpected = sorted(target_names - manifest_names)
        raise ToolRegistrationError(
            "manifest와 registry target 이름 집합이 다릅니다: "
            f"missing_targets={missing}, unexpected_targets={unexpected}"
        )
    # hint 집합의 오타는 그 도구를 조용히 기본 분류로 떨어뜨리므로 등록을 막는다.
    unknown_hint_names = {
        set_name: sorted(names - manifest_names)
        for set_name, names in _HINT_NAME_SETS.items()
        if names - manifest_names
    }
    if unknown_hint_names:
        raise ToolRegistrationError(
            "MCP hint 집합에 manifest에 없는 도구 이름이 있습니다: "
            f"{unknown_hint_names}"
        )

    resolved = []
    for name in tool_names:
        target, function = functions_by_name[name]
        resolved.append((_tool_spec(name, target), function))
    return resolved


def unresolvable_targets() -> list[str]:
    """현재 manifest 기준으로 해석할 수 없는 타깃 오류를 반환한다."""
    try:
        _resolved_tools(load_public_tool_names())
    except ToolRegistrationError as exc:
        return [str(exc)]
    return []


def register_public_tools(server: FastMCP) -> None:
    """전체 계약 검증에 성공한 뒤 공개 도구를 한 번에 등록한다."""
    resolved = _resolved_tools(load_public_tool_names())
    for spec, function in resolved:
        server.add_tool(
            function,
            name=spec.name,
            description=_english_first_description(spec, function),
            annotations=ToolAnnotations(
                readOnlyHint=spec.read_only,
                destructiveHint=spec.destructive,
                idempotentHint=spec.idempotent,
                openWorldHint=spec.open_world,
            ),
        )
