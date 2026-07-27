# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Token usage tracking and cost estimation for chat sessions.

Tracks per-turn and cumulative token usage, maps model names to pricing,
and provides cost estimates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Per-1M token pricing (input, output) — update as pricing changes
_COST_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-haiku-3-5-20241022": (1.0, 5.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (1.0, 5.0),
    "claude-3-opus-20240229": (15.0, 75.0),
    "claude-3-sonnet-20240229": (3.0, 15.0),
    "claude-3-haiku-20240307": (0.25, 1.25),
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-4": (30.0, 60.0),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.0, 60.0),
    "o1-mini": (3.0, 12.0),
    "o3-mini": (1.1, 4.4),
}

# Rough estimate: 1 token ≈ 4 characters
CHARS_PER_TOKEN = 4


@dataclass
class TurnUsage:
    """Token usage for a single agent turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    model: str = ""
    cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "model": self.model,
            "cost": self.cost,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TurnUsage:
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            model=d.get("model", ""),
            cost=d.get("cost", 0.0),
        )


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute cost in USD for the given model and token counts.

    Returns 0.0 for unknown models.
    """
    # Try exact match first, then prefix match
    pricing = _COST_TABLE.get(model)
    if pricing is None:
        # Prefix matching for model variants (e.g. "claude-3-sonnet-20240229-v1")
        for key, val in _COST_TABLE.items():
            if model.startswith(key) or key.startswith(model):
                pricing = val
                break

    if pricing is None:
        return 0.0

    input_rate, output_rate = pricing
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


class UsageTracker:
    """Tracks cumulative token usage across turns."""

    def __init__(self):
        self.turns: list[TurnUsage] = []

    @property
    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def total_cost(self) -> float:
        return sum(t.cost for t in self.turns)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def record_turn(self, usage: TurnUsage) -> None:
        """Record usage from a completed turn.

        If cost is 0 and model is known, compute it automatically.
        """
        if usage.cost == 0.0 and usage.model:
            usage.cost = compute_cost(
                usage.model,
                usage.input_tokens,
                usage.output_tokens,
            )
        self.turns.append(usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": [t.to_dict() for t in self.turns],
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": self.total_cost,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UsageTracker:
        tracker = cls()
        for t in d.get("turns", []):
            tracker.turns.append(TurnUsage.from_dict(t))
        return tracker
