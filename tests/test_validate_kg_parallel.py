"""VALIDATE_KG_PARALLEL=true 병렬 실행 테스트."""
from __future__ import annotations

from tools.kg_validation import _run_checks
from tools.validation_support.registry import CheckRegistry


def _make_reg():
    reg = CheckRegistry()
    reg.register("heavy_a", lambda: {"name": "heavy_a", "passed": True}, is_heavy=True)
    reg.register("light_b", lambda: {"name": "light_b", "passed": True})
    reg.register("heavy_c", lambda: {"name": "heavy_c", "passed": False}, is_heavy=True)
    reg.register("light_d", lambda: {"name": "light_d", "passed": True})
    return reg


def test_sequential_default_preserves_order(monkeypatch):
    monkeypatch.delenv("VALIDATE_KG_PARALLEL", raising=False)
    out = _run_checks(_make_reg())
    assert [c["name"] for c in out] == ["heavy_a", "light_b", "heavy_c", "light_d"]


def test_parallel_mode_preserves_registered_order(monkeypatch):
    monkeypatch.setenv("VALIDATE_KG_PARALLEL", "true")
    monkeypatch.setenv("VALIDATE_KG_MAX_WORKERS", "2")
    out = _run_checks(_make_reg())
    # 등록 순서 기준 복원 — heavy가 먼저 계산되더라도 최종 배열 순서는 동일.
    assert [c["name"] for c in out] == ["heavy_a", "light_b", "heavy_c", "light_d"]


def test_parallel_mode_returns_all_results(monkeypatch):
    monkeypatch.setenv("VALIDATE_KG_PARALLEL", "1")
    out = _run_checks(_make_reg())
    passed = {c["name"]: c["passed"] for c in out}
    assert passed == {"heavy_a": True, "light_b": True, "heavy_c": False, "light_d": True}
