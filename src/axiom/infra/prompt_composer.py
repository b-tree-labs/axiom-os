# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T0-3 seven-layer prompt composer.

Replaces the ChatAgent's string-concatenation system prompt with an
ordered, composable structure. Every layer is a *list of
contributions* (not one string) so multiple sources — prompt registry,
policy engine, classroom extension, workspace detector — can plug in
without colliding.

Cache boundary: layers 1–5 are cacheable (stable across turns); 6–7
are fresh each turn. ``render_blocks`` emits Anthropic-style content
blocks with ``cache_control: {type: "ephemeral"}`` on the cached
prefix only.

Compaction: every contribution carries ``required: bool``. Under
budget pressure, ``compact_to_budget`` drops optional contributions in
reverse layer order until the total fits, never dropping required.

Observability: ``observability_payload`` produces a dict suitable for
writing as a ``MemoryFragment(episodic)`` with
``fact_kind = "prompt_composition"`` so per-turn composition can be
inspected post hoc.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Layer registry (public — extensions reference these names)
# ---------------------------------------------------------------------------

LAYERS: tuple[str, ...] = (
    "identity",
    "capabilities",
    "policies",
    "domain_context",
    "session_memory",
    "retrieved",
    "live",
)

CACHEABLE_LAYERS: tuple[str, ...] = LAYERS[:5]
_CACHE_BOUNDARY_LAYER: str = LAYERS[5]  # first non-cacheable


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerContribution:
    """One named contribution to one layer of the system prompt."""

    layer: str
    name: str
    content: str
    source: str
    required: bool = True
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "content_chars": len(self.content),
            "source": self.source,
            "required": self.required,
            "tokens": self.tokens,
        }


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def _default_count(text: str) -> int:
    # Late import so the composer stays useful without the tokenizer.
    try:
        from axiom.infra.token_counter import count_tokens

        return count_tokens(text)
    except Exception:
        return max(1, len(text) // 4) if text else 0


class PromptComposer:
    """Ordered, per-layer system-prompt composer.

    Usage:
        composer = PromptComposer()
        composer.add("identity", name="persona", content=..., source="axiom")
        composer.add("retrieved", name="rag_context", content=block, source="t0-1")
        blocks = composer.render_blocks()   # Anthropic-format content blocks
        # or
        text = composer.render_text()        # flat string (local models)
    """

    def __init__(self, count_fn: Callable[[str], int] | None = None) -> None:
        self._count_fn = count_fn or _default_count
        # layer -> ordered dict of name -> LayerContribution
        self._by_layer: dict[str, dict[str, LayerContribution]] = {
            layer: {} for layer in LAYERS
        }

    # -- write ---------------------------------------------------------------

    def add(
        self,
        layer: str,
        *,
        name: str,
        content: str,
        source: str,
        required: bool = True,
    ) -> LayerContribution:
        """Add (or replace) a named contribution in ``layer``."""
        if layer not in self._by_layer:
            raise ValueError(
                f"unknown layer {layer!r}; must be one of {LAYERS}"
            )
        tokens = self._count_fn(content) if content else 0
        contrib = LayerContribution(
            layer=layer,
            name=name,
            content=content,
            source=source,
            required=required,
            tokens=tokens,
        )
        # Replace preserves insertion order only when the key already exists.
        self._by_layer[layer][name] = contrib
        return contrib

    def remove(self, layer: str, name: str) -> None:
        """Remove a named contribution. Silent no-op if absent."""
        self._by_layer.get(layer, {}).pop(name, None)

    # -- read ----------------------------------------------------------------

    def debug(self) -> list[LayerContribution]:
        """All contributions in canonical order — for debug inspection."""
        out: list[LayerContribution] = []
        for layer in LAYERS:
            out.extend(self._by_layer[layer].values())
        return out

    def render_text(self) -> str:
        """Flat-string render (e.g., for legacy gateway paths)."""
        parts: list[str] = []
        for layer in LAYERS:
            for contrib in self._by_layer[layer].values():
                if contrib.content:
                    parts.append(contrib.content)
        return "\n\n".join(parts)

    def render_blocks(self) -> list[dict[str, Any]]:
        """Anthropic-format content blocks, cache prefix + fresh suffix."""
        cached_parts: list[str] = []
        fresh_parts: list[str] = []
        for layer in LAYERS:
            bucket = cached_parts if layer in CACHEABLE_LAYERS else fresh_parts
            for contrib in self._by_layer[layer].values():
                if contrib.content:
                    bucket.append(contrib.content)

        blocks: list[dict[str, Any]] = []
        if cached_parts:
            blocks.append({
                "type": "text",
                "text": "\n\n".join(cached_parts),
                "cache_control": {"type": "ephemeral"},
            })
        if fresh_parts:
            blocks.append({
                "type": "text",
                "text": "\n\n".join(fresh_parts),
            })
        return blocks

    # -- compaction ----------------------------------------------------------

    def compact_to_budget(
        self,
        max_tokens: int,
        count_fn: Callable[[str], int] | None = None,
    ) -> list[LayerContribution]:
        """Drop optional contributions in reverse layer order until fit.

        Returns the list of contributions that were dropped (for
        observability — callers can log them).
        """
        count = count_fn or self._count_fn
        dropped: list[LayerContribution] = []

        def total() -> int:
            return sum(
                count(c.content)
                for layer in LAYERS
                for c in self._by_layer[layer].values()
            )

        # Walk layers volatile → stable; within a layer, drop in reverse
        # add-order. Required contributions are never touched.
        for layer in reversed(LAYERS):
            bucket = self._by_layer[layer]
            for name in list(reversed(list(bucket.keys()))):
                if total() <= max_tokens:
                    return dropped
                contrib = bucket[name]
                if contrib.required:
                    continue
                del bucket[name]
                dropped.append(contrib)
        return dropped

    # -- observability --------------------------------------------------------

    def observability_payload(self) -> dict[str, Any]:
        """Dict suitable for a MemoryFragment(episodic) with
        ``fact_kind = "prompt_composition"``."""
        layer_counts: dict[str, int] = {}
        contributions: list[dict[str, Any]] = []
        for layer in LAYERS:
            layer_tokens = 0
            for contrib in self._by_layer[layer].values():
                layer_tokens += contrib.tokens
                contributions.append(contrib.to_dict())
            if layer_tokens:
                layer_counts[layer] = layer_tokens
        return {
            "fact_kind": "prompt_composition",
            "layers": list(LAYERS),
            "cacheable_layers": list(CACHEABLE_LAYERS),
            "cache_boundary_layer": _CACHE_BOUNDARY_LAYER,
            "layer_counts": layer_counts,
            "total_tokens": sum(layer_counts.values()),
            "contributions": contributions,
        }
