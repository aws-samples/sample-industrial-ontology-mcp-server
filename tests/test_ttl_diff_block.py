"""S2 _format_ttl_diff_block 테스트."""
from __future__ import annotations

from tools.multi_agent_tbox import _format_ttl_diff_block


def test_unchanged_returns_no_change_marker():
    same = "@prefix : <http://x/> .\n:A a :B .\n"
    out = _format_ttl_diff_block(same, same)
    assert "변경 없음" in out


def test_small_change_returns_diff_block():
    prev = "@prefix : <http://x/> .\n:A a :B .\n:C a :D .\n"
    curr = "@prefix : <http://x/> .\n:A a :B .\n:C a :D .\n:E a :F .\n:G a :H .\n"
    out = _format_ttl_diff_block(prev, curr)
    assert "```diff" in out
    assert ":E a :F" in out


def test_large_change_falls_back_to_full_ttl():
    prev = "@prefix : <http://x/> .\n"
    # 30k chars new TTL — diff size will exceed max_diff_chars default 20k
    curr = prev + "\n".join(f":C{i} a :Thing ." for i in range(800))
    out = _format_ttl_diff_block(prev, curr, max_diff_chars=5000)
    # large diff → full TTL fallback
    assert "```turtle" in out


def test_empty_vs_empty_returns_no_change():
    out = _format_ttl_diff_block("", "")
    assert "변경 없음" in out
