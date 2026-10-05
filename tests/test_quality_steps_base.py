"""Phase 1 — quality_steps 패키지의 기반 dataclass 동작 검증."""
from __future__ import annotations

from rdflib import Graph

from tools.quality_steps import _POST_STEPS, _PRE_STEPS, StepContext, StepResult
from tools.quality_steps._base import StepFn


class TestStepContext:
    def test_default_fields_initialized(self):
        ctx = StepContext(domain_ns="http://example.org/x#")
        assert ctx.domain_ns == "http://example.org/x#"
        assert ctx.change_log == []
        assert ctx.shared == {}

    def test_change_log_is_independent_per_instance(self):
        # default_factory 가 mutable shared state 를 만들지 않는지 확인.
        a = StepContext(domain_ns="ns1")
        b = StepContext(domain_ns="ns2")
        a.change_log.append({"step": "x"})
        assert b.change_log == []

    def test_shared_dict_independent_per_instance(self):
        a = StepContext(domain_ns="ns1")
        b = StepContext(domain_ns="ns1")
        a.shared["pk_index"] = {"foo": 1}
        assert "pk_index" not in b.shared


class TestStepResult:
    def test_default_fields(self):
        r = StepResult(name="step_test")
        assert r.name == "step_test"
        assert r.stats == {}
        assert r.triples_delta == 0
        assert r.error is None

    def test_full_fields(self):
        r = StepResult(
            name="step_0d",
            stats={"removed": 3},
            triples_delta=-3,
            error=None,
        )
        assert r.stats["removed"] == 3
        assert r.triples_delta == -3


class TestPipelineStub:
    def test_pre_steps_list_exists(self):
        # Phase 2 시점 — _PRE_STEPS 는 아직 비어있음 (Phase 3 부터 채움).
        assert isinstance(_PRE_STEPS, list)

    def test_post_steps_populated(self):
        # Phase 2~ 진행에 따라 갯수는 증가. 최소 보장: Phase 2 의 10개 (22~29).
        assert len(_POST_STEPS) >= 10

    def test_step_fn_signature_compiles(self):
        # 컴파일 시점 import 검증 — StepFn alias 가 존재하는지.
        def dummy(g: Graph, ctx: StepContext) -> StepResult:
            return StepResult(name="dummy", triples_delta=0)

        fn: StepFn = dummy
        ctx = StepContext(domain_ns="ns")
        g = Graph()
        result = fn(g, ctx)
        assert result.name == "dummy"


# ───────────────────────────────────────────────────────────────────
# Phase 8 helpers (run_step_pipeline / run_step_pipeline_grouped)
# ───────────────────────────────────────────────────────────────────


def test_accumulate_redundant_pops_keys_and_sums_into_antipattern():
    from tools.quality_steps import _accumulate_redundant

    stats = {"antipattern_redundant_removed": 1}
    result_stats = {
        "_step12_redundant_delta": 3,
        "_step14_redundant_delta": 5,
        "other_key": 7,
    }

    _accumulate_redundant(stats, result_stats)

    assert "_step12_redundant_delta" not in result_stats
    assert "_step14_redundant_delta" not in result_stats
    assert result_stats == {"other_key": 7}
    assert stats["antipattern_redundant_removed"] == 1 + 3 + 5


def test_accumulate_redundant_initializes_when_key_missing():
    from tools.quality_steps import _accumulate_redundant

    stats: dict = {}
    result_stats = {"_step12_redundant_delta": 4}

    _accumulate_redundant(stats, result_stats)

    assert stats["antipattern_redundant_removed"] == 4


def test_run_step_pipeline_records_change_log_and_merges_stats():
    from rdflib import OWL, RDF, Graph, URIRef

    from tools.quality_steps import StepContext, StepResult, run_step_pipeline

    def add_class_step(g: Graph, ctx: StepContext) -> StepResult:
        before = len(g)
        g.add((URIRef("urn:Foo"), RDF.type, OWL.Class))
        return StepResult(
            name="add_class_step",
            stats={"_step12_redundant_delta": 2, "classes_added": 1},
            triples_delta=len(g) - before,
            step_number=99,
            step_label="add_class",
        )

    g = Graph()
    ctx = StepContext(domain_ns="urn:test#")
    stats: dict = {}
    change_log: list[dict] = []

    run_step_pipeline([add_class_step], g, ctx, stats, change_log)

    assert stats == {"antipattern_redundant_removed": 2, "classes_added": 1}
    assert len(change_log) == 1
    entry = change_log[0]
    assert entry["step"] == 99
    assert entry["name"] == "add_class"
    assert entry["triples_before"] == 0
    assert entry["triples_after"] == 1
    assert entry["delta"] == 1


def test_run_step_pipeline_uses_name_when_step_number_missing():
    from rdflib import Graph

    from tools.quality_steps import StepContext, StepResult, run_step_pipeline

    def noop_step(g, ctx):
        return StepResult(name="my_step")

    change_log: list[dict] = []
    run_step_pipeline(
        [noop_step], Graph(), StepContext(domain_ns="urn:t#"),
        stats={}, change_log=change_log,
    )

    assert change_log[0]["step"] == "my_step"
    assert change_log[0]["name"] == "my_step"


def test_run_step_pipeline_skips_failing_step_and_continues():
    from rdflib import Graph

    from tools.quality_steps import StepContext, StepResult, run_step_pipeline

    # ValueError 사용: RuntimeError 는 step 12c contract (의도적 abort) 신호로
    # 전파되므로, 우발 버그 swallow 경로 검증에는 다른 Exception 종류가 필요.
    def boom(g, ctx):
        raise ValueError("boom")

    def ok(g, ctx):
        return StepResult(name="ok", step_number=2, step_label="ok")

    change_log: list[dict] = []
    stats: dict = {}
    run_step_pipeline(
        [boom, ok], Graph(), StepContext(domain_ns="urn:t#"),
        stats=stats, change_log=change_log,
    )

    assert len(change_log) == 1
    assert change_log[0]["step"] == 2


def test_run_step_pipeline_grouped_emits_single_entry():
    from rdflib import OWL, RDF, Graph, URIRef

    from tools.quality_steps import (
        StepContext,
        StepResult,
        run_step_pipeline_grouped,
    )

    def add_one(name, n):
        def _step(g, ctx):
            for i in range(n):
                g.add((URIRef(f"urn:{name}{i}"), RDF.type, OWL.Class))
            return StepResult(
                name=name, step_number=name, step_label=name,
                stats={f"{name}_added": n}, triples_delta=n,
            )
        return _step

    g = Graph()
    ctx = StepContext(domain_ns="urn:t#")
    stats: dict = {}
    change_log: list[dict] = []

    run_step_pipeline_grouped(
        [add_one("a", 2), add_one("b", 3)],
        g, ctx, stats, change_log,
        group_step=9, group_label="group_label",
    )

    # Both substeps' stats merged.
    assert stats == {"a_added": 2, "b_added": 3}
    # Single change_log entry, with combined delta.
    assert len(change_log) == 1
    entry = change_log[0]
    assert entry["step"] == 9
    assert entry["name"] == "group_label"
    assert entry["triples_before"] == 0
    assert entry["triples_after"] == 5
    assert entry["delta"] == 5


def test_run_step_pipeline_grouped_skips_failing_step_and_emits_entry():
    """모든 substep 이 실패해도 group change_log entry 는 생성된다 (delta=0)."""
    from rdflib import Graph

    from tools.quality_steps import (
        StepContext,
        run_step_pipeline_grouped,
    )

    # ValueError 사용: RuntimeError 는 step 12c contract 로 전파되므로 swallow
    # 경로 검증용에는 다른 Exception 종류가 필요.
    def boom(g, ctx):
        raise ValueError("boom")

    g = Graph()
    ctx = StepContext(domain_ns="urn:t#")
    stats: dict = {}
    change_log: list[dict] = []

    run_step_pipeline_grouped(
        [boom, boom],
        g, ctx, stats, change_log,
        group_step=9, group_label="all_failed_group",
    )

    # 두 substep 모두 실패해도 group entry 1건은 항상 기록.
    assert len(change_log) == 1
    entry = change_log[0]
    assert entry["step"] == 9
    assert entry["name"] == "all_failed_group"
    assert entry["triples_before"] == 0
    assert entry["triples_after"] == 0
    assert entry["delta"] == 0
    # stats 변화 없음.
    assert stats == {}


def test_run_step_pipeline_propagates_runtime_error():
    """RuntimeError 는 step 12c contract 처럼 호출자로 전파되어야 한다 (skip 금지)."""
    import pytest as _pytest
    from rdflib import Graph

    from tools.quality_steps import StepContext, run_step_pipeline

    def coverage_fail(g, ctx):
        raise RuntimeError("simulated coverage gate fail")

    def should_not_run(g, ctx):
        raise AssertionError("should never reach this step")

    with _pytest.raises(RuntimeError, match="simulated coverage gate fail"):
        run_step_pipeline(
            [coverage_fail, should_not_run],
            Graph(), StepContext(domain_ns="urn:t#"),
            stats={}, change_log=[],
        )


def test_run_step_pipeline_grouped_propagates_runtime_error():
    """grouped helper 도 RuntimeError 는 전파한다 (group entry 기록 안 함)."""
    import pytest as _pytest
    from rdflib import Graph

    from tools.quality_steps import StepContext, run_step_pipeline_grouped

    def coverage_fail(g, ctx):
        raise RuntimeError("simulated grouped fail")

    change_log: list[dict] = []
    with _pytest.raises(RuntimeError, match="simulated grouped fail"):
        run_step_pipeline_grouped(
            [coverage_fail],
            Graph(), StepContext(domain_ns="urn:t#"),
            stats={}, change_log=change_log,
            group_step=9, group_label="aborted_group",
        )
    # RuntimeError abort 시 group entry 도 기록되지 않는다 (의도된 abort).
    assert change_log == []
