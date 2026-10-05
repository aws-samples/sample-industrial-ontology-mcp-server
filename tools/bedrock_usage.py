"""Bedrock 호출 토큰/비용 집계.

세션 스코프 누적기. multi_agent_tbox 협업 1회 동안 input/output/cache 토큰을 모으고,
USD 환산을 제공한다. quality_history.json에 기록할 때 쓴다.

가격표는 Anthropic Sonnet 4.5 기준 (USD per 1M tokens). 모델/리전이 달라지면 갱신 필요.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Anthropic Claude Sonnet 4.5 기준 (2025-10 기준, USD per 1M tokens).
# 실제 Bedrock 가격은 리전별로 다를 수 있으니 근사치로 사용.
_PRICE_PER_1M: dict[str, dict[str, float]] = {
    "sonnet-4-5": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,       # 10% of base input
        "cache_creation": 3.75,   # 125% of base input
    },
    "opus-4": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
}


@dataclass
class UsageAccumulator:
    """Bedrock 호출 한 세션의 토큰/비용 누적기."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    calls: int = 0
    model_key: str = "sonnet-4-5"
    per_call: list[dict] = field(default_factory=list)

    def record(self, usage: dict | None) -> None:
        """Bedrock 응답의 usage dict을 추가."""
        self.calls += 1
        if not usage:
            self.per_call.append({})
            return
        it = usage.get("input_tokens") or 0
        ot = usage.get("output_tokens") or 0
        cr = usage.get("cache_read_input_tokens") or 0
        cc = usage.get("cache_creation_input_tokens") or 0
        self.input_tokens += it
        self.output_tokens += ot
        self.cache_read_tokens += cr
        self.cache_creation_tokens += cc
        self.per_call.append({
            "input_tokens": it, "output_tokens": ot,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
        })

    def cache_hit_ratio(self) -> float:
        """cache_read_tokens / (cache_read + input). 0~1 비율."""
        denom = self.cache_read_tokens + self.input_tokens
        if denom == 0:
            return 0.0
        return round(self.cache_read_tokens / denom, 4)

    def cost_usd(self) -> float:
        price = _PRICE_PER_1M.get(self.model_key, _PRICE_PER_1M["sonnet-4-5"])
        total = (
            self.input_tokens * price["input"]
            + self.output_tokens * price["output"]
            + self.cache_read_tokens * price["cache_read"]
            + self.cache_creation_tokens * price["cache_creation"]
        ) / 1_000_000
        return round(total, 4)

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "model": self.model_key,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
            "cache_creation_input_tokens": self.cache_creation_tokens,
            "cache_hit_ratio": self.cache_hit_ratio(),
            "cost_usd_estimate": self.cost_usd(),
        }


__all__ = ("UsageAccumulator",)
