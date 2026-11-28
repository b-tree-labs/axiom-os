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

# Where a contribution lives in a cache-stable request (ADR-272 D2, RS-P8).
#
# ``session``: true for the whole conversation, so it sits in the stable system part.
# ``per_request``: changes from one request to the next, so it rides in the turn tail, after the
# history. A contribution that does not say is ``per_request``: a new contribution cannot break the
# prefix by accident. The ``policies`` layer is the exception that is not a choice: the ADR-160
# policy tier stays in the system prompt whatever a contribution declares.
SCOPES: tuple[str, ...] = ("session", "per_request")
DEFAULT_SCOPE: str = "per_request"
ALWAYS_SYSTEM_LAYERS: tuple[str, ...] = ("policies",)

#: The line that opens a turn tail, so the model reads what follows as context for this request and
#: not as something the person typed.
TURN_TAIL_LABEL: str = "--- Context for this request only (not part of the person's message) ---"


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
    scope: str = DEFAULT_SCOPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "content_chars": len(self.content),
            "source": self.source,
            "required": self.required,
            "tokens": self.tokens,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class PromptParts:
    """The prompt as two parts: the stable system part and the turn tail (empty when there is none)."""

    system: str
    tail: str


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
        scope: str | None = None,
    ) -> LayerContribution:
        """Add (or replace) a named contribution in ``layer``.

        ``scope`` is ``"session"`` or ``"per_request"``; ``None`` (undeclared) is ``per_request``.
        It decides only where ``render_parts`` puts the contribution; ``render_text`` and
        ``render_blocks`` ignore it.
        """
        if layer not in self._by_layer:
            raise ValueError(
                f"unknown layer {layer!r}; must be one of {LAYERS}"
            )
        if scope is None:
            scope = DEFAULT_SCOPE
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; must be one of {SCOPES}")
        tokens = self._count_fn(content) if content else 0
        contrib = LayerContribution(
            layer=layer,
            name=name,
            content=content,
            source=source,
            required=required,
            tokens=tokens,
            scope=scope,
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

    def render_parts(self) -> PromptParts:
        """The cache-stable layout: the system part and the turn tail, rendered apart.

        The system part is every ``session`` contribution plus the whole ``policies`` layer, in layer
        order and joined as ``render_text`` joins them. The tail is every other contribution, in
        layer order, after ``TURN_TAIL_LABEL``; it is empty (no label) when nothing is per-request.
        """
        system_parts: list[str] = []
        tail_parts: list[str] = []
        for layer in LAYERS:
            for contrib in self._by_layer[layer].values():
                if not contrib.content:
                    continue
                stays = layer in ALWAYS_SYSTEM_LAYERS or contrib.scope == "session"
                (system_parts if stays else tail_parts).append(contrib.content)
        tail = "\n\n".join([TURN_TAIL_LABEL, *tail_parts]) if tail_parts else ""
        return PromptParts(system="\n\n".join(system_parts), tail=tail)

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
        members: list[LayerContribution] = []
        for layer in LAYERS:
            layer_tokens = 0
            for contrib in self._by_layer[layer].values():
                layer_tokens += contrib.tokens
                contributions.append(contrib.to_dict())
                members.append(contrib)
            if layer_tokens:
                layer_counts[layer] = layer_tokens

        # A stable name for the *design* this turn was composed from, so turns
        # that shared one can be grouped and compared. Computed over the
        # cacheable layers only — the per-turn ones differ by construction, and
        # including them would give every turn a unique id and make grouping,
        # the reason it exists, impossible.
        from axiom.infra.prompt_stack_id import describe_stack

        stack = describe_stack(members)

        return {
            "fact_kind": "prompt_composition",
            "stack_id": stack["stack_id"],
            "stack_fragments": stack["fragments"],
            "layers": list(LAYERS),
            "cacheable_layers": list(CACHEABLE_LAYERS),
            "cache_boundary_layer": _CACHE_BOUNDARY_LAYER,
            "layer_counts": layer_counts,
            "total_tokens": sum(layer_counts.values()),
            "contributions": contributions,
        }
