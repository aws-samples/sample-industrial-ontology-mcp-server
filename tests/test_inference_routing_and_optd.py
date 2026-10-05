"""로컬 추론 한계와 OPT-D 헬퍼의 회귀 테스트.

다음 동작이 조용히 회귀하지 않도록 고정한다.

1. 대용량 입력은 GraphDB에 자동 위임하지 않고 로컬 축소 안내로 실패한다.

2. ``graphdb_export_inferred``는 ``format``에 맞는 ``Accept`` 헤더를 보낸다.

3. ``_prune_inference_noise``는 도메인 IRI를 하드코딩하지 않는다.

4. ``stream_closure_to_nt_file``은 유효한 N-Triples와 chunk 경계를 보존한다.

5. ``_rss_mb``는 macOS와 Linux에서 같은 MB 단위를 반환한다.

6. 명시적 registry에 underscore 접두 private 이름이 노출되지 않는다.
"""
from __future__ import annotations

from unittest import mock

import pytest
from rdflib import URIRef

# ── #1: 대용량 입력은 로컬 경계를 유지 ─────────────────────────────


def test_oversized_inference_input_returns_local_reduction_error():
    """로컬 안전 한계를 넘은 입력은 GraphDB 위임 없이 축소 안내를 반환한다."""
    from tools import inference as inf
    from tools.remote import graphdb as gdb_mod

    with (
        mock.patch.object(
            inf,
            "_inference_inputs_newer_than_output",
            return_value=True,
        ),
        mock.patch.object(inf, "_estimate_input_triples", return_value=12_000_001),
        mock.patch.object(
            gdb_mod,
            "_graphdb_run_inference_sync",
            return_value='{"success": true, "engine": "graphdb"}',
        ),
    ):
        result = inf._run_owl_rl_inference_sync(force=True)

    payload = __import__("json").loads(result)
    assert payload["success"] is False
    assert "입력" in payload["error"]
    assert "축소" in payload["hint"]


# ── #2: graphdb_export_inferred format → Accept header ──────────────


@pytest.mark.parametrize("fmt,expected_accept", [
    ("turtle", "text/turtle"),
    ("ttl", "text/turtle"),
    ("nt", "application/n-triples"),
    ("n-triples", "application/n-triples"),
])
def test_graphdb_export_inferred_format_maps_to_accept_header(
    tmp_path, monkeypatch, fmt, expected_accept,
):
    import config
    from tools.remote import graphdb as gdb_mod

    # export 대상은 INFERRED_PATH 와 같은 디렉터리 안이어야 한다.
    monkeypatch.setattr(config, "INFERRED_PATH", str(tmp_path / "all_inferred.ttl"))

    captured: dict[str, str] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"# empty\n"

    def _fake_get(url, params=None, headers=None, stream=None, timeout=None):
        captured["accept"] = headers["Accept"]
        return _FakeResp()

    out_path = str(tmp_path / "out.ttl")
    with mock.patch.object(gdb_mod.requests, "get", side_effect=_fake_get):
        result = gdb_mod.graphdb_export_inferred(
            output_path=out_path, format=fmt,
        )
    assert "export 완료" in result
    assert captured["accept"] == expected_accept


def test_graphdb_export_inferred_rejects_unknown_format(tmp_path):
    from tools.remote import graphdb as gdb_mod

    result = gdb_mod.graphdb_export_inferred(
        output_path=str(tmp_path / "out.ttl"),
        format="rdfxml",
    )
    assert "지원하지 않는 format" in result


# ── #3: _prune_inference_noise no longer hardcodes the steel namespace ─


def test_prune_inference_noise_uses_domain_ns_not_hardcoded_iri():
    """Patterns 6a/6b must derive their FILTER from DOMAIN_NS so the prune
    step works regardless of which domain the project is configured for.

    The check looks for *any* absolute http(s) IRI rather than naming the
    deployments it is protecting against. Naming them (as this test used to)
    puts customer identities into a tracked file, and the list silently goes
    stale for the next deployment — a hardcoded IRI for a domain nobody thought
    to enumerate would pass.
    """
    import inspect
    import re

    from tools import inference as inf

    src = inspect.getsource(inf._prune_inference_noise)
    # Strip docstring/comments: prose may legitimately mention an example IRI.
    code = re.sub(r"#[^\n]*", "", re.sub(r'"""(?:.|\n)*?"""', "", src))
    hardcoded = [
        iri for iri in re.findall(r'["\']https?://[^"\'\s]+', code)
        # Standard vocabularies are not domain-specific.
        if not re.search(r"w3\.org|purl\.org|xmlns\.com|schema\.org", iri)
    ]
    assert not hardcoded, (
        f"Pattern 6a/6b SPARQL hardcodes a domain IRI {hardcoded}; it must use "
        "DOMAIN_NS so every deployment benefits from the post-RL cleanup."
    )
    # Sanity: the function must still reference the namespace constant.
    assert "domain_ns" in src or "DOMAIN_NS" in src


# ── #4: stream_closure_to_nt_file output is valid NT ────────────────


def test_stream_closure_to_nt_file_writes_valid_ntriples(tmp_path, monkeypatch):
    from rdflib import Literal

    from tools.inference_optd import stream_closure_to_nt_file

    triples = [
        (URIRef("http://example.org/a"), URIRef("http://example.org/p"),
         URIRef("http://example.org/b")),
        (URIRef("http://example.org/a"), URIRef("http://example.org/p"),
         Literal("hello")),
        (URIRef("http://example.org/a"), URIRef("http://example.org/p"),
         Literal(42)),
    ]
    out_path = str(tmp_path / "closure.nt")
    # Force chunk boundary in the middle of the input
    monkeypatch.setenv("OWL_CLOSURE_CHUNK", "2")
    written = stream_closure_to_nt_file(list(triples), out_path)
    assert written == 3

    # Round-trip parse to confirm validity
    from rdflib import Graph
    g = Graph()
    g.parse(out_path, format="nt")
    assert len(g) == 3


# ── #5: _rss_mb scales correctly on Linux vs macOS ──────────────────


def test_rss_mb_linux_kb_path():
    from tools import inference as inf

    fake_usage = mock.MagicMock(ru_maxrss=2_000_000)  # 2 GB on Linux (KB)
    with mock.patch.object(inf.resource, "getrusage", return_value=fake_usage), \
         mock.patch.object(inf.sys, "platform", "linux"):
        mb = inf._rss_mb()
    # Linux: 2_000_000 KB / 1024 ≈ 1953 MB
    assert 1900 < mb < 2000


def test_rss_mb_macos_bytes_path():
    from tools import inference as inf

    fake_usage = mock.MagicMock(ru_maxrss=2_000_000_000)  # 2 GB on macOS (bytes)
    with mock.patch.object(inf.resource, "getrusage", return_value=fake_usage), \
         mock.patch.object(inf.sys, "platform", "darwin"):
        mb = inf._rss_mb()
    # macOS: 2_000_000_000 / 1024 / 1024 ≈ 1907 MB
    assert 1900 < mb < 2000


# ── #6: no underscore-prefixed names leaked as MCP tools ────────────


def test_no_underscore_prefixed_mcp_tools_registered():
    """명시적 registry가 private helper를 공개하지 않는지 고정한다."""
    import asyncio

    from app import build_server

    tools = asyncio.run(build_server().list_tools())
    leaked = [t.name for t in tools if t.name.startswith("_")]
    assert leaked == [], f"private names leaked as MCP tools: {leaked}"
