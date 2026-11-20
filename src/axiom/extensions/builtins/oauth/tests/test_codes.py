# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The authorization-code store — short-lived, single-use, PKCE-bearing codes."""

from __future__ import annotations

from axiom.extensions.builtins.oauth.codes import InMemoryAuthorizationCodeStore
from axiom.extensions.builtins.oauth.pkce import S256


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _issue(store):
    return store.issue(
        client_id="app",
        redirect_uri="https://app.example/cb",
        subject="user:alice",
        scope="memory.read",
        code_challenge="chal",
        code_challenge_method=S256,
        resource="https://api.example",
    )


def test_issue_populates_and_returns_a_code():
    store = InMemoryAuthorizationCodeStore()
    rec = _issue(store)
    assert rec.code
    assert rec.subject == "user:alice"
    assert rec.redirect_uri == "https://app.example/cb"
    assert rec.code_challenge == "chal"
    assert rec.resource == "https://api.example"


def test_code_is_single_use():
    store = InMemoryAuthorizationCodeStore()
    rec = _issue(store)
    assert store.consume(rec.code) is not None
    assert store.consume(rec.code) is None  # replay rejected


def test_expired_code_is_rejected_and_removed():
    clock = _Clock()
    store = InMemoryAuthorizationCodeStore(ttl_seconds=60, now=clock)
    rec = _issue(store)
    clock.t += 61
    assert store.consume(rec.code) is None


def test_unknown_code_is_none():
    store = InMemoryAuthorizationCodeStore()
    assert store.consume("nope") is None


def test_issued_codes_are_unique():
    store = InMemoryAuthorizationCodeStore()
    codes = {_issue(store).code for _ in range(50)}
    assert len(codes) == 50
