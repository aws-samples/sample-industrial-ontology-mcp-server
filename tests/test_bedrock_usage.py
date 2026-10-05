"""UsageAccumulator 테스트."""
from __future__ import annotations

from tools.bedrock_usage import UsageAccumulator


def test_record_accumulates():
    acc = UsageAccumulator()
    acc.record({"input_tokens": 100, "output_tokens": 50})
    acc.record({"input_tokens": 200, "output_tokens": 80,
                 "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 300})
    assert acc.calls == 2
    assert acc.input_tokens == 300
    assert acc.output_tokens == 130
    assert acc.cache_read_tokens == 1000
    assert acc.cache_creation_tokens == 300


def test_cache_hit_ratio():
    acc = UsageAccumulator()
    acc.record({"input_tokens": 200, "cache_read_input_tokens": 800})
    assert acc.cache_hit_ratio() == 0.8


def test_cost_calculation_sonnet():
    acc = UsageAccumulator(model_key="sonnet-4-5")
    acc.record({"input_tokens": 1_000_000, "output_tokens": 500_000})
    # input 1M × $3 = $3, output 0.5M × $15 = $7.5 → $10.5
    assert abs(acc.cost_usd() - 10.5) < 0.01


def test_empty_usage_still_counts_call():
    acc = UsageAccumulator()
    acc.record(None)
    assert acc.calls == 1
    assert acc.input_tokens == 0


def test_to_dict_shape():
    acc = UsageAccumulator()
    acc.record({"input_tokens": 10, "output_tokens": 5})
    d = acc.to_dict()
    for k in ("calls", "model", "input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens",
              "cache_hit_ratio", "cost_usd_estimate"):
        assert k in d
