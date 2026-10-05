"""공개 MCP 도구별 최소 schema 계약."""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

from app import build_server

CONTRACT_TOOL_NAMES = (
    "add_golden_queries",
    "add_tacit_from_natural_language",
    "analyze_causal_rules",
    "analyze_graph_topology",
    "analyze_inference_quality",
    "analyze_tbox",
    "annotate_lpg_graph_context",
    "ask_neo4j",
    "ask_ontology",
    "augment_csv_fk",
    "bump_tbox_version",
    "canonicalize_graph",
    "check_competency_questions_exist",
    "check_cq_coverage",
    "check_golden_queries_exist",
    "check_owl2_profile",
    "check_pipeline_state",
    "check_quality_rules",
    "check_shacl_owl_cardinality_consistency",
    "check_tacit_exist",
    "classify_inference_triples",
    "classify_tbox",
    "compare_canonical",
    "compare_tbox_baseline",
    "convert_rdf_to_lpg",
    "convert_sensor_to_rdf",
    "detect_failure_patterns",
    "detect_tbox_antipatterns",
    "estimate_change_impact",
    "evaluate_fair_score",
    "evaluate_farber_dimensions",
    "evaluate_miro",
    "evaluate_oquare",
    "export_quality_dqv",
    "generate_abox",
    "generate_competency_questions",
    "generate_csv_erd",
    "generate_lpg_semantic_dictionary",
    "generate_pipeline_report",
    "generate_quality_dashboard",
    "generate_schema_driven_tests",
    "generate_semantic_dictionary",
    "generate_table_class_map_report",
    "generate_tacit_from_data",
    "generate_tacit_from_rules",
    "generate_tbox",
    "generate_tbox_collaborative",
    "get_golden_history",
    "get_inference_status",
    "get_meta_audit_history",
    "get_pipeline_quality_history",
    "get_quality_history",
    "get_tbox_status",
    "golden_queries_schema_example",
    "graphdb_count_triples",
    "graphdb_create_repository",
    "graphdb_export_inferred",
    "graphdb_health",
    "graphdb_import_file",
    "graphdb_run_inference",
    "graphdb_sparql_construct",
    "graphdb_sparql_select",
    "graphdb_sparql_update",
    "improve_tbox_quality",
    "initialize_domain_rules",
    "list_csv_tables",
    "list_prompt_cache",
    "list_tacit_files",
    "measure_instance_quality",
    "measure_tbox_metrics",
    "monitor_csv_drift",
    "neo4j_deploy_lpg",
    "neo4j_query",
    "neo4j_stats",
    "ontology_agent_health_check",
    "profile_csv_data",
    "query_inference_justification",
    "read_abox",
    "read_competency_questions",
    "read_csv_schema",
    "read_inferred",
    "read_inferred_delta",
    "read_iof_mapping",
    "read_lpg_semantic_dictionary",
    "read_meta_audit",
    "read_semantic_dictionary",
    "read_tacit",
    "read_tbox",
    "reset_pipeline_state",
    "resolve_entity_references",
    "run_adversarial_csv_suite",
    "run_entailment_regression",
    "run_golden_queries",
    "run_kg_mutation_audit",
    "run_meta_audit",
    "run_owl_rl_inference",
    "run_partial_pipeline_on_drift",
    "run_swrl_inference",
    "run_tbox_mutation_audit",
    "save_step",
    "skip_golden_regression",
    "skip_tacit_knowledge",
    "sparql_local",
    "sparql_local_reload",
    "suggest_tacit_rules",
    "summarize_confidence_sidecar",
    "summarize_origin_sidecar",
    "test_domain_queries",
    "trace_provenance",
    "trace_quality_issues",
    "update_tbox_incremental",
    "validate_competency_questions",
    "validate_cq_tbox_only",
    "validate_kg",
    "validate_ontoclean",
    "validate_owl_cardinality",
    "validate_owl_consistency",
    "validate_owl_justification",
    "validate_owl_realisation",
    "validate_rules",
    "validate_semantic_dictionary",
    "validate_shacl",
    "validate_tbox_shacl",
    "validate_ttl_syntax",
    "verify_cross_store_parity",
    "verify_prompt_reproducibility",
    "verify_roundtrip_fidelity",
    "verify_sparql_cypher_parity",
    "visualize_tbox",
)


@lru_cache(maxsize=1)
def _tools_by_name():
    """한 서버의 공개 도구를 이름으로 색인한다."""
    tools = asyncio.run(build_server().list_tools())
    return {tool.name: tool for tool in tools}


def _assert_tool_contract(name: str) -> None:
    """도구 하나의 이름, schema, 설명, hint 계약을 검증한다."""
    tool = _tools_by_name()[name]

    assert tool.name == name
    assert tool.inputSchema["type"] == "object"
    assert re.match(r"^[A-Za-z]", (tool.description or "").lstrip())
    assert tool.annotations is not None
    assert isinstance(tool.annotations.destructiveHint, bool)
    # 읽기 전용 도구는 파괴적일 수 없다. 도구별 기대값은 test_registry_tool_annotations 가 고정한다.
    assert not (tool.annotations.readOnlyHint and tool.annotations.destructiveHint)


def _make_contract_test(name: str):
    """정적 이름 목록에서 pytest가 수집할 개별 계약 함수를 만든다."""
    def contract_test():
        _assert_tool_contract(name)

    contract_test.__name__ = f"test_{name}_contract"
    return contract_test


for _tool_name in CONTRACT_TOOL_NAMES:
    globals()[f"test_{_tool_name}_contract"] = _make_contract_test(_tool_name)

del _tool_name
