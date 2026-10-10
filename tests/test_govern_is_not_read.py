# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Governing is a different act from reading — ADR-151 §1, §2, §3.

Two bundles shipped: `admin = ("*",)` and `viewer = ("*:read",)`. A scope's
verb came from the HTTP method, so `*` fused read, write and govern into one
grant and an administrator who configures a node without reading what it
holds could not be expressed. The honest grant did not exist, so the
dishonest one got issued.

Three things are asserted here, each from the ADR:

- **§2** `govern` is a verb in the scope grammar, and it is *not* derivable
  from the HTTP method: POST is `invoke` whether it writes a document or
  grants a role. A mount declares which of its routes govern, and until it
  does its governing routes are indistinguishable from its writing ones.
- **§1** six roles, and `admin` reads nothing.
- **§3** the floor is an allowlist: a `guest` bundle containing `*` is
  refused, so a public tier can never be written as a subtraction.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http.authz_hook import parse_scope
from axiom.governance.intent import ActionIntent
from axiom.extensions.builtins.webgate.role_bundles import (
    BundleRegistry,
    ScopeBundle,
    default_bundle_registry,
)


# ------------------------------------------------------------------ §2 grammar

def test_govern_is_a_verb_the_grammar_accepts():
    intent, resource = parse_scope("agreements:govern")
    assert intent.value == "http.govern"
    assert resource.value == "extension://agreements/*"


def test_a_governing_scope_does_not_cover_reading():
    """The point of the verb. `agreements:govern` admits somebody to an
    agreement and does not show them what the agreement unlocks."""
    govern, _ = parse_scope("agreements:govern")
    assert not govern.matches(ActionIntent("http.read"))
    assert not govern.matches(ActionIntent("http.invoke"))


def test_a_reading_scope_does_not_cover_governing():
    read, _ = parse_scope("agreements:read")
    assert not read.matches(ActionIntent("http.govern"))


def test_a_star_scope_still_covers_everything_including_governing():
    """`*` keeps its meaning, which is why `owner` holds it and nothing else
    does."""
    intent, _ = parse_scope("agreements")
    for act in ("http.read", "http.invoke", "http.govern"):
        assert intent.matches(ActionIntent(act)), act


def test_a_misspelt_verb_is_refused_rather_than_widened():
    with pytest.raises(ValueError) as refused:
        parse_scope("agreements:governs")
    assert "govern" in str(refused.value)


# -------------------------------------------------------------- §1 the roles

EXPECTED = {
    "owner": "reads, writes and governs, and appoints administrators",
    "admin": "governs and reads nothing",
    "operator": "reads, writes and governs",
    "creator": "reads and writes, grants nothing",
    "viewer": "reads everything, changes nothing",
    "guest": "only what has been published",
}


def test_every_role_in_the_adr_is_registered():
    roles = set(default_bundle_registry().roles())
    assert set(EXPECTED) <= roles, f"missing: {sorted(set(EXPECTED) - roles)}"


def test_an_administrator_reads_nothing():
    """The row the whole ADR exists for. An administrator who needs to read
    asks for `operator`, and the ask is visible."""
    admin = default_bundle_registry().get("admin")
    assert admin is not None
    assert admin.scopes == ("*:govern",), admin.scopes


def test_a_creator_builds_but_grants_nothing():
    creator = default_bundle_registry().get("creator")
    assert creator is not None
    assert "*:govern" not in creator.scopes
    assert "*" not in creator.scopes
    assert set(creator.scopes) == {"*:read", "*:invoke"}, creator.scopes


def test_only_the_owner_holds_the_unrestricted_grant():
    registry = default_bundle_registry()
    holders = [r for r in registry.roles() if "*" in (registry.get(r).scopes or ())]
    assert holders == ["owner"], holders


def test_an_operator_governs_and_reads_where_an_administrator_does_not():
    operator = default_bundle_registry().get("operator")
    assert operator is not None
    assert "*:govern" in operator.scopes
    assert "*:read" in operator.scopes


# --------------------------------------------------------------- §3 the floor

def test_the_floor_names_what_is_published_rather_than_subtracting():
    guest = default_bundle_registry().get("guest")
    assert guest is not None
    assert "*" not in guest.scopes
    assert "*:read" not in guest.scopes


def test_a_guest_bundle_containing_a_wildcard_is_refused():
    """A public tier written as a subtraction is wrong the moment somebody
    mounts something new, and wrong silently. The registry refuses it rather
    than trusting whoever writes the next bundle."""
    registry = BundleRegistry()
    with pytest.raises(ValueError) as refused:
        registry.register(ScopeBundle(role="guest", scopes=("*:read",), description="no"))
    assert "guest" in str(refused.value).lower()


def test_a_guest_bundle_of_named_surfaces_is_accepted():
    registry = BundleRegistry()
    registry.register(
        ScopeBundle(role="guest", scopes=("status:read", "about:read"), description="ok")
    )
    assert registry.get("guest") is not None
