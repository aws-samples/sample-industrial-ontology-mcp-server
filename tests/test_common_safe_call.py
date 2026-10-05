"""Tests for tools/common.py safe_call."""
from __future__ import annotations

import logging


def test_safe_call_returns_value():
    from tools.common import safe_call
    assert safe_call(lambda x: x * 2, 5) == 10


def test_safe_call_fallback_on_exception():
    from tools.common import safe_call

    def boom():
        raise ValueError("boom")

    assert safe_call(boom, fallback="safe") == "safe"


def test_safe_call_logs_at_level(caplog):
    from tools.common import safe_call

    def boom():
        raise RuntimeError("x")

    lg = logging.getLogger("test_safe_call")
    with caplog.at_level(logging.WARNING, logger="test_safe_call"):
        safe_call(boom, fallback=None, logger=lg, context="while testing")
    assert any("RuntimeError" in r.message for r in caplog.records)
    assert any("while testing" in r.message for r in caplog.records)


def test_safe_call_default_fallback_none():
    from tools.common import safe_call
    assert safe_call(lambda: 1 / 0) is None


def test_safe_call_kwargs_propagate():
    from tools.common import safe_call

    def add(a, b=0):
        return a + b

    assert safe_call(add, 3, b=4) == 7
