# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-3 prompt composer.

Seven-layer system-prompt composition with composite contributions,
debug dump, compaction-aware drop policy, and Anthropic cache-block
output.

Layers (stable → volatile):
    1. identity        — persona/role
    2. capabilities    — tool list summary
    3. policies        — guardrails
    4. domain_context  — CLAUDE.md, workspace, facility pack, classroom role
    5. session_memory  — pinned facts / preferences / active tasks
    6. retrieved       — RAG context (T0-1)
    7. live            — user turn, mode flags, tool-result echoes

Cache boundary: layers 1–5 are cacheable; 6–7 are fresh each turn.
"""

from __future__ import annotations

import pytest

from axiom.infra.prompt_composer import (
    CACHEABLE_LAYERS,
    LAYERS,
    LayerContribution,
    PromptComposer,
)

# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------


class TestLayerRegistry:
    def test_seven_layers_defined(self):
        assert len(LAYERS) == 7

    def test_cacheable_layers_are_first_five(self):
        assert LAYERS[:5] == CACHEABLE_LAYERS

    def test_layer_names_are_stable(self):
        """Names are part of the public API — extensions reference them."""
        assert LAYERS == (
            "identity",
            "capabilities",
            "policies",
            "domain_context",
            "session_memory",
            "retrieved",
            "live",
        )


class TestComposerBasics:
    def test_empty_composer_yields_empty_prompt(self):
        c = PromptComposer()
        assert c.render_text() == ""
        assert c.render_blocks() == []

    def test_single_contribution_renders(self):
        c = PromptComposer()
        c.add("identity", name="persona", content="You are neut.", source="axiom")
        assert "You are neut." in c.render_text()

    def test_unknown_layer_raises(self):
        c = PromptComposer()
        with pytest.raises(ValueError, match="layer"):
            c.add("nonexistent_layer", name="x", content="y", source="test")


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_layers_render_in_canonical_order(self):
        c = PromptComposer()
        # Add out of order to prove the composer re-orders.
        c.add("live", name="turn", content="LIVE", source="chat")
        c.add("identity", name="persona", content="IDENT", source="axiom")
        c.add("retrieved", name="rag", content="RAG", source="t0-1")
        text = c.render_text()
        assert text.index("IDENT") < text.index("RAG") < text.index("LIVE")

    def test_contributions_within_layer_preserve_add_order(self):
        c = PromptComposer()
        c.add("domain_context", name="claude_md", content="AAA", source="workspace")
        c.add("domain_context", name="classroom", content="BBB", source="classroom_ext")
        c.add("domain_context", name="facility", content="CCC", source="facility_pack")
        text = c.render_text()
        assert text.index("AAA") < text.index("BBB") < text.index("CCC")


# ---------------------------------------------------------------------------
# Cache boundary
# ---------------------------------------------------------------------------


class TestCacheBlocks:
    def test_cacheable_layers_emit_cache_control(self):
        c = PromptComposer()
        c.add("identity", name="persona", content="I", source="axiom")
        c.add("retrieved", name="rag", content="R", source="t0-1")
        blocks = c.render_blocks()
        # The prefix block carrying cacheable layers has cache_control.
        cached = [b for b in blocks if b.get("cache_control")]
        fresh = [b for b in blocks if not b.get("cache_control")]
        assert cached
        assert fresh
        # Cache block precedes fresh block.
        assert blocks.index(cached[-1]) < blocks.index(fresh[0])

    def test_only_cacheable_content_in_cached_block(self):
        c = PromptComposer()
        c.add("identity", name="persona", content="IDENT", source="axiom")
        c.add("live", name="turn", content="LIVE", source="chat")
        blocks = c.render_blocks()
        cached_text = next(b["text"] for b in blocks if b.get("cache_control"))
        fresh_text = next(b["text"] for b in blocks if not b.get("cache_control"))
        assert "IDENT" in cached_text
        assert "LIVE" not in cached_text
        assert "LIVE" in fresh_text

    def test_no_cached_block_when_no_cacheable_layers_populated(self):
        """If nothing is in layers 1–5, we shouldn't emit an empty cached block."""
        c = PromptComposer()
        c.add("live", name="turn", content="LIVE", source="chat")
        blocks = c.render_blocks()
        assert not any(b.get("cache_control") for b in blocks)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompaction:
    def test_optional_drops_before_required(self):
        c = PromptComposer()
        c.add("identity", name="p", content="KEEP" * 10,
              source="axiom", required=True)
        c.add("domain_context", name="huge", content="BULK" * 100,
              source="workspace", required=False)
        # Fake small budget; compact must drop the optional layer 4 contribution
        c.compact_to_budget(max_tokens=50,
                            count_fn=lambda s: len(s) // 4)
        text = c.render_text()
        assert "KEEP" in text
        assert "BULK" not in text

    def test_required_is_never_dropped(self):
        c = PromptComposer()
        c.add("policies", name="critical", content="CRITICAL" * 100,
              source="policy", required=True)
        c.compact_to_budget(max_tokens=5, count_fn=lambda s: len(s) // 4)
        # Still there even under absurdly tight budget.
        assert "CRITICAL" in c.render_text()


# ---------------------------------------------------------------------------
# Debug + observability
# ---------------------------------------------------------------------------


class TestDebugDump:
    def test_debug_lists_every_contribution(self):
        c = PromptComposer()
        c.add("identity", name="persona", content="X", source="axiom")
        c.add("domain_context", name="claude_md", content="Y", source="workspace")
        dump = c.debug()
        names = [d.name for d in dump]
        assert names == ["persona", "claude_md"]
        assert isinstance(dump[0], LayerContribution)
        assert dump[0].source == "axiom"
        assert dump[0].layer == "identity"

    def test_debug_records_token_counts(self):
        c = PromptComposer(count_fn=lambda s: len(s))
        c.add("identity", name="persona", content="abc", source="axiom")
        [entry] = c.debug()
        assert entry.tokens == 3

    def test_observability_payload_shape(self):
        c = PromptComposer(count_fn=lambda s: len(s))
        c.add("identity", name="persona", content="abc", source="axiom")
        c.add("live", name="turn", content="de", source="chat")
        payload = c.observability_payload()
        assert payload["fact_kind"] == "prompt_composition"
        assert payload["layer_counts"]["identity"] == 3
        assert payload["layer_counts"]["live"] == 2
        # Cache boundary index is 5 (first fresh layer is 'retrieved').
        assert payload["cache_boundary_layer"] == "retrieved"


# ---------------------------------------------------------------------------
# Extension registration
# ---------------------------------------------------------------------------


class TestReplaceContribution:
    def test_replace_same_name_updates_content(self):
        """A second add with the same (layer, name) replaces the first —
        lets an extension update its own contribution without leaking."""
        c = PromptComposer()
        c.add("domain_context", name="classroom", content="v1",
              source="classroom_ext")
        c.add("domain_context", name="classroom", content="v2",
              source="classroom_ext")
        text = c.render_text()
        assert "v2" in text
        assert "v1" not in text

    def test_remove_contribution(self):
        c = PromptComposer()
        c.add("domain_context", name="classroom", content="x",
              source="classroom_ext")
        c.remove("domain_context", "classroom")
        assert "x" not in c.render_text()
