"""공개 워크샵 흐름의 최소 실행 계약."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from app import build_server

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSHOP_TOOLS = frozenset(
    {
        "check_pipeline_state",
        "generate_competency_questions",
        "list_csv_tables",
        "read_csv_schema",
        "generate_csv_erd",
        "profile_csv_data",
        "generate_tbox_collaborative",
        "read_tbox",
        "analyze_tbox",
        "improve_tbox_quality",
        "measure_tbox_metrics",
        "update_tbox_incremental",
        "validate_ttl_syntax",
        "check_quality_rules",
        "validate_owl_consistency",
        "classify_tbox",
        "validate_tbox_shacl",
        "add_tacit_from_natural_language",
        "generate_tacit_from_rules",
        "generate_tacit_from_data",
        "skip_tacit_knowledge",
        "visualize_tbox",
        "generate_abox",
        "validate_kg",
        "sparql_local",
        "test_domain_queries",
        "reset_pipeline_state",
    }
)


def test_server_exposes_workshop_workflow_tools():
    """워크샵이 안내하는 27개 도구가 공개 server에 모두 있어야 한다."""
    names = {
        tool.name
        for tool in asyncio.run(build_server().list_tools())
    }

    assert names >= WORKSHOP_TOOLS


@pytest.mark.slow
def test_workshop_examples_run_against_pregenerated_graph():
    """체크인된 그래프에서 워크샵 코드 블록이 실제로 실행돼야 한다."""
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verify_workshop_sparql.py"),
            "--ignore-placeholders",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.requires_bedrock
def test_configured_bedrock_model_responds():
    """명시 설정된 Bedrock 모델이 실제 요청에 비어 있지 않은 답을 반환한다."""
    from tools.bedrock import invoke_bedrock_text

    response = invoke_bedrock_text(
        "Reply with the token OA_BEDROCK_OK.",
        max_tokens=32,
        max_retries=0,
        temperature=0,
    )

    assert response.strip()
