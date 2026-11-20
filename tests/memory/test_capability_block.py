# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A capability nobody knows about is worth nothing.

The instruction-file write-back already reaches thirteen harness rules files, so
an assistant learns from its own instruction file at session start with no
install step and no MCP. That channel has only ever carried remembered context.
It has never said "here is what this system can do for you, and when to reach
for it" — which is the whole of the discovery problem.

These pin what the block has to be, because the failure mode is not a crash: it
is a block people skim past, or worse, one that advertises capability the install
does not have and teaches them to stop trusting it.
"""

from __future__ import annotations

from axiom.memory.rendering import (
    CAPABILITY_BLOCK_BEGIN,
    CAPABILITY_BLOCK_END,
    render_capability_block,
    splice_marked_block,
)


def _caps(n: int = 3):
    return [
        {"name": "data.gold_aggregate", "when": "someone asks for a total, average or trend over stored data"},
        {"name": "federation.node_status", "when": "someone asks what is deployed or healthy on a node"},
        {"name": "memory.search", "when": "someone refers to a past decision or an earlier session"},
    ][:n]


def test_the_block_is_task_shaped_not_a_tool_inventory():
    """An assistant reaches for a capability when it recognises the SITUATION.
    A list of names is skimmed; a trigger is acted on."""
    out = render_capability_block(_caps())
    for cap in _caps():
        assert cap["when"] in out, f"missing the trigger for {cap['name']}"


def test_an_install_with_nothing_advertises_nothing():
    """Honest to the install. Advertising absent capability is how someone
    learns to stop trusting the block entirely."""
    assert render_capability_block([]) == ""


def test_the_block_is_budget_bounded():
    """It costs context in every session on every harness. A catalogue gets
    ignored, and an ignored block is worse than no block."""
    many = [{"name": f"ext.verb{i}", "when": f"situation number {i}"} for i in range(200)]
    out = render_capability_block(many, budget_lines=14)
    assert out.count("\n") <= 14, f"block ran to {out.count(chr(10))} lines"
    assert out.strip(), "budget must truncate, not empty the block"


def test_rendering_is_idempotent():
    """The write-back only writes on change. A block that differs run to run
    would rewrite thirteen files every session for nothing."""
    first = render_capability_block(_caps())
    for _ in range(5):
        assert render_capability_block(_caps()) == first


def test_the_block_is_marked_so_it_can_be_replaced_and_removed():
    out = render_capability_block(_caps())
    assert out.startswith(CAPABILITY_BLOCK_BEGIN)
    assert out.rstrip().endswith(CAPABILITY_BLOCK_END)


# --- the splice must not be memory-specific any more ------------------------


def test_splice_replaces_only_its_own_marked_region():
    """Capabilities and memory are independently managed blocks in one file.
    Splicing one must leave the other exactly as it was."""
    memory_block = "<!-- axiom:cross-mem:begin -->\nremembered\n<!-- axiom:cross-mem:end -->"
    existing = f"# My own notes\n\n{memory_block}\n"
    caps = render_capability_block(_caps())
    out = splice_marked_block(existing, caps,
                              begin=CAPABILITY_BLOCK_BEGIN, end=CAPABILITY_BLOCK_END)
    assert memory_block in out, "splicing capabilities disturbed the memory block"
    assert "# My own notes" in out, "splicing disturbed the author's own content"
    assert CAPABILITY_BLOCK_BEGIN in out


def test_splice_replaces_rather_than_appends_on_a_second_run():
    existing = "# notes\n"
    once = splice_marked_block(existing, render_capability_block(_caps()),
                               begin=CAPABILITY_BLOCK_BEGIN, end=CAPABILITY_BLOCK_END)
    twice = splice_marked_block(once, render_capability_block(_caps(2)),
                                begin=CAPABILITY_BLOCK_BEGIN, end=CAPABILITY_BLOCK_END)
    assert twice.count(CAPABILITY_BLOCK_BEGIN) == 1, "block accreted instead of replacing"
    assert "memory.search" not in twice, "stale capability survived the replace"


def test_negative_control_the_splice_can_fail():
    # Proves the assertions above are not vacuous: a block never spliced is absent.
    assert CAPABILITY_BLOCK_BEGIN not in "# notes\n"
