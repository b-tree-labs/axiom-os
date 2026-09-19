# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One principal for one human, whatever machine they are on.

Before this, nothing joined. The same person was `@laptop:person` on one node and
`@workstation:person` on another, because the principal was derived from the NODE's
federation `display_name` — which is `node:person`, so `@laptop:person` parses as
name=laptop, context=person under ADR-020's `@name:context`. The person sat in the
context slot, and the federation `owner` field disagreed with itself across
machines (`flast@example.edu` vs `first.last@example.edu`).

The fix follows ADR-020 (handle grammar, normalised there by ADR-084) and the
consistent lesson from Matrix, OIDC and SCIM: key ownership on the identity
provider's stable subject, never on an email or a display name. An email is a
label that changes; an institutional EID is issued once.
"""
from __future__ import annotations

import pytest

from axiom.infra.principal import principal_from_idp_subject


class TestTheMapping:
    def test_an_eid_becomes_a_conformant_handle(self):
        assert (
            principal_from_idp_subject("a1b2c3@idp.example.edu", context="example")
            == "@a1b2c3:example"
        )

    def test_the_same_subject_gives_the_same_handle_on_any_machine(self):
        """The whole point: this is a pure function of the IdP assertion, so
        it cannot vary with which node happens to be running it."""
        first = principal_from_idp_subject("a1b2c3@idp.example.edu", context="example")
        second = principal_from_idp_subject("a1b2c3@idp.example.edu", context="example")
        assert first == second == "@a1b2c3:example"

    def test_the_context_is_switchable(self):
        """ADR-020 makes context switching first-class; the same human has one
        name and many contexts."""
        assert (
            principal_from_idp_subject("a1b2c3@idp.example.edu", context="netl")
            == "@a1b2c3:netl"
        )

    def test_a_bare_subject_without_a_domain_still_works(self):
        assert principal_from_idp_subject("a1b2c3", context="example") == "@a1b2c3:example"

    def test_no_context_omits_the_suffix(self):
        """ADR-020: the `:context` suffix is optional and omitted for the
        principal's home context."""
        assert principal_from_idp_subject("a1b2c3@idp.example.edu") == "@a1b2c3"


class TestItRefusesWhatItCannotMakeSafe:
    def test_an_empty_subject_is_refused_and_says_why(self):
        """Returning something guessable would silently mint an owner.

        The message matters, not just the raise: an empty subject fails the
        grammar check anyway, so a mutant deleting this branch survived a
        bare `pytest.raises`. What the branch is FOR is telling the operator
        "the IdP gave us nothing" instead of "that is not a valid name".
        """
        with pytest.raises(ValueError, match="no local part"):
            principal_from_idp_subject("", context="example")
        with pytest.raises(ValueError, match="no local part"):
            principal_from_idp_subject("@idp.example.edu", context="example")

    @pytest.mark.parametrize("subject", ["has space@x", "has/slash", "a@b@c"])
    def test_a_subject_that_cannot_form_a_handle_is_refused(self, subject):
        """ADR-020's grammar is `[A-Za-z0-9_.-]`. Quietly stripping characters
        would map two different humans onto one owner."""
        with pytest.raises(ValueError):
            principal_from_idp_subject(subject, context="example")

    def test_an_invalid_context_is_refused(self):
        with pytest.raises(ValueError):
            principal_from_idp_subject("a1b2c3@idp.example.edu", context="not a context")


class TestItIsAdr020Conformant:
    def test_the_result_parses_as_a_principal(self):
        from axiom.vega.identity.principal import Principal

        handle = principal_from_idp_subject("a1b2c3@idp.example.edu", context="example")
        p = Principal(handle=handle, public_bytes=b"")
        assert p.name == "a1b2c3"
        assert p.context == "example"

    def test_it_never_produces_the_fediverse_double_at(self):
        handle = principal_from_idp_subject("a1b2c3@idp.example.edu", context="example")
        assert handle.count("@") == 1

    def test_the_person_is_the_name_not_the_context(self):
        """The exact inversion this replaces: `@laptop:person` put the node in
        the name slot and the human in the context slot."""
        handle = principal_from_idp_subject("a1b2c3@idp.example.edu", context="example")
        name, _, context = handle.lstrip("@").partition(":")
        assert name == "a1b2c3", "the human must be the name"
        assert context == "example", "the context must be the org, not the person"
