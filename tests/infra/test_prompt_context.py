# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What a prompt contributor is allowed to know, pinned and read-only.

A contributor is arbitrary installed extension code. Whatever it is handed
becomes a public contract and a disclosure, so the object it receives is a
small named set rather than an internal state object. These tests exist so
that widening the set is a deliberate act somebody has to argue for, and so
that a contributor cannot reach back through the object it was handed and
change the request it is contributing to.

The failure being designed against is concrete: a payload assembled by
passing a whole internal object put credentials into a log, because nobody
could enumerate what was in it. Every field here is enumerable, and the
enumeration is asserted below.
"""

from __future__ import annotations

import dataclasses

import pytest

from axiom.infra.prompt_context import PromptContext

#: The whole field set. Adding a name here is the reviewed act of widening
#: what every installed extension may see about a request.
PINNED_FIELDS = frozenset(
    {
        "principal_id",
        "session_id",
        "interaction_mode",
        "workspace_context",
    }
)

#: Substrings that mark a value as credential-shaped. None may ever name a
#: field: a contributor is not a party to the request's secrets.
CREDENTIAL_WORDS = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "key",
    "auth",
    "cookie",
    "session_key",
    "bearer",
    "signature",
    "private",
)


class TestFieldSetIsPinned:
    """The set is exactly four names, and widening it fails here first."""

    def test_field_set_is_exactly_the_pinned_names(self):
        names = {f.name for f in dataclasses.fields(PromptContext)}
        assert names == set(PINNED_FIELDS), (
            "PromptContext's field set changed. Every field is disclosed to "
            "arbitrary installed extension code and is a permanent public "
            "contract, so widening it belongs in a review, not a diff. Update "
            "PINNED_FIELDS deliberately if the new field is justified."
        )

    def test_every_field_is_a_string(self):
        # Strings are immutable, so no field can be a shared mutable container
        # a contributor could edit in place while the object itself is frozen.
        for f in dataclasses.fields(PromptContext):
            assert f.type in ("str", str), f"{f.name} is {f.type!r}, expected str"

    def test_no_field_name_is_credential_shaped(self):
        for f in dataclasses.fields(PromptContext):
            lowered = f.name.lower()
            for word in CREDENTIAL_WORDS:
                assert word not in lowered, (
                    f"field {f.name!r} looks credential-shaped ({word!r}); "
                    "a prompt contributor is never handed a secret"
                )

    def test_every_field_defaults_to_the_empty_string(self):
        ctx = PromptContext()
        for f in dataclasses.fields(PromptContext):
            assert getattr(ctx, f.name) == ""


class TestReadOnly:
    """A contributor cannot mutate request state through what it was handed."""

    def test_assigning_to_a_field_raises(self):
        ctx = PromptContext(principal_id="@axi:one")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.principal_id = "@axi:two"  # type: ignore[misc]
        assert ctx.principal_id == "@axi:one"

    def test_deleting_a_field_raises(self):
        ctx = PromptContext(session_id="s1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            del ctx.session_id  # type: ignore[misc]
        assert ctx.session_id == "s1"

    def test_attaching_a_new_attribute_raises(self):
        # A contributor must not be able to smuggle state onto the object for
        # the next contributor in the same composition to read.
        ctx = PromptContext()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.smuggled = "payload"  # type: ignore[attr-defined]
        assert not hasattr(ctx, "smuggled")

    def test_every_pinned_field_rejects_assignment(self):
        ctx = PromptContext()
        for f in dataclasses.fields(PromptContext):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(ctx, f.name, "mutated")


class TestValues:
    """Construction keeps what it was given, and instances stay independent."""

    def test_fields_round_trip(self):
        ctx = PromptContext(
            principal_id="@axi:one",
            session_id="sess-1",
            interaction_mode="plan",
            workspace_context="brief one",
        )
        assert ctx.principal_id == "@axi:one"
        assert ctx.session_id == "sess-1"
        assert ctx.interaction_mode == "plan"
        assert ctx.workspace_context == "brief one"

    def test_two_instances_do_not_share_values(self):
        a = PromptContext(principal_id="@axi:one", session_id="sess-1")
        b = PromptContext(principal_id="@axi:two", session_id="sess-2")
        assert a.principal_id == "@axi:one"
        assert b.principal_id == "@axi:two"
        assert a != b
