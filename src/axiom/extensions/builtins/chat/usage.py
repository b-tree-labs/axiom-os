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

#: Cache prices as multiples of the input price: (read, 5-minute write, 1-hour write). Only what a provider
#: documents is listed; a model not listed here and not declared by its provider entry is priced as
#: uncached (ADR-272 D1). Anthropic: reads 0.1, writes 1.25 (5 minutes) and 2 (1 hour).
_CACHE_TABLE: dict[str, tuple[float, float, float]] = {
    "claude-": (0.1, 1.25, 2.0),
}

# Rough estimate: 1 token ≈ 4 characters
CHARS_PER_TOKEN = 4


@dataclass
class TurnUsage:
    """Usage for one model call.

    ``cost`` is ``None`` when the call could not be priced: unknown is never reported as zero. A call
    that is free (a local model) carries ``0.0``. ``latency_ms`` and ``tool_calls`` are ``None`` when
    the call was not measured. ``turn`` groups the calls of one user turn (0 = not attributed).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    #: Cached input tokens read, and written, this call. ``None`` is "the server did not report cache use",
    #: which is not 0: unknown is never reported as zero (ADR-272 D6). The 5-minute and 1-hour fields are the
    #: part of ``cache_creation_tokens`` whose lifetime the reply gave.
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    #: ``input_tokens`` already holds the cached tokens (OpenAI format); Anthropic's count does not.
    input_includes_cache: bool = False
    model: str = ""
    cost: float | None = None
    #: What caching saved on this call: its cost with every prompt token at the input price, less ``cost``.
    #: Below zero when the writes cost more than the reads saved. ``None`` when the server reported no
    #: cache use or the call is unpriced.
    cache_saved: float | None = None
    provider: str = ""
    latency_ms: float | None = None
    tool_calls: int | None = None
    turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_write_5m_tokens": self.cache_write_5m_tokens,
            "cache_write_1h_tokens": self.cache_write_1h_tokens,
            "input_includes_cache": self.input_includes_cache,
            "model": self.model,
            "cost": self.cost,
            "cache_saved": self.cache_saved,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
            "tool_calls": self.tool_calls,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TurnUsage:
        cost = d.get("cost")
        tokens = (d.get("input_tokens", 0) or 0) + (d.get("output_tokens", 0) or 0)
        # Records written before cost could be unknown say 0.0 for a model nobody priced. Zero there
        # was a guess, not a price: re-ask the table and call it unpriced if the table cannot say.
        if cost == 0.0 and tokens and "provider" not in d and compute_cost(d.get("model", ""), 1, 1) is None:
            cost = None
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            # Records written before the cache was counted carry no field: not reported, not 0.
            cache_read_tokens=d.get("cache_read_tokens"),
            cache_creation_tokens=d.get("cache_creation_tokens"),
            cache_write_5m_tokens=d.get("cache_write_5m_tokens", 0) or 0,
            cache_write_1h_tokens=d.get("cache_write_1h_tokens", 0) or 0,
            input_includes_cache=bool(d.get("input_includes_cache", False)),
            model=d.get("model", ""),
            cost=cost,
            cache_saved=d.get("cache_saved"),
            provider=d.get("provider", ""),
            latency_ms=d.get("latency_ms"),
            tool_calls=d.get("tool_calls"),
            turn=d.get("turn", 0) or 0,
        )

    @property
    def prompt_tokens(self) -> int:
        """Every prompt token of the call: uncached, read from the cache and written to it."""
        if self.input_includes_cache:
            return self.input_tokens
        return self.input_tokens + (self.cache_read_tokens or 0) + (self.cache_creation_tokens or 0)

    @property
    def cache_hit_ratio(self) -> float | None:
        """Cached reads over every prompt token, or ``None`` when the server reported no cache use."""
        if self.cache_read_tokens is None:
            return None
        prompt = self.prompt_tokens
        return self.cache_read_tokens / prompt if prompt else None


@dataclass
class ModelCache:
    """One model's cache use over a session, for the usage page (RS-P11-A4)."""

    model: str
    calls: int
    #: Calls whose server reported cache use. The ratio and the saving cover these calls only.
    reported: int
    #: ``None`` when no call reported: "not reported", never 0%.
    hit_ratio: float | None
    #: ``None`` when no reporting call could be priced.
    saved: float | None


def price_of(model: str, provider: Any = None) -> tuple[float, float] | None:
    """The (input, output) price per 1M tokens for ``model``, or ``None`` when nobody knows it.

    Order of authority: the provider's own configured price (both halves, and only for the model it is
    configured for), a local provider (free, stated), the built-in table (longest listed name that the
    model name starts with), else ``None``.
    """
    if provider is not None:
        p_in = getattr(provider, "cost_per_1k_input", None)
        p_out = getattr(provider, "cost_per_1k_output", None)
        if p_in is not None and p_out is not None and model in ("", getattr(provider, "model", "")):
            return (float(p_in) * 1000.0, float(p_out) * 1000.0)
        try:
            from axiom.llm.placement import placement_of

            if placement_of(provider).placement == "local":
                return (0.0, 0.0)
        except Exception:  # noqa: BLE001 - placement is an input to pricing, not a reason to fail
            pass
    if not model:
        return None
    if model in _COST_TABLE:
        return _COST_TABLE[model]
    # Variants ("claude-3-sonnet-20240229-v1"): the longest listed name the model starts with. The
    # reverse test (a listed name starting with the model) let "gpt" borrow a real price.
    for key in sorted(_COST_TABLE, key=len, reverse=True):
        if model.startswith(key):
            return _COST_TABLE[key]
    return None


def cache_prices_of(model: str, provider: Any = None) -> tuple[float, float, float] | None:
    """(read, 5-minute write, 1-hour write) as multiples of the input price, or ``None`` when undeclared.

    The provider entry's own ``cache_read_price`` and ``cache_write_price`` come first, then the built-in
    table (longest listed prefix of the model name). ``None`` means priced as uncached.
    """
    if provider is not None and model in ("", getattr(provider, "model", "")):
        read = getattr(provider, "cache_read_price", None)
        writes = getattr(provider, "cache_write_price", None)
        if read is not None or isinstance(writes, dict):
            writes = writes if isinstance(writes, dict) else {}
            w5 = float(writes.get("5m", 1.0))
            return (float(read if read is not None else 1.0), w5, float(writes.get("1h", w5)))
    for key in sorted(_CACHE_TABLE, key=len, reverse=True):
        if model.startswith(key):
            return _CACHE_TABLE[key]
    return None


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    provider: Any = None,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    input_includes_cache: bool = False,
) -> float | None:
    """Cost in USD, or ``None`` when the model is unpriced. Never 0.0 for unknown.

    Uncached input x input price + reads x read multiplier + writes x write multiplier (by lifetime; a
    write with no stated lifetime is a 5-minute write) + output. A model with no declared cache prices is
    priced as uncached: every prompt token costs the input price, cached or not.
    ``input_includes_cache`` says ``input_tokens`` already holds the cached tokens.
    """
    price = price_of(model, provider)
    if price is None:
        return None
    cache = cache_prices_of(model, provider)
    if cache is None or not (cache_read or cache_write):
        # Priced as uncached means every prompt token at the input price. A count that leaves the
        # cached tokens out (Anthropic's) has them added back, or they would cost nothing.
        prompt = input_tokens + (0 if input_includes_cache else cache_read + cache_write)
        return (prompt * price[0] + output_tokens * price[1]) / 1_000_000
    read_x, write_5m_x, write_1h_x = cache
    uncached = input_tokens - ((cache_read + cache_write) if input_includes_cache else 0)
    split = cache_write_5m + cache_write_1h
    write_5m = cache_write_5m + max(0, cache_write - split)
    total = (
        max(0, uncached) * price[0]
        + cache_read * price[0] * read_x
        + write_5m * price[0] * write_5m_x
        + cache_write_1h * price[0] * write_1h_x
        + output_tokens * price[1]
    )
    return total / 1_000_000


def format_cost(cost: float | None) -> str:
    """``$0.0075``, or ``unpriced``. A cost nobody knows is never printed as a number."""
    return "unpriced" if cost is None else f"${cost:.4f}"


def estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


class UsageTracker:
    """Tracks cumulative token usage across calls, grouped into user turns."""

    def __init__(self):
        self.turns: list[TurnUsage] = []
        self._turn_counter = 0
        #: What the last turn sent besides the conversation (system prompt, tool schemas), in characters;
        #: ``None`` until a turn has run. Read by /context.
        self.prompt_overhead: dict[str, int] | None = None

    def begin_turn(self) -> int:
        """Mark the start of a user turn; calls recorded until the next one belong to it."""
        self._turn_counter += 1
        return self._turn_counter

    def note_overhead(self, *, system_chars: int, tools_chars: int, tool_count: int) -> None:
        self.prompt_overhead = {
            "system_chars": system_chars,
            "tools_chars": tools_chars,
            "tool_count": tool_count,
        }

    @property
    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def total_cost(self) -> float:
        """The priced portion only. Read ``unpriced_turns`` with it: this is a lower bound if it is >0."""
        return sum(t.cost for t in self.turns if t.cost is not None)

    @property
    def unpriced_turns(self) -> int:
        """User turns with at least one call that could not be priced."""
        keys = {(t.turn if t.turn else ("call", i)) for i, t in enumerate(self.turns) if t.cost is None}
        return len(keys)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def cost_label(self) -> str:
        """The total cost, saying how much of the session it does not cover."""
        missing = self.unpriced_turns
        noun = "turn" if missing == 1 else "turns"
        if not missing:
            return format_cost(self.total_cost)
        if not any(t.cost is not None for t in self.turns):
            return f"unpriced ({missing} {noun} unpriced)"
        return f"{format_cost(self.total_cost)} + {missing} {noun} unpriced"

    def record_turn(self, usage: TurnUsage, provider: Any = None) -> None:
        """Record usage from a completed call, pricing it if it carries no cost.

        ``provider`` is the provider that answered; its configured price (or its being local) is what
        prices the call. Without a price the cost stays ``None``.
        """
        if usage.cost is None:
            usage.cost = compute_cost(
                usage.model,
                usage.input_tokens,
                usage.output_tokens,
                provider=provider,
                cache_read=usage.cache_read_tokens or 0,
                cache_write=usage.cache_creation_tokens or 0,
                cache_write_5m=usage.cache_write_5m_tokens,
                cache_write_1h=usage.cache_write_1h_tokens,
                input_includes_cache=usage.input_includes_cache,
            )
        if usage.cache_saved is None and usage.cost is not None and usage.cache_read_tokens is not None:
            uncached = compute_cost(usage.model, usage.prompt_tokens, usage.output_tokens, provider=provider)
            if uncached is not None:
                usage.cache_saved = uncached - usage.cost
        if not usage.turn:
            usage.turn = self._turn_counter
        if not usage.provider and provider is not None:
            usage.provider = getattr(provider, "name", "") or ""
        self.turns.append(usage)

    def cache_by_model(self) -> list[ModelCache]:
        """Hit ratio and saving per model, in the order each model was first called."""
        rows: dict[str, ModelCache] = {}
        prompt: dict[str, int] = {}
        read: dict[str, int] = {}
        for t in self.turns:
            name = t.model or "unknown"
            row = rows.setdefault(name, ModelCache(name, 0, 0, None, None))
            row.calls += 1
            if t.cache_read_tokens is None:
                continue
            row.reported += 1
            prompt[name] = prompt.get(name, 0) + t.prompt_tokens
            read[name] = read.get(name, 0) + t.cache_read_tokens
            if t.cache_saved is not None:
                row.saved = (row.saved or 0.0) + t.cache_saved
        for name, row in rows.items():
            if prompt.get(name):
                row.hit_ratio = read[name] / prompt[name]
        return list(rows.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": [t.to_dict() for t in self.turns],
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": self.total_cost,
            "unpriced_turns": self.unpriced_turns,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UsageTracker:
        tracker = cls()
        for t in d.get("turns", []):
            tracker.turns.append(TurnUsage.from_dict(t))
        tracker._turn_counter = max((t.turn for t in tracker.turns), default=0)
        return tracker
