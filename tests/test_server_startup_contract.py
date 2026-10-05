"""MCP 서버 등록이 부분 성공 없이 fail-closed인지 검증한다."""

from __future__ import annotations

import asyncio
import importlib

import pytest
from mcp.server.fastmcp import FastMCP


def _registry_module():
    try:
        return importlib.import_module("tools.registry")
    except ImportError as exc:
        pytest.fail(f"명시적 tool registry가 없습니다: {exc}")


def test_import_failure_aborts_before_any_tool_is_registered(monkeypatch):
    """타깃 하나의 import 실패가 도구 하나의 조용한 소실로 끝나면 안 된다."""
    registry = _registry_module()
    server = FastMCP("registration-failure-test")
    original_import = registry.importlib.import_module

    def fail_one_import(module_name: str):
        if module_name == "tools.report":
            raise ImportError("injected report import failure")
        return original_import(module_name)

    monkeypatch.setattr(registry.importlib, "import_module", fail_one_import)

    with pytest.raises(registry.ToolRegistrationError, match="tools.report"):
        registry.register_public_tools(server)

    assert asyncio.run(server.list_tools()) == []


def test_missing_registry_target_aborts_registration(monkeypatch):
    """manifest 도구의 명시적 target이 사라지면 기동을 중단해야 한다."""
    registry = _registry_module()
    server = FastMCP("registration-missing-target-test")
    monkeypatch.setattr(
        registry,
        "PUBLIC_TOOL_TARGETS",
        tuple(
            target
            for target in registry.PUBLIC_TOOL_TARGETS
            if target != "tools.report:generate_pipeline_report"
        ),
    )

    with pytest.raises(
        registry.ToolRegistrationError,
        match="generate_pipeline_report",
    ):
        registry.register_public_tools(server)

    assert asyncio.run(server.list_tools()) == []


def test_unlisted_registry_target_aborts_registration(monkeypatch):
    """manifest에 없는 target이 추가되면 기동을 중단해야 한다."""
    registry = _registry_module()
    server = FastMCP("registration-unlisted-target-test")
    monkeypatch.setattr(
        registry,
        "PUBLIC_TOOL_TARGETS",
        (*registry.PUBLIC_TOOL_TARGETS, "tools.registry:load_public_tool_names"),
    )

    with pytest.raises(
        registry.ToolRegistrationError,
        match="load_public_tool_names",
    ):
        registry.register_public_tools(server)

    assert asyncio.run(server.list_tools()) == []


def test_manifest_mismatch_aborts_registration(monkeypatch):
    """타깃과 manifest 이름 집합이 다르면 등록을 시작하지 않아야 한다."""
    registry = _registry_module()
    tool_names = registry.load_public_tool_names()
    server = FastMCP("registration-mismatch-test")
    monkeypatch.setattr(
        registry,
        "load_public_tool_names",
        lambda: tool_names[:-1],
    )

    with pytest.raises(registry.ToolRegistrationError, match="manifest"):
        registry.register_public_tools(server)

    assert asyncio.run(server.list_tools()) == []
