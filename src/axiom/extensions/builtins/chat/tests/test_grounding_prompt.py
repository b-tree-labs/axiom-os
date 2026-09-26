# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Axiom-default grounding + honesty policy fragment."""

from __future__ import annotations

from axiom.extensions.builtins.chat.grounding_prompt import (
    GROUNDING_POLICY,
    grounding_policy_fragment,
)
from axiom.infra.prompt_composer import LAYERS, PromptComposer


def test_fragment_shape_and_layer():
    f = grounding_policy_fragment()
    assert f["layer"] == "policies" and f["layer"] in LAYERS
    assert f["name"] == "grounding_and_honesty"
    assert f["source"] == "axiom"
    assert f["required"] is True


def test_key_directives_present():
    t = GROUNDING_POLICY.lower()
    assert "use your registered tools" in t          # prefer tools over recall
    assert "never fabricate" in t                    # no made-up values
    assert "null or empty" in t                      # null → report it, don't fill in
    assert "cite the tool or source" in t            # faithful citation
    assert "report tool failures honestly" in t      # no fake successes


def test_domain_agnostic():
    # The Axiom default must name no tool and no domain (those live in the
    # consumer's domain_context layer).
    t = GROUNDING_POLICY.lower()
    for domain_word in ("reactor", "nuclear", "telemetry", "netl", "triga", "gemma"):
        assert domain_word not in t


def test_composes_into_the_policies_layer():
    c = PromptComposer()
    f = grounding_policy_fragment()
    c.add(f["layer"], name=f["name"], content=f["content"], source=f["source"], required=f["required"])
    rendered = c.render_text()
    assert "Grounding and honesty" in rendered
