# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which principal owns what this human writes — and what it inherits.

The canonical handle comes from the identity provider (ADR-020 grammar,
`@a1b2c3:example`). Everything written before it exists is owned by a
node-derived legacy handle — `@laptop:person` here, `@workstation:person` on the next
machine — because the principal used to be built from the node's federation
`display_name`.

Those are adopted on read, the same way an unowned session is. The guard is
that a legacy handle is only adoptable if THIS NODE minted it: adopting any
handle that happens to be legacy-shaped would let one person claim another's
fragments by writing the right string.
"""
from __future__ import annotations

from axiom.infra.principal import canonical_principal, legacy_principals


class TestTheCanonicalPrincipal:
    def test_a_configured_idp_subject_wins(self, monkeypatch):
        monkeypatch.setenv("AXIOM_IDP_SUBJECT", "a1b2c3@idp.example.edu")
        monkeypatch.setenv("AXIOM_IDENTITY_CONTEXT", "example")
        assert canonical_principal() == "@a1b2c3:example"

    def test_it_is_the_same_on_every_machine(self, monkeypatch):
        """The point of the exercise: a pure function of the assertion, so it
        cannot vary with which node runs it."""
        monkeypatch.setenv("AXIOM_IDP_SUBJECT", "a1b2c3@idp.example.edu")
        monkeypatch.setenv("AXIOM_IDENTITY_CONTEXT", "example")
        first = canonical_principal()
        monkeypatch.setenv("AXIOM_NODE_NAME", "some-other-node")
        assert canonical_principal() == first

    def test_without_an_idp_it_falls_back_rather_than_failing(self, monkeypatch):
        """Chat must keep working on a laptop with no IdP wired."""
        monkeypatch.delenv("AXIOM_IDP_SUBJECT", raising=False)
        assert canonical_principal(legacy="@laptop:person") == "@laptop:person"

    def test_a_malformed_subject_falls_back_instead_of_raising(self, monkeypatch):
        """A broken IdP claim must not take chat down; it degrades to the
        legacy handle and the person plane keeps reporting `open`."""
        monkeypatch.setenv("AXIOM_IDP_SUBJECT", "not a subject")
        assert canonical_principal(legacy="@laptop:person") == "@laptop:person"


class TestAdoption:
    def test_this_node_s_legacy_handle_is_adoptable(self):
        assert "@laptop:person" in legacy_principals(display_name="laptop:person")

    def test_the_handle_is_offered_in_both_orders(self):
        """The node minted `@laptop:person` from `laptop:person`. The inverted read
        — `@person:laptop` — is what the convention SHOULD have produced, and
        some records may carry it, so both are adoptable from this node."""
        found = legacy_principals(display_name="laptop:person")
        assert "@laptop:person" in found and "@person:laptop" in found

    def test_another_node_s_handle_is_not_adoptable(self):
        """Adopting any legacy-shaped handle would let one person claim
        another's fragments by writing the right string."""
        assert "@workstation:person" not in legacy_principals(display_name="laptop:person")

    def test_no_display_name_adopts_nothing(self):
        assert legacy_principals(display_name="") == []

    def test_the_canonical_handle_is_never_listed_as_legacy(self):
        """It is the destination, not a source; listing it would make the
        adoption check trivially true."""
        found = legacy_principals(display_name="laptop:person")
        assert "@a1b2c3:example" not in found
