"""공개 도구의 경로형 파라미터가 경계 테스트에 빠짐없이 올라 있는지 고정한다.

``tools/registry.py`` 가 등록하는 공개 도구 시그니처에서 이름이 경로처럼 보이는
파라미터를 모두 찾는다. 각 파라미터는 ``tests/test_input_path_boundaries.py`` 의 경계
표 행이거나, 아래 두 목록 중 하나에 근거와 함께 올라 있어야 한다.

- ``_COVERED_ELSEWHERE``: 쓰기 대상이나 원격 호출처럼 공통 공격 하네스로 구동할 수
  없어 다른 테스트가 경계를 고정하는 파라미터. 값은 그 테스트의 node id 이며, 그
  테스트가 실제로 있는지 확인한다.
- ``_NOT_A_PATH``: 이름은 경로처럼 보이지만 파일 시스템 경로를 만들지 않는 파라미터.

새 공개 도구가 경로를 받으면 이 테스트가 먼저 실패하므로, 경계 가드와 표 행을
함께 추가하게 된다.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tests.test_input_path_boundaries import _ALL_TARGETS

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 파라미터 이름을 ``_`` 로 나눈 조각 중 하나라도 여기 있으면 경로형으로 본다.
#: 실제 시그니처의 경로·디렉터리·파일 이름·CSV·사이드카·파일 이름이 되는 키를 덮는다.
_PATH_TOKENS = frozenset({
    "path", "dir", "file", "filename", "csv", "sidecar",
    "timestamp", "key", "table", "tables",
})

#: (공개 도구 이름, 파라미터) → 경계를 고정하는 테스트 node id.
_COVERED_ELSEWHERE = {
    ("annotate_lpg_graph_context", "nodes_csv"):
        "tests/test_tool_path_containment_family.py::"
        "test_annotate_lpg_graph_context_rejects_outside_csv_without_partial_write",
    ("annotate_lpg_graph_context", "relationships_csv"):
        "tests/test_tool_path_containment_family.py::"
        "test_annotate_lpg_graph_context_rejects_outside_csv_without_partial_write",
    ("export_quality_dqv", "output_path"):
        "tests/test_tool_path_containment_family.py::"
        "test_export_quality_dqv_rejects_outside_target_before_measuring",
    ("graphdb_export_inferred", "output_path"):
        "tests/test_tool_path_containment_family.py::"
        "test_graphdb_export_inferred_rejects_outside_target_before_request",
    ("graphdb_run_inference", "output_path"):
        "tests/test_tool_path_containment_family.py::"
        "test_graphdb_run_inference_rejects_outside_output_before_any_stage",
    ("graphdb_run_inference", "tbox_path"):
        "tests/test_remote_store_defaults.py::"
        "test_public_entry_rejects_input_path_without_starting_a_job",
    ("graphdb_run_inference", "abox_path"):
        "tests/test_remote_store_defaults.py::"
        "test_public_entry_rejects_input_path_without_starting_a_job",
    ("graphdb_import_file", "file_path"):
        "tests/test_graphdb_import_file_validation.py::"
        "test_graphdb_import_file_rejects_symlink_to_outside",
    ("profile_csv_data", "table_name"):
        "tests/test_local_artifacts.py::TestProfileCsvData::"
        "test_rejects_paths_outside_rawdata_directory",
    ("validate_owl_realisation", "ttl_path"):
        "tests/test_ttl_path_containment.py::"
        "test_ttl_path_outside_tbox_dir_is_rejected_before_load",
    ("run_entailment_regression", "inferred_path"):
        "tests/test_sparql_callsite_inputs.py::"
        "test_entailment_rejects_inferred_path_outside_generated",
    ("run_entailment_regression", "golden_path"):
        "tests/test_sparql_callsite_inputs.py::"
        "test_entailment_rejects_golden_path_outside_rules",
}

_TABLE_FILTER = (
    "쉼표로 구분한 테이블 이름 필터다. 로드한 CSV 스키마의 테이블 이름과 대조만 하고 "
    "파일 경로를 만들지 않는다."
)

#: (공개 도구 이름, 파라미터) → 경로가 아닌 이유.
_NOT_A_PATH = {
    ("generate_tbox", "save_to_tbox_path"):
        "bool 플래그다. 기본 TBOX_PATH 에 저장할지만 정한다.",
    ("generate_tbox", "tables"): _TABLE_FILTER,
    ("generate_tbox_collaborative", "tables"): _TABLE_FILTER,
    ("generate_abox", "tables"): _TABLE_FILTER,
    ("update_tbox_incremental", "changed_tables"):
        "변경 테이블 이름 목록이다. T-Box 클래스 이름 대조에만 쓰고 파일 경로를 만들지 않는다.",
}


def _looks_like_path(parameter: str) -> bool:
    return any(token in _PATH_TOKENS for token in parameter.split("_"))


def _public_tool_parameters() -> list[tuple[str, str, str, str]]:
    """(공개 도구 이름, 모듈, 함수, 파라미터) 를 registry 정본 순서로 돌려준다."""
    from tools import registry

    rows = []
    for spec, function in registry._resolved_tools(registry.load_public_tool_names()):
        module_name, function_name = spec.target.split(":")
        for parameter in inspect.signature(function).parameters:
            rows.append((spec.name, module_name, function_name, parameter))
    return rows


def _boundary_table_keys() -> set[tuple[str, str, str]]:
    return {(module, function, parameter) for module, function, parameter, *_ in _ALL_TARGETS}


def _uncovered(
    parameters: list[tuple[str, str, str, str]],
    table_keys: set[tuple[str, str, str]],
) -> list[str]:
    missing = []
    for tool, module, function, parameter in parameters:
        if not _looks_like_path(parameter):
            continue
        if (module, function, parameter) in table_keys:
            continue
        if (tool, parameter) in _COVERED_ELSEWHERE or (tool, parameter) in _NOT_A_PATH:
            continue
        missing.append(f"{tool}({parameter}) [{module}:{function}]")
    return missing


@pytest.fixture(scope="module")
def public_parameters() -> list[tuple[str, str, str, str]]:
    return _public_tool_parameters()


def test_every_path_like_parameter_is_covered(public_parameters):
    """경로형 파라미터는 경계 표나 사유 있는 목록 중 하나에 반드시 올라 있다."""
    missing = _uncovered(public_parameters, _boundary_table_keys())

    assert not missing, (
        "경계 테스트가 없는 경로형 파라미터가 있다. 경계 가드를 넣고 "
        "tests/test_input_path_boundaries.py 의 표에 행을 추가하라: " + ", ".join(missing)
    )


def test_removing_a_table_row_is_detected(public_parameters):
    """표 행 하나를 지우면 위 검사가 그 파라미터를 미커버로 보고한다."""
    table_keys = _boundary_table_keys()
    removed = ("tools.cwa_owa_tagging", "summarize_origin_sidecar", "sidecar_path")
    assert removed in table_keys

    missing = _uncovered(public_parameters, table_keys - {removed})

    assert missing == [
        "summarize_origin_sidecar(sidecar_path) "
        "[tools.cwa_owa_tagging:summarize_origin_sidecar]"
    ]


def test_table_rows_point_at_real_public_parameters(public_parameters):
    """표 행은 실제 공개 도구의 실제 파라미터를 가리킨다 (오타 행은 아무것도 지키지 않는다)."""
    real = {(module, function, parameter) for _, module, function, parameter in public_parameters}

    stale = sorted(_boundary_table_keys() - real)

    assert not stale, f"공개 도구 시그니처에 없는 표 행: {stale}"


def test_allowlists_name_real_parameters_and_do_not_overlap(public_parameters):
    """사유 목록은 실제 경로형 파라미터만 담고, 표 행과 겹치지 않는다."""
    real = {(tool, parameter) for tool, _, _, parameter in public_parameters}
    tabled = {
        (tool, parameter)
        for tool, module, function, parameter in public_parameters
        if (module, function, parameter) in _boundary_table_keys()
    }

    for listing in (_COVERED_ELSEWHERE, _NOT_A_PATH):
        assert set(listing) <= real, sorted(set(listing) - real)
        assert all(_looks_like_path(parameter) for _, parameter in listing)
        assert not set(listing) & tabled, sorted(set(listing) & tabled)
    assert not set(_COVERED_ELSEWHERE) & set(_NOT_A_PATH)
    assert all(reason.strip() for reason in _NOT_A_PATH.values())


@pytest.mark.parametrize("node_id", sorted(set(_COVERED_ELSEWHERE.values())))
def test_covering_tests_exist(node_id):
    """``_COVERED_ELSEWHERE`` 가 가리키는 테스트가 실제로 있다."""
    file_part, *names = node_id.split("::")
    source = (_REPO_ROOT / file_part).read_text(encoding="utf-8")
    *classes, function = names

    for class_name in classes:
        assert f"class {class_name}" in source, node_id
    assert f"def {function}(" in source, node_id
