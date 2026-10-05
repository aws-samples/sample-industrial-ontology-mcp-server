"""Tests for tools/prompt_audit.py — Q5."""
from __future__ import annotations

import json
from unittest.mock import patch


def test_key_deterministic():
    from tools.prompt_audit import _key_for
    k1 = _key_for("model-a", 0.0, "hello")
    k2 = _key_for("model-a", 0.0, "hello")
    assert k1 == k2


def test_key_varies_by_model_and_temp():
    from tools.prompt_audit import _key_for
    k_a = _key_for("model-a", 0.0, "hi")
    k_b = _key_for("model-b", 0.0, "hi")
    k_t = _key_for("model-a", 0.5, "hi")
    assert len({k_a, k_b, k_t}) == 3


def test_cache_miss_then_hit(tmp_path):
    from tools.prompt_audit import cached_invoke
    calls = {"n": 0}

    def fake(prompt, max_tokens, temperature):
        calls["n"] += 1
        return {"text": "response-" + str(calls["n"]), "stop_reason": "end_turn"}

    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        r1 = cached_invoke("prompt", "m1", temperature=0, invoker=fake)
        r2 = cached_invoke("prompt", "m1", temperature=0, invoker=fake)

    assert r1["cache_hit"] is False
    assert r2["cache_hit"] is True
    assert calls["n"] == 1
    assert r1["text"] == r2["text"]


def test_force_refresh_bypasses_cache(tmp_path):
    from tools.prompt_audit import cached_invoke
    calls = {"n": 0}

    def fake(prompt, max_tokens, temperature):
        calls["n"] += 1
        return {"text": f"v{calls['n']}", "stop_reason": "end_turn"}

    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        cached_invoke("p", "m", temperature=0, invoker=fake)
        r = cached_invoke("p", "m", temperature=0, invoker=fake, force_refresh=True)

    assert r["cache_hit"] is False
    assert r["text"] == "v2"
    assert calls["n"] == 2


def test_cache_varies_by_temperature(tmp_path):
    from tools.prompt_audit import cached_invoke
    calls = {"n": 0}

    def fake(prompt, max_tokens, temperature):
        calls["n"] += 1
        return {"text": f"v{calls['n']}", "stop_reason": "end_turn"}

    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        cached_invoke("p", "m", temperature=0.0, invoker=fake)
        cached_invoke("p", "m", temperature=0.7, invoker=fake)

    assert calls["n"] == 2


def test_list_cache_entries(tmp_path):
    from tools.prompt_audit import cached_invoke, list_cache_entries

    def fake(prompt, max_tokens, temperature):
        return {"text": "x", "stop_reason": "end_turn"}

    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        cached_invoke("p1", "model-a", temperature=0, invoker=fake)
        cached_invoke("p2", "model-b", temperature=0.5, invoker=fake)
        entries = list_cache_entries()

    assert len(entries) == 2
    models = {e["model_id"] for e in entries}
    assert models == {"model-a", "model-b"}


def test_verify_reproducibility_returns_meta(tmp_path):
    from tools.prompt_audit import cached_invoke, verify_reproducibility

    def fake(prompt, max_tokens, temperature):
        return {"text": "hi there", "stop_reason": "end_turn"}

    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        r = cached_invoke("p", "m", temperature=0, invoker=fake)
        v = verify_reproducibility(r["cache_key"])

    assert v["success"] is True
    assert v["cached_response_summary"]["text_len"] == len("hi there")


def test_verify_reproducibility_miss(tmp_path):
    from tools.prompt_audit import verify_reproducibility
    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        v = verify_reproducibility("nonexistent_key")
    assert v["success"] is False


def test_list_prompt_cache_tool(tmp_path):
    from tools.prompt_audit import cached_invoke, list_prompt_cache

    def fake(prompt, max_tokens, temperature):
        return {"text": "x", "stop_reason": "end_turn"}

    with patch("tools.prompt_audit.CACHE_DIR", str(tmp_path)):
        cached_invoke("p", "m", temperature=0, invoker=fake)
        raw = list_prompt_cache(limit=10)
    data = json.loads(raw)
    assert data["success"] is True
    assert data["total"] == 1
