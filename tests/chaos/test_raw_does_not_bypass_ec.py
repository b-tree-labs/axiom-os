# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security: `raw` must not be a way around export control.

`raw` skips the system prompt, retrieval, tools and session — every
augmentation layer. The question that matters is whether it also skips EXPORT
CONTROL, because a flag that quietly downgrades an export-controlled request
to a public provider would be far worse than a benchmarking convenience.

It does not, and this pins WHY rather than trusting the reading:

- export control is enforced at the LLM GATEWAY, on `routing_tier` — not in
  the system prompt, which is the part `raw` empties;
- the routing classifier runs in `turn()` BEFORE the raw branch, so the tier
  is decided whether or not the caller asked for raw;
- `_raw_turn` forwards `routing_tier` and `routing_decision` to the gateway.

Delete any one of those and an export-controlled question could be answered by
a public model. That is what these tests exist to notice.
"""

from __future__ import annotations

import inspect

from axiom.extensions.builtins.chat import agent as agent_module


class TestTheTierSurvivesTheBypass:
    def test_raw_forwards_the_routing_tier_to_the_gateway(self):
        source = inspect.getsource(agent_module.ChatAgent._raw_turn)
        assert "routing_tier=routing_tier" in source, (
            "a raw turn that drops the tier lets an export-controlled request "
            "be answered by whichever provider is default"
        )

    def test_raw_forwards_the_routing_decision(self):
        """The decision carries WHY the tier was chosen; the gateway's refusal
        messages and the audit row are both built from it."""
        source = inspect.getsource(agent_module.ChatAgent._raw_turn)
        assert "routing_decision=routing_decision" in source

    def test_the_caller_hands_raw_the_classified_tier(self):
        """The turn body must pass the tier it CLASSIFIED, not the parameter
        default — `_raw_turn`'s own default is the permissive "any".

        `turn()` delegates to `_turn_impl`, which is where the raw branch and
        the classifier both live.
        """
        source = inspect.getsource(agent_module.ChatAgent._turn_impl)
        assert "self._raw_turn(" in source
        call = source[source.index("self._raw_turn(") :][:220]
        assert "routing_tier=routing_tier" in call, call

    def test_the_permissive_default_is_only_a_signature_default(self):
        """If `_raw_turn` were ever called without a tier it would default to
        "any". Pinned so the default is visible rather than surprising."""
        signature = inspect.signature(agent_module.ChatAgent._raw_turn)
        assert signature.parameters["routing_tier"].default == "any"


class TestExportControlIsNotAPromptConcern:
    """The layer `raw` empties is the system prompt. If export control were
    enforced there, `raw` WOULD be a bypass — so this pins that it is not."""

    def test_the_gateway_owns_the_export_controlled_tier(self):
        from axiom.llm import gateway

        source = inspect.getsource(gateway)
        assert "export_controlled" in source
        assert "EC_UNATTRIBUTED" in source, (
            "the unattributed-EC refusal is the gateway's, and a raw turn "
            "still goes through the gateway"
        )

    def test_a_raw_turn_still_calls_the_gateway(self):
        source = inspect.getsource(agent_module.ChatAgent._raw_turn)
        assert "self.gateway.complete_with_tools(" in source

    def test_raw_empties_the_prompt_not_the_routing(self):
        """Both halves of the claim, together: the system prompt goes, the
        tier stays."""
        source = inspect.getsource(agent_module.ChatAgent._raw_turn)
        assert 'system=""' in source
        assert "routing_tier=routing_tier" in source
