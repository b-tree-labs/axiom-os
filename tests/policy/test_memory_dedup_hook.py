# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.policy.memory_dedup_hook` (issue #202.3).

Pre-write semantic dedup at the tool-dispatch surface. Wires as a
`tool.pre_invoke` hook on `HookBus` so it composes with the existing
hook chain (audit, policy, future ext-imported tools all go through
the same gateway — see `docs/working/design-ext-import-and-tool-
dispatch-dedup.md`).

Behavior matrix:
  - tool isn't a memory-write tool          → pass-through (no decision)
  - memory-write + sim < threshold          → pass-through
  - memory-write + sim >= threshold         → deny with structured payload
  - memory-write + args.force=True          → pass-through (operator override)
  - threshold from env var                  → AXIOM_MEMORY_DEDUP_THRESHOLD honored
  - embedding probe raises                  → pass-through (don't break dispatch)
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Pattern matcher — is this tool a memory-write op?
# ---------------------------------------------------------------------------


class TestIsMemoryWriteTool:
    def test_compose_matches(self):
        from axiom.policy.memory_dedup_hook import is_memory_write_tool
        assert is_memory_write_tool("axiom_memory__compose") is True
        assert is_memory_write_tool("memory_compose") is True
        assert is_memory_write_tool("axi_memory_compose") is True

    def test_retrieve_does_not_match(self):
        """Reads must NOT be intercepted — only writes."""
        from axiom.policy.memory_dedup_hook import is_memory_write_tool
        assert is_memory_write_tool("axiom_memory__retrieve") is False
        assert is_memory_write_tool("memory_recall") is False
        assert is_memory_write_tool("memory_search") is False

    def test_unrelated_tools_do_not_match(self):
        from axiom.policy.memory_dedup_hook import is_memory_write_tool
        assert is_memory_write_tool("search") is False
        assert is_memory_write_tool("read_file") is False
        assert is_memory_write_tool("axi_rag_query") is False


# ---------------------------------------------------------------------------
# Pre-invoke hook behavior
# ---------------------------------------------------------------------------


def _payload(*, tool_name, content="some claim", force=False):
    """Build a ToolPreInvokePayload-shaped dict."""
    args = {"content": content}
    if force:
        args["force"] = True
    return {
        "tool_name": tool_name,
        "args": args,
        "principal": "@test:axiom",
        "classification": "",
        "ext_origin": "",
    }


class TestNonMemoryWriteToolsPassThrough:
    def test_unrelated_tool_returns_none(self, monkeypatch):
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        # Probe must not even be called for non-write tools
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (_ for _ in ()).throw(
                AssertionError("probe should not run for non-write tools")
            ),
        )
        result = memory_dedup_pre_invoke(_payload(tool_name="search"))
        assert result is None


class TestDedupDeniesOnHighSimilarity:
    def test_high_similarity_returns_deny_with_structured_payload(
        self, monkeypatch,
    ):
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.95, "fragment-abc-123"),
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
            content="The capital of France is Paris.",
        ))
        assert result is not None
        assert result["decision"] == "deny"
        # Reason is structured + legible to an LLM
        meta = result.get("metadata") or {}
        assert meta.get("deduped") is True
        assert meta.get("existing_fragment_id") == "fragment-abc-123"
        assert meta.get("similarity") == 0.95
        # Hint phrase for the LLM
        assert "already" in (meta.get("hint") or "").lower() or "known" in (meta.get("hint") or "").lower()

    def test_at_threshold_denies(self, monkeypatch):
        """Boundary: similarity == threshold → deny (>= comparison)."""
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        monkeypatch.setenv("AXIOM_MEMORY_DEDUP_THRESHOLD", "0.92")
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.92, "frag-y"),
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
        ))
        assert result is not None
        assert result["decision"] == "deny"


class TestDedupAllowsBelowThreshold:
    def test_low_similarity_returns_none(self, monkeypatch):
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.45, "frag-x"),
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
        ))
        assert result is None  # pass-through


class TestForceFlagBypasses:
    def test_force_true_bypasses_even_when_similar(self, monkeypatch):
        """Operator override: an explicit `args.force=True` skips the
        dedup probe entirely. Logged for audit elsewhere."""
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        probe_called = []
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (probe_called.append(True), (1.0, "x"))[1],
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose", force=True,
        ))
        assert result is None
        # Probe wasn't even invoked — force short-circuits before similarity
        assert probe_called == []


class TestThresholdEnvVar:
    def test_custom_threshold_from_env(self, monkeypatch):
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        # Raise threshold to 0.99 → 0.95 sim no longer denies
        monkeypatch.setenv("AXIOM_MEMORY_DEDUP_THRESHOLD", "0.99")
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.95, "frag-z"),
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
        ))
        assert result is None  # below the raised threshold → pass-through

    def test_default_threshold_when_env_unset(self, monkeypatch):
        from axiom.policy.memory_dedup_hook import (
            DEFAULT_DEDUP_THRESHOLD, memory_dedup_pre_invoke,
        )
        monkeypatch.delenv("AXIOM_MEMORY_DEDUP_THRESHOLD", raising=False)
        # Just above default → deny
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (DEFAULT_DEDUP_THRESHOLD + 0.01, "f"),
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
        ))
        assert result is not None
        assert result["decision"] == "deny"

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        from axiom.policy.memory_dedup_hook import (
            DEFAULT_DEDUP_THRESHOLD, memory_dedup_pre_invoke,
        )
        monkeypatch.setenv("AXIOM_MEMORY_DEDUP_THRESHOLD", "not-a-float")
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (DEFAULT_DEDUP_THRESHOLD + 0.01, "f"),
        )
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
        ))
        assert result is not None and result["decision"] == "deny"


class TestProbeFailureIsSafe:
    def test_probe_exception_passes_through(self, monkeypatch):
        """If the embedding probe raises (model unreachable, malformed
        content, ...), dispatch MUST continue. The dedup hook is an
        optimization, never a hard dependency."""
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (_ for _ in ()).throw(RuntimeError("embed unreachable")),
        )
        # Must not raise
        result = memory_dedup_pre_invoke(_payload(
            tool_name="axiom_memory__compose",
        ))
        assert result is None  # pass-through on probe failure


class TestEmptyContentSkipsProbe:
    def test_empty_args_content_is_pass_through(self, monkeypatch):
        """Nothing to dedup against if the args carry no content."""
        from axiom.policy.memory_dedup_hook import memory_dedup_pre_invoke
        probe_called = []
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (probe_called.append(True), (1.0, "x"))[1],
        )
        result = memory_dedup_pre_invoke({
            "tool_name": "axiom_memory__compose",
            "args": {},  # no content
            "principal": "@test:axiom",
            "classification": "",
            "ext_origin": "",
        })
        assert result is None
        assert probe_called == []


# ---------------------------------------------------------------------------
# Platform-adapter integration — `pre_invoke_handler` returning HookResult
# ---------------------------------------------------------------------------


class TestPlatformAdapter:
    def test_pre_invoke_handler_pass_through_returns_allow(
        self, monkeypatch,
    ):
        from axiom.infra.hooks import HookContext
        from axiom.policy.memory_dedup_hook import pre_invoke_handler
        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.1, "x"),
        )
        ctx = HookContext(
            event="tool.pre_invoke",
            payload=_payload(tool_name="axiom_memory__compose"),
            principal="@test:axiom",
        )
        result = pre_invoke_handler(ctx)
        assert result.decision == "allow"

    def test_pre_invoke_handler_deny_encodes_metadata_in_reason(
        self, monkeypatch,
    ):
        """Structured metadata can't ride on HookResult today (no
        metadata field); the wrapper JSON-encodes it into the reason
        string so downstream consumers can recover the structured
        info."""
        import json

        from axiom.infra.hooks import HookContext
        from axiom.policy.memory_dedup_hook import pre_invoke_handler

        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.97, "frag-existing"),
        )
        ctx = HookContext(
            event="tool.pre_invoke",
            payload=_payload(tool_name="axiom_memory__compose"),
            principal="@test:axiom",
        )
        result = pre_invoke_handler(ctx)
        assert result.decision == "deny"
        # Reason has a human-readable prefix + JSON metadata tail
        assert "semantic duplicate" in result.reason
        assert "metadata=" in result.reason
        # Tail parses back to the structured payload
        metadata_json = result.reason.split("metadata=", 1)[1]
        metadata = json.loads(metadata_json)
        assert metadata["deduped"] is True
        assert metadata["existing_fragment_id"] == "frag-existing"
        assert metadata["similarity"] == 0.97


# ---------------------------------------------------------------------------
# Full end-to-end: register the hook on a HookBus, call dispatch_tool,
# verify HookDenied propagates with the structured reason
# ---------------------------------------------------------------------------


class TestEndToEndViaDispatchTool:
    def test_dispatch_denies_when_memory_write_is_duplicate(
        self, monkeypatch,
    ):
        from axiom.infra.hooks import HookBus, HookSpec
        from axiom.infra.hooks.types import HookDenied
        from axiom.infra.tool_gateway import dispatch_tool
        from axiom.policy.memory_dedup_hook import pre_invoke_handler

        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.99, "frag-dup"),
        )

        bus = HookBus()
        bus.register(HookSpec(
            event="tool.pre_invoke",
            entry=pre_invoke_handler,
            priority=50,
            fail_mode="warn",
            source="platform",
        ))

        # Stub dispatcher — should never be called when dedup denies
        dispatched = []
        def fake_dispatcher(name, args):
            dispatched.append((name, args))
            return {"ok": True}

        try:
            dispatch_tool(
                tool_name="axiom_memory__compose",
                args={"content": "duplicate fact"},
                principal="@test:axiom",
                hookbus=bus,
                dispatcher=fake_dispatcher,
            )
            assert False, "expected HookDenied"
        except HookDenied as exc:
            assert "semantic duplicate" in exc.reason
            assert "frag-dup" in exc.reason
        # And the underlying tool was never invoked
        assert dispatched == []

    def test_dispatch_proceeds_when_no_duplicate(self, monkeypatch):
        from axiom.infra.hooks import HookBus, HookSpec
        from axiom.infra.tool_gateway import dispatch_tool
        from axiom.policy.memory_dedup_hook import pre_invoke_handler

        monkeypatch.setattr(
            "axiom.policy.memory_dedup_hook._compute_max_similarity",
            lambda content: (0.1, "x"),
        )

        bus = HookBus()
        bus.register(HookSpec(
            event="tool.pre_invoke",
            entry=pre_invoke_handler,
            priority=50,
            fail_mode="warn",
            source="platform",
        ))

        dispatched = []
        def fake_dispatcher(name, args):
            dispatched.append((name, args))
            return {"ok": True}

        result = dispatch_tool(
            tool_name="axiom_memory__compose",
            args={"content": "novel claim"},
            principal="@test:axiom",
            hookbus=bus,
            dispatcher=fake_dispatcher,
        )
        assert result == {"ok": True}
        assert len(dispatched) == 1
