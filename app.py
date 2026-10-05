"""Ontology Agent FastMCP 서버를 구성한다."""

from mcp.server.fastmcp import FastMCP

from tools.registry import register_public_tools


def build_server() -> FastMCP:
    """manifest 계약에 맞는 공개 도구를 등록한 새 서버를 반환한다."""
    server = FastMCP("ontology-agent")
    register_public_tools(server)
    return server
