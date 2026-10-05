"""공개 MCP 도구의 hint 가 구현의 실제 부수효과와 일치하는지 고정한다.

MCP 클라이언트는 readOnlyHint 와 destructiveHint 로 자동 승인 여부를 정할 수 있다.
데이터를 지우는 도구가 non-destructive 로, 파일을 쓰는 도구가 read-only 로 게시되면
확인 없이 실행될 수 있으므로 분류 전체를 이름 집합으로 고정한다.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

import pytest

# 기존 사용자 데이터, KG 산출물, 파이프라인 상태, 원격 저장소, 호출자 지정 경로를
# 지우거나 덮어쓸 수 있는 도구.
EXPECTED_DESTRUCTIVE = frozenset({
    "add_golden_queries",               # append=False 면 SME golden query 전체 대체
    "add_tacit_from_natural_language",  # overwrite=True 면 data/source/tacit 파일 대체
    "annotate_lpg_graph_context",       # 호출자가 지정한 CSV 를 제자리에서 다시 쓴다
    "augment_csv_fk",                   # data/source/rawdata CSV 를 다시 쓴다
    "bump_tbox_version",                # T-Box 를 제자리에서 다시 쓴다
    "convert_rdf_to_lpg",               # LPG nodes.csv / relationships.csv 대체
    "export_quality_dqv",               # 호출자가 지정한 output_path 를 덮어쓴다
    "generate_abox",                    # A-Box 와 master data 대체
    "generate_competency_questions",    # data/source/query_tests 의 CQ 대체
    "generate_lpg_semantic_dictionary",  # LPG 딕셔너리 대체
    "generate_semantic_dictionary",     # include_stats=False 가 통계를 0 으로 되돌린다
    "generate_tacit_from_data",         # overwrite=True 면 tacit 파일 대체
    "generate_tacit_from_rules",        # tacit_rules.json 이 지정한 tacit 파일 대체
    "generate_tbox",                    # T-Box 대체
    "generate_tbox_collaborative",      # T-Box 대체
    "graphdb_create_repository",        # overwrite=True 면 repository 삭제
    "graphdb_export_inferred",          # 추론 그래프 또는 지정 경로 덮어쓰기
    "graphdb_import_file",              # replace=True 면 기존 데이터 삭제
    "graphdb_run_inference",            # overwrite_repo=True 면 repository 삭제
    "graphdb_sparql_update",            # SPARQL DELETE / DROP / CLEAR
    "improve_tbox_quality",             # T-Box 를 제자리에서 다시 쓴다
    "initialize_domain_rules",          # overwrite=True 면 SME 가 다듬은 rules 대체
    "monitor_csv_drift",                # drift baseline 을 되돌릴 수 없게 전진시킨다
    "neo4j_deploy_lpg",                 # replace=True 면 전체 노드 DETACH DELETE
    "reset_pipeline_state",             # 파이프라인 상태 파일 삭제
    "run_owl_rl_inference",             # 추론 그래프 대체
    "run_partial_pipeline_on_drift",    # update_tbox_incremental 과 baseline 전진
    "run_swrl_inference",               # 추론 그래프를 제자리에서 다시 쓴다
    "suggest_tacit_rules",              # rules/domain/tacit_rules.suggested.json 대체
    "update_tbox_incremental",          # 백업 없이 T-Box 대체
})

# 쓰기가 보고서, 감사 산출물, 누적 이력, skip 마커, 자기 체크포인트 항목에 한정된 도구.
EXPECTED_ADDITIVE_WRITERS = frozenset({
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

# 읽기 접두사가 없지만 아무것도 쓰지 않아 read-only 로 게시되는 도구.
EXPECTED_READ_ONLY_WITHOUT_PREFIX = frozenset({
    "canonicalize_graph",
    "convert_sensor_to_rdf",
    "generate_quality_dashboard",
    "generate_schema_driven_tests",
    "graphdb_count_triples",
    "graphdb_health",
    "graphdb_sparql_construct",
    "graphdb_sparql_select",
    "neo4j_query",
    "neo4j_stats",
    "resolve_entity_references",
    "run_entailment_regression",
})

# Bedrock 또는 Neo4j / GraphDB 를 호출할 수 있는 도구.
EXPECTED_OPEN_WORLD = frozenset({
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


@lru_cache(maxsize=1)
def _annotations_by_name():
    """한 서버가 게시하는 도구별 annotation 을 이름으로 색인한다."""
    from app import build_server

    tools = asyncio.run(build_server().list_tools())
    return {tool.name: tool.annotations for tool in tools}


def _names_where(field: str) -> set[str]:
    """지정한 hint 가 True 인 도구 이름 집합을 돌려준다."""
    return {
        name
        for name, hints in _annotations_by_name().items()
        if getattr(hints, field) is True
    }


@pytest.mark.parametrize(
    "name",
    [
        "neo4j_deploy_lpg",
        "graphdb_create_repository",
        "graphdb_sparql_update",
        "graphdb_import_file",
        "graphdb_run_inference",
        "reset_pipeline_state",
        "update_tbox_incremental",
        "initialize_domain_rules",
        "add_golden_queries",
        "monitor_csv_drift",
    ],
)
def test_data_deleting_tools_are_advertised_destructive(name):
    """기존 데이터를 지우거나 대체하는 도구는 destructive 이고 read-only 가 아니다."""
    hints = _annotations_by_name()[name]

    assert hints.destructiveHint is True
    assert hints.readOnlyHint is False


@pytest.mark.parametrize("name", sorted(
    {"test_domain_queries", "monitor_csv_drift", "validate_kg"}
))
def test_file_writing_tools_are_not_advertised_read_only(name):
    """읽기 접두사가 있어도 파일을 쓰는 도구는 read-only 로 게시하지 않는다."""
    assert _annotations_by_name()[name].readOnlyHint is False


def test_destructive_set_is_pinned():
    """destructiveHint=True 집합은 구현 검토로 확정한 집합과 정확히 같다."""
    assert _names_where("destructiveHint") == EXPECTED_DESTRUCTIVE


def test_read_only_set_is_pinned():
    """read-only 집합은 쓰기 도구 전체를 뺀 나머지와 정확히 같다."""
    from tools.registry import load_public_tool_names

    writers = EXPECTED_DESTRUCTIVE | EXPECTED_ADDITIVE_WRITERS
    assert not EXPECTED_DESTRUCTIVE & EXPECTED_ADDITIVE_WRITERS
    assert _names_where("readOnlyHint") == set(load_public_tool_names()) - writers
    assert _names_where("readOnlyHint") >= EXPECTED_READ_ONLY_WITHOUT_PREFIX


def test_open_world_set_is_pinned():
    """openWorldHint 는 Bedrock 또는 원격 저장소 호출 여부와 정확히 같다."""
    assert _names_where("openWorldHint") == EXPECTED_OPEN_WORLD


def test_local_lpg_conversion_is_not_open_world():
    """tools.remote 모듈에 있어도 로컬 파일만 다루는 도구는 open world 가 아니다."""
    hints = _annotations_by_name()
    assert hints["convert_rdf_to_lpg"].openWorldHint is False
    assert hints["generate_lpg_semantic_dictionary"].openWorldHint is False


def test_hints_are_mutually_consistent():
    """read-only 도구는 destructive 가 아니고 idempotent 이며, 쓰기 도구는 idempotent 가 아니다."""
    for name, hints in _annotations_by_name().items():
        if hints.readOnlyHint:
            assert hints.destructiveHint is False, name
            assert hints.idempotentHint is True, name
        else:
            assert hints.idempotentHint is False, name


def test_unclassified_writer_defaults_to_destructive():
    """분류 집합에 없는 쓰기 도구는 non-destructive 로 게시되지 않는다."""
    from tools.registry import _tool_spec

    spec = _tool_spec("purge_everything", "tools.example:purge_everything")

    assert spec.read_only is False
    assert spec.destructive is True
    assert spec.open_world is False


def test_unknown_name_in_hint_set_blocks_registration(monkeypatch):
    """hint 집합의 오타는 기본 분류로 조용히 떨어지지 않고 등록을 막는다."""
    import tools.registry as registry

    patched = dict(registry._HINT_NAME_SETS)
    patched["_ADDITIVE_WRITE_TOOLS"] = frozenset({"neo4j_deploy_lgp"})
    monkeypatch.setattr(registry, "_HINT_NAME_SETS", patched)

    with pytest.raises(registry.ToolRegistrationError, match="neo4j_deploy_lgp"):
        registry._resolved_tools(registry.load_public_tool_names())
