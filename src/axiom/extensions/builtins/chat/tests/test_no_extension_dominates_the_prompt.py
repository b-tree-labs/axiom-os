# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One extension must not become the agent's personality.

Measured on a real install: 69 tools across twelve families, and exactly one
prompt contributor. `model_corral` holds five of those tools — 7% — and
contributed three of the prompt's six fragments, including one in `identity`.
`classroom`, with eighteen tools, contributed nothing.

The agent's character was therefore decided by whichever extension happened to
write a prompt first. Asked to list telemetry metrics it offered `model_search`
and `facility_list`, because that is the only domain it had been given a
character for.

Two rules keep that from recurring, and both are platform-level so every
extension gets them without asking:

`identity` is who the agent IS. That belongs to the platform and the product —
an extension declaring it is a category error, and the effect is that installing
an extension changes who the assistant thinks it is.

And no extension may take an unbounded share of the remaining layers, because
the first one to write a large prompt wins an argument nobody knew was
happening.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.prompt_fairness import (
    EXTENSION_TOKEN_BUDGET,
    enforce_contribution_limits,
)


def _frag(layer, name, content, source="ext.a"):
    return {"layer": layer, "name": name, "content": content, "source": source}


def test_an_extension_cannot_declare_the_agents_identity():
    kept = enforce_contribution_limits(
        [_frag("identity", "model_corral_role", "You are a reactor physicist.")]
    )

    assert kept == []


def test_capabilities_and_policies_are_still_an_extensions_to_shape():
    """Negative control: this must not silence extensions, only bound them."""
    kept = enforce_contribution_limits(
        [
            _frag("capabilities", "corral_next_steps", "After a tool, suggest..."),
            _frag("policies", "corral_guardrails", "Never invent a composition."),
        ]
    )

    assert len(kept) == 2


def test_one_extension_cannot_take_an_unbounded_share():
    huge = "word " * (EXTENSION_TOKEN_BUDGET * 2)
    kept = enforce_contribution_limits([_frag("capabilities", "big", huge)])

    assert len(kept) == 1
    assert len(kept[0]["content"]) < len(huge)


def test_the_budget_is_per_extension_not_per_fragment():
    """Splitting a large prompt across three fragments must not evade it."""
    chunk = "word " * EXTENSION_TOKEN_BUDGET
    kept = enforce_contribution_limits(
        [
            _frag("capabilities", "a", chunk),
            _frag("capabilities", "b", chunk),
            _frag("policies", "c", chunk),
        ]
    )

    total = sum(len(f["content"]) for f in kept)
    assert total < len(chunk) * 3


def test_a_second_extension_gets_its_own_budget():
    """Negative control: the cap is per contributor, so a well-behaved
    extension is not squeezed out by a greedy one."""
    chunk = "word " * 20
    kept = enforce_contribution_limits(
        [
            _frag("capabilities", "a", chunk, source="ext.a"),
            _frag("capabilities", "b", chunk, source="ext.b"),
        ]
    )

    assert len(kept) == 2
    assert all(f["content"] for f in kept)


def test_a_modest_contribution_is_untouched():
    """The common case must pass through byte-identical."""
    content = "After showing materials, offer to generate the cards."
    kept = enforce_contribution_limits([_frag("capabilities", "small", content)])

    assert kept[0]["content"] == content


def test_a_normal_multi_fragment_extension_fits():
    """The cap exists to stop domination, not to squeeze ordinary guidance.

    Calibrated against the real one: model_corral contributes a role line, a
    next-steps block and a guardrails block — 331 tokens together. At the first
    budget I picked, 250, its guardrails consumed the allowance and the rest was
    dropped, which meant declaration order decided what survived. A safety rule
    losing a race to a suggestion list is not a policy anyone chose.
    """
    role = "x" * 100
    next_steps = "y" * 480
    guardrails = "z" * 760  # ~335 tokens all told, the measured real case

    kept = enforce_contribution_limits(
        [
            _frag("capabilities", "role", role),
            _frag("capabilities", "next_steps", next_steps),
            _frag("policies", "guardrails", guardrails),
        ]
    )

    assert len(kept) == 3, "a typical extension should not lose a fragment"
    assert kept[2]["content"] == guardrails, "guardrails must survive intact"
