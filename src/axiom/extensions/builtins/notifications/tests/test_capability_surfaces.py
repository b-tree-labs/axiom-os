# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which surfaces each notifications capability reaches, and why.

Every capability here was registered bare — no `SkillSpec`, no `surfaces` — so
all of them were CLI-only by OMISSION rather than by decision. The monitor case
is precisely "an agent notices something and tells a human", and the agent
could not reach the verb that does it. Found by the session working on the
approval gate; verified here independently.

The restricted three are the point of this file. The failure it guards is a
one-word edit that looks like an improvement.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications import skills as notifications
from axiom.infra.skills import SkillRegistry


@pytest.fixture
def registry() -> SkillRegistry:
    r = SkillRegistry()
    notifications.bind(r)
    return r


#: Verbs an agent must NEVER reach, each for its own reason.
RESTRICTED = {
    # Acknowledging means a PERSON took responsibility. If an agent can ack,
    # the record "somebody picked this up" becomes false — and that record is
    # the only reason the column is worth having. The harm is not a side
    # effect to be gated; it is the meaning of the data being destroyed.
    "notifications.ack",
    # Writes credentials. An agent that can change how a human is reached can
    # change WHO is reached.
    "notifications.setup",
    # Writes routing. Same class: identity-adjacent.
    "notifications.recipient_set",
}


class TestEveryCapabilityDeclaresItself:
    def test_none_are_undeclared(self, registry):
        undeclared = [n for n in registry.list("notifications") if registry.spec(n) is None]
        assert undeclared == [], f"CLI-only by omission: {undeclared}"

    def test_every_capability_names_its_surfaces(self, registry):
        missing = [
            n for n in registry.list("notifications")
            if not (registry.spec(n) and registry.spec(n).surfaces)
        ]
        assert missing == []

    def test_every_capability_declares_its_side_effects(self, registry):
        """Undeclared means the projector must assume WRITE. Declaring is what
        puts a verb behind the confirm gate on every surface at once."""
        undeclared = [
            n for n in registry.list("notifications")
            if registry.spec(n) and registry.spec(n).side_effects is None
        ]
        assert undeclared == []

    def test_agent_facing_capabilities_document_their_inputs(self, registry):
        """A projected capability with no inputs gives a model an untyped
        blob — reachable but unusable."""
        for name in registry.list("notifications"):
            spec = registry.spec(name)
            if not spec or "agent_tool" not in (spec.surfaces or ()):
                continue
            if name.endswith(".channels") or name.endswith(".recipient_list"):
                continue  # genuinely take no parameters
            assert spec.inputs, f"{name} is agent-reachable with no declared inputs"


class TestTheRestrictedVerbsStayRestricted:
    @pytest.mark.parametrize("name", sorted(RESTRICTED))
    def test_never_reachable_by_an_agent(self, registry, name):
        surfaces = registry.spec(name).surfaces or ()
        assert "agent_tool" not in surfaces, name
        assert "mcp" not in surfaces, name

    @pytest.mark.parametrize("name", sorted(RESTRICTED))
    def test_still_reachable_by_a_person(self, registry, name):
        """Restricted is not removed. A human must still be able to do these,
        or the restriction has broken the product instead of protecting it."""
        assert "cli" in (registry.spec(name).surfaces or ()), name


class TestTheMonitorCaseWorks:
    """The reason this was worth fixing."""

    def test_an_agent_can_tell_a_human_something_happened(self, registry):
        spec = registry.spec("notifications.alert")
        assert "agent_tool" in (spec.surfaces or ())
        assert spec.side_effects is True, "declared, so the confirm gate applies"

    def test_an_agent_can_read_what_is_waiting(self, registry):
        assert "agent_tool" in (registry.spec("notifications.list").surfaces or ())

    def test_but_it_cannot_close_the_loop_on_a_persons_behalf(self, registry):
        """The asymmetry is deliberate: an agent may raise and may read, but
        only a person may say they have taken it up."""
        assert "agent_tool" not in (registry.spec("notifications.ack").surfaces or ())
