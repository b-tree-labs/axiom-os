# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rotating refresh-token store with reuse detection (ADR-085)."""

from __future__ import annotations

from axiom.extensions.builtins.oauth.refresh import InMemoryRefreshTokenStore


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _issue(store, **overrides):
    fields = {
        "client_id": "spa",
        "subject": "user:alice",
        "scope": "memory.read offline_access",
        "resource": "https://api.example",
        **overrides,
    }
    return store.issue(**fields)


def test_issue_then_rotate_returns_record_once():
    store = InMemoryRefreshTokenStore()
    rt = _issue(store)
    rec = store.rotate(rt.token)
    assert rec is not None
    assert rec.subject == "user:alice"


def test_rotation_keeps_the_family():
    store = InMemoryRefreshTokenStore()
    rt = _issue(store)
    rec = store.rotate(rt.token)
    nxt = _issue(store, family_id=rec.family_id)
    assert nxt.family_id == rt.family_id


def test_replay_of_rotated_token_is_rejected_and_revokes_family():
    store = InMemoryRefreshTokenStore()
    rt = _issue(store)
    rec = store.rotate(rt.token)            # legit rotation, rt now consumed
    nxt = _issue(store, family_id=rec.family_id)
    # Attacker replays the already-rotated original token.
    assert store.rotate(rt.token) is None
    # Reuse detected -> the whole family is revoked, including the fresh token.
    assert store.rotate(nxt.token) is None


def test_unknown_token_is_none():
    store = InMemoryRefreshTokenStore()
    assert store.rotate("nope") is None


def test_expired_token_is_none():
    clock = _Clock()
    store = InMemoryRefreshTokenStore(ttl_seconds=100, now=clock)
    rt = _issue(store)
    clock.t += 101
    assert store.rotate(rt.token) is None


def test_tokens_are_unique():
    store = InMemoryRefreshTokenStore()
    tokens = {_issue(store).token for _ in range(50)}
    assert len(tokens) == 50
