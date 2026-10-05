"""공개 MCP 도구 registry의 불변식 테스트."""

from __future__ import annotations

import asyncio
import re

import pytest


def _build_server():
    try:
        from app import build_server
    except ImportError as exc:
        pytest.fail(f"명시적 server builder가 없습니다: {exc}")
    return build_server()


def _scope():
    from tools.registry import load_public_tool_names

    return load_public_tool_names()


def test_registered_tools_match_manifest():
    """등록된 이름 집합은 manifest의 공개 도구 집합과 정확히 같아야 한다."""
    server = _build_server()
    actual = {tool.name for tool in asyncio.run(server.list_tools())}

    assert actual == set(_scope())
    assert len(actual) == 129


def test_registry_targets_all_resolve():
    """명시적 registry의 모든 module:function 타깃은 해석돼야 한다."""
    try:
        from tools.registry import unresolvable_targets
    except ImportError as exc:
        pytest.fail(f"명시적 tool registry가 없습니다: {exc}")

    assert unresolvable_targets() == []


@pytest.mark.parametrize(
    "content",
    [
        'schema_version = 2\n[mcp]\ntools = ["tool_one"]\n',
        (
            'schema_version = 1\nunexpected = true\n'
            '[mcp]\ntools = ["tool_one"]\n'
        ),
    ],
)
def test_tool_manifest_rejects_invalid_root_contract(tmp_path, content):
    """runtime registry도 공개 tool manifest의 root schema를 fail-closed로 검증한다."""
    from tools.registry import ToolRegistrationError, load_public_tool_names

    manifest = tmp_path / "mcp-tools.toml"
    manifest.write_text(content, encoding="utf-8")

    with pytest.raises(ToolRegistrationError):
        load_public_tool_names(manifest)


def test_each_public_tool_has_a_collected_contract_test(
    pytest_collected_node_ids,
):
    """manifest와 독립적으로 수집된 계약 테스트가 공개 도구마다 있어야 한다."""
    missing = [
        name
        for name in _scope()
        if (
            "tests/public/test_tool_contracts.py::"
            f"test_{name}_contract"
        )
        not in pytest_collected_node_ids
    ]

    assert missing == []


def test_every_public_tool_has_annotations():
    """공개 도구마다 MCP의 네 가지 boolean hint가 명시돼야 한다."""
    tools = asyncio.run(_build_server().list_tools())
    missing = [tool.name for tool in tools if tool.annotations is None]
    incomplete = [
        tool.name
        for tool in tools
        if tool.annotations is not None
        and any(
            getattr(tool.annotations, field) is None
            for field in (
                "readOnlyHint",
                "destructiveHint",
                "idempotentHint",
                "openWorldHint",
            )
        )
    ]
    # destructiveHint 의 도구별 기대값은 tests/test_registry_tool_annotations.py 가 고정한다.
    assert missing == []
    assert incomplete == []


def test_public_tool_descriptions_are_english_first():
    """모델에 게시되는 설명은 ASCII 영문 문장으로 시작해야 한다."""
    tools = asyncio.run(_build_server().list_tools())
    non_english_first = [
        tool.name
        for tool in tools
        if not re.match(r"^[A-Za-z]", (tool.description or "").lstrip())
    ]

    assert non_english_first == []
