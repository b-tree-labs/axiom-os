# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Binding an actor from a handle, without reaching for a private name.

``set_current_actor`` accepts any object; ``get_current_actor`` returns it only
if it is a ``Principal``, and steps silently past anything else.

A caller holding a ``PrincipalContext`` — which every skill invocation has, and
which carries a handle — binds it and gets one of two wrong answers with no
error either way. With nothing ambient, ``AuthnUnavailable``, which callers
swallow into "unattributed". With an ``AXIOM_ACTOR`` set, the *ambient*
identity, which puts a wrong name on the record rather than no name.

That happened, in the LangGraph shim, and the private name is why: the
conversion existed and was not reachable.
"""

from __future__ import annotations

import pytest

from axiom.governance import (
    AuthnUnavailable,
    get_current_actor,
    principal_from_handle,
    set_current_actor,
)


@pytest.fixture(autouse=True)
def _clear_actor(monkeypatch):
    monkeypatch.delenv("AXIOM_ACTOR", raising=False)
    set_current_actor(None)
    yield
    set_current_actor(None)


class TestItBuildsSomethingTheReaderAccepts:
    def test_a_handle_becomes_a_bindable_principal(self):
        set_current_actor(principal_from_handle("@ben:ut"))
        assert get_current_actor().handle == "@ben:ut"

    def test_a_bare_name_gains_the_at(self):
        assert principal_from_handle("ben:ut").handle == "@ben:ut"

    def test_it_is_deterministic(self):
        """Two calls for one handle must not produce two identities."""
        assert (
            principal_from_handle("@ben:ut").public_bytes
            == principal_from_handle("@ben:ut").public_bytes
        )

    def test_different_handles_differ(self):
        assert (
            principal_from_handle("@ben:ut").public_bytes
            != principal_from_handle("@nima:netl").public_bytes
        )


class TestWhyThisIsPublic:
    def test_binding_a_principal_context_reads_back_nothing(self):
        """The failure the public helper exists to prevent.

        A ``PrincipalContext`` has a handle and is not a ``Principal``.
        """
        from axiom.infra.principal import PrincipalContext

        set_current_actor(PrincipalContext(handle="@ben:ut"))

        with pytest.raises(AuthnUnavailable):
            get_current_actor()

    def test_with_an_ambient_actor_it_misattributes_instead(self, monkeypatch):
        """The worse half, and not what I first assumed.

        A non-Principal does not defeat the ``AXIOM_ACTOR`` fallback — the
        resolver simply steps past it. So the outcome depends on the
        environment: with nothing ambient the call is *unattributed*, and with
        something ambient it is attributed to *that*, silently, which is a
        wrong name on the record rather than a missing one.
        """
        monkeypatch.setenv("AXIOM_ACTOR", "@fallback:host")
        from axiom.infra.principal import PrincipalContext

        set_current_actor(PrincipalContext(handle="@ben:ut"))

        assert get_current_actor().handle == "@fallback:host"

    def test_converting_first_fixes_both(self, monkeypatch):
        monkeypatch.setenv("AXIOM_ACTOR", "@fallback:host")
        from axiom.infra.principal import PrincipalContext

        context = PrincipalContext(handle="@ben:ut")
        set_current_actor(principal_from_handle(context.handle))

        assert get_current_actor().handle == "@ben:ut"
