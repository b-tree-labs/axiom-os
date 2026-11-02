# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A service principal is made to forget what it may no longer reach.

Memory is scoped per principal, and ownership is the base case in
``access.is_visible`` — checked BEFORE the access graph. That is right for a
person: you do not un-learn a site by losing a login, and a human's
recollection is their own.

It is wrong for a service principal. A service account is not a person with
memories; its knowledge is an extension of its access, and it is typically
privileged enough to stay connected to the system it knows. Leaving its
fragments in place after access is withdrawn leaves a live, privileged actor
holding what it learned, which visibility filtering cannot fix precisely
because ownership outranks the graph.

So withdrawal has to DELETE rather than hide, which is what tombstones are
for — and they propagate, so a peer drops its copy too.
"""

from __future__ import annotations

import pytest

from axiom.memory.fragment import CognitiveType, MemoryFragment, Provenance
from axiom.memory.lifecycle import (
    forget_resource,
    is_service_principal,
)

HUMAN = "@alice:ut"
SERVICE = "agent:axi"
NODE = "node://site-a"
SENNA, PROST = "site-b", "site-c"


def _frag(principal, resource, body="note"):
    return MemoryFragment(
        id=f"{principal}-{resource}-{body}",
        cognitive_type=CognitiveType.EPISODIC,
        content={"text": body},
        provenance=Provenance(
            timestamp="2026-09-23T00:00:00Z",
            principal_id=principal,
            agents=frozenset({SERVICE}),
            resources=frozenset({resource}),
            accountable_human_id=HUMAN,
        ),
    )


class TestTellingTheTwoApart:
    @pytest.mark.parametrize("handle", ["agent:axi", "service:ingest", "bot:scan"])
    def test_a_non_person_handle_is_a_service_principal(self, handle):
        assert is_service_principal(handle)

    @pytest.mark.parametrize("handle", ["@alice:ut", "@bob:senna", "@c:d"])
    def test_an_at_handle_is_a_person(self, handle):
        assert not is_service_principal(handle)

    def test_an_empty_handle_is_treated_as_a_service_principal(self):
        """Fail closed. An unattributed fragment is not evidence of a human,
        and forgetting too much is recoverable while forgetting too little
        is the thing we are trying to prevent."""
        assert is_service_principal("")


class TestAServicePrincipalForgets:
    def test_its_fragments_about_the_lost_resource_are_tombstoned(self):
        frags = [_frag(SERVICE, PROST), _frag(SERVICE, SENNA)]
        stones = forget_resource(frags, resource=PROST, signer_node=NODE)
        assert [s.fragment_id for s in stones] == [frags[0].id]

    def test_the_reason_names_the_resource_so_a_peer_can_audit_it(self):
        stones = forget_resource([_frag(SERVICE, PROST)], resource=PROST, signer_node=NODE)
        assert PROST in stones[0].reason

    def test_the_tombstone_carries_the_signing_node(self):
        stones = forget_resource([_frag(SERVICE, PROST)], resource=PROST, signer_node=NODE)
        assert stones[0].signer_node == NODE

    def test_ownership_does_not_rescue_it(self):
        """The whole point. `is_visible` would let this service principal go
        on seeing its own fragment, because ownership is checked before the
        graph. Forgetting is a deletion, not a filter."""
        own = _frag(SERVICE, PROST)
        assert own.provenance.principal_id == SERVICE
        assert forget_resource([own], resource=PROST, signer_node=NODE)


class TestAPersonKeepsTheirRecollection:
    def test_a_human_fragment_is_not_tombstoned(self):
        stones = forget_resource([_frag(HUMAN, PROST)], resource=PROST, signer_node=NODE)
        assert stones == []

    def test_a_mixed_set_forgets_only_the_service_half(self):
        mine, theirs = _frag(HUMAN, PROST), _frag(SERVICE, PROST)
        stones = forget_resource([mine, theirs], resource=PROST, signer_node=NODE)
        assert [s.fragment_id for s in stones] == [theirs.id]

    def test_the_override_exists_for_a_deliberate_human_redaction(self):
        """Separate flag, so forgetting a person's memory is never something
        that happens as a side effect of revoking a machine's access."""
        stones = forget_resource(
            [_frag(HUMAN, PROST)], resource=PROST, signer_node=NODE,
            include_humans=True,
        )
        assert len(stones) == 1


class TestWhatItDoesNotTouch:
    def test_a_fragment_about_another_resource_survives(self):
        assert forget_resource([_frag(SERVICE, SENNA)], resource=PROST, signer_node=NODE) == []

    def test_a_fragment_naming_several_resources_is_forgotten_if_one_matches(self):
        """A fragment that mentions two sites cannot be partially forgotten,
        so it goes. Keeping it would keep the withdrawn site's content."""
        f = _frag(SERVICE, SENNA)
        f.provenance.resources  # frozenset({SENNA})
        multi = MemoryFragment(
            id="multi",
            cognitive_type=CognitiveType.EPISODIC,
            content={"text": "both"},
            provenance=Provenance(
                timestamp="2026-09-23T00:00:00Z",
                principal_id=SERVICE,
                agents=frozenset({SERVICE}),
                resources=frozenset({SENNA, PROST}),
                accountable_human_id=HUMAN,
            ),
        )
        stones = forget_resource([multi], resource=PROST, signer_node=NODE)
        assert [s.fragment_id for s in stones] == ["multi"]

    def test_an_empty_resource_forgets_nothing(self):
        """Guard against a caller passing "" and erasing a service
        principal's entire memory by accident."""
        with pytest.raises(ValueError):
            forget_resource([_frag(SERVICE, PROST)], resource="", signer_node=NODE)
