"""Tests for tools.validation_support.registry."""
from __future__ import annotations

import pytest

from tools.validation_support.registry import CheckRegistry, CheckResult


def test_register_and_iter_order_preserved():
    reg = CheckRegistry()
    reg.register("a", lambda: {"name": "a", "passed": True})
    reg.register("b", lambda: {"name": "b", "passed": False})
    keys = [s.key for s in reg]
    assert keys == ["a", "b"]
    assert len(reg) == 2


def test_duplicate_key_raises():
    reg = CheckRegistry()
    reg.register("dup", lambda: {"name": "dup", "passed": True})
    with pytest.raises(ValueError):
        reg.register("dup", lambda: {"name": "dup", "passed": False})


def test_is_heavy_flag_retained():
    reg = CheckRegistry()
    reg.register("heavy", lambda: {"name": "heavy", "passed": True}, is_heavy=True)
    spec = next(iter(reg))
    assert spec.is_heavy is True


def test_diagnosis_hint_retained():
    reg = CheckRegistry()
    reg.register("x", lambda: {"name": "x", "passed": True}, diagnosis_hint="schema_gap")
    spec = next(iter(reg))
    assert spec.diagnosis_hint == "schema_gap"


def test_decorator_form_registers_function():
    reg = CheckRegistry()

    @reg.check("dec", is_heavy=True, diagnosis_hint="data_quality")
    def my_check():
        return {"name": "dec", "passed": True}

    assert len(reg) == 1
    spec = next(iter(reg))
    assert spec.key == "dec"
    assert spec.is_heavy is True
    assert spec.diagnosis_hint == "data_quality"
    # 데코레이터는 원 함수를 그대로 돌려준다
    assert my_check() == {"name": "dec", "passed": True}


def test_runner_invoked_returns_dict():
    reg = CheckRegistry()
    reg.register("run", lambda: {"name": "run", "passed": True, "violations": []})
    result = next(iter(reg)).runner()
    assert result["name"] == "run"
    assert result["passed"] is True


def test_check_result_from_dict_and_back():
    d = {"name": "r", "passed": True, "violations": [1, 2]}
    cr = CheckResult.from_dict(d)
    assert cr.name == "r"
    assert cr.passed is True
    assert cr.details == {"violations": [1, 2]}
    back = cr.to_dict()
    assert back == d


def test_check_result_defaults_when_dict_incomplete():
    cr = CheckResult.from_dict({})
    assert cr.name == "<unnamed>"
    assert cr.passed is False
    assert cr.details == {}
