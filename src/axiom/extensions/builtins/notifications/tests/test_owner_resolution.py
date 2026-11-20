# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for owner-nickname resolution (ADR-066 PR-2).

The possessive owner in "Ben's RIVET 0.6.0" resolves from the *inherent*
node identity — federation is a given, not a ceremony. Precedence, one
possessive render form across all tiers (Ben, 2026-06-04):

1. remote peer's agent (peers exist) → peer registry display_name
2. local / own context              → settings ``user.name``
3. else                            → birth-host (auto, persisted, never empty)
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.owner_resolution import (
    resolve_owner_display,
)


class _Settings:
    def __init__(self, values: dict[str, str]):
        self._v = values

    def get(self, key: str, default=None):
        return self._v.get(key, default)


class _Peer:
    def __init__(self, display_name: str):
        self.display_name = display_name


class _PeerRegistry:
    """Maps a context label → a KnownNode-like with display_name."""

    def __init__(self, by_context: dict[str, _Peer]):
        self._by = by_context

    def for_context(self, context: str):
        return self._by.get(context)


# --- tier 2: user.name is the canonical local source ---------------------- #
def test_local_owner_from_user_name():
    settings = _Settings({"user.name": "Ben"})
    got = resolve_owner_display(
        "bens", local_context="bens", settings=settings, peers=None, host="ben-mbp"
    )
    assert got == "Ben"


# --- tier 1: remote peer wins for a non-local context --------------------- #
def test_remote_owner_from_peer_registry():
    settings = _Settings({"user.name": "Ben"})
    peers = _PeerRegistry({"alices": _Peer("Alice")})
    got = resolve_owner_display(
        "alices", local_context="bens", settings=settings, peers=peers, host="ben-mbp"
    )
    assert got == "Alice"


# --- tier 3: birth-host when no user.name and no peer --------------------- #
def test_birth_host_fallback_when_unnamed_and_solo():
    settings = _Settings({"user.name": ""})
    got = resolve_owner_display(
        "bens", local_context="bens", settings=settings, peers=None, host="ben-mbp"
    )
    assert got == "ben-mbp"


# --- precedence: user.name beats host for the local context --------------- #
def test_user_name_beats_host():
    settings = _Settings({"user.name": "Ben"})
    got = resolve_owner_display(
        "bens", local_context="bens", settings=settings, peers=None, host="ben-mbp"
    )
    assert got == "Ben"


# --- precedence: peer beats user.name for a remote context ---------------- #
def test_peer_beats_user_name_for_remote():
    settings = _Settings({"user.name": "Ben"})
    peers = _PeerRegistry({"alices": _Peer("Alice")})
    got = resolve_owner_display(
        "alices", local_context="bens", settings=settings, peers=peers, host="ben-mbp"
    )
    assert got == "Alice"


# --- a remote context with no peer entry falls to host (never empty) ------ #
def test_unknown_remote_context_falls_to_host():
    settings = _Settings({"user.name": ""})
    peers = _PeerRegistry({})
    got = resolve_owner_display(
        "ghost", local_context="bens", settings=settings, peers=peers, host="ben-mbp"
    )
    assert got == "ben-mbp"


# --- never empty: blank user.name + blank host still yields something ----- #
def test_never_empty():
    settings = _Settings({"user.name": ""})
    got = resolve_owner_display(
        "bens", local_context="bens", settings=settings, peers=None, host=""
    )
    assert got  # non-empty fallback (host helper guarantees a value)
