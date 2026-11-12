# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A flood of wrong secrets for one key must not cost the node its attention.

A key id is not secret to the site that owns it. A token with a real key id and
a wrong secret reaches the memory-hard hash (about 25 ms), and the hash runs on
the thread that serves everyone: measured on an ingest edge, 8 clients sending
wrong secrets for one key took every other caller's latency from 0.5 ms to over
100 ms. A node on the public internet cannot let that be free.

What has to hold:

- after a few wrong secrets for a key inside a short window, more wrong secrets
  for that key are refused without running the hash;
- the key's own holder is not locked out by it: a token already verified keeps
  resolving, through the flood and through an unrelated reload of the file;
- one key's failures do not touch another key;
- when the window passes, a token that has not been verified yet is tried again.
"""

from __future__ import annotations

from pathlib import Path

from axiom.webauth import api_keys
from axiom.webauth.api_keys import (
    TOKEN_PREFIX,
    JsonFileApiKeyStore,
    append_api_key_record,
    mint_api_key,
)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _issue(path: Path, principal):
    token, record = mint_api_key(principal=principal, scopes=("data_platform:invoke",))
    append_api_key_record(path, record)
    return token, record["key_id"]


def _wrong(key_id: str, n: int = 0) -> str:
    return f"{TOKEN_PREFIX}{key_id}_not-the-secret-{n}"


def _counting(monkeypatch):
    calls = []
    real = api_keys.verify_password

    def counted(token, hashed):
        calls.append(token)
        return real(token, hashed)

    monkeypatch.setattr(api_keys, "verify_password", counted)
    return calls


def test_wrong_secrets_stop_reaching_the_hash_after_a_few(tmp_path, monkeypatch):
    f = tmp_path / "keys.json"
    _token, kid = _issue(f, "@svc:site-a")
    clock = _Clock()
    store = JsonFileApiKeyStore(f, clock=clock)
    calls = _counting(monkeypatch)
    for i in range(50):
        assert store.resolve(_wrong(kid, i)) is None
    assert 1 <= len(calls) <= store.failures_allowed


def test_the_holder_is_not_locked_out_by_the_flood(tmp_path, monkeypatch):
    f = tmp_path / "keys.json"
    token, kid = _issue(f, "@svc:site-a")
    clock = _Clock()
    store = JsonFileApiKeyStore(f, clock=clock)
    assert store.resolve(token) is not None  # warmed: this holder has been verified
    for i in range(50):
        store.resolve(_wrong(kid, i))
    assert store.resolve(token) is not None
    # and an unrelated change to the file (another key issued) does not cool it
    _issue(f, "@svc:site-b")
    calls = _counting(monkeypatch)
    assert store.resolve(token) is not None
    assert calls == []


def test_a_change_to_the_keys_own_record_does_cool_it(tmp_path, monkeypatch):
    from axiom.webauth.api_keys import revoke_api_key_record

    f = tmp_path / "keys.json"
    token, kid = _issue(f, "@svc:site-a")
    store = JsonFileApiKeyStore(f, clock=_Clock())
    assert store.resolve(token) is not None
    revoke_api_key_record(f, kid)
    assert store.resolve(token) is None


def test_one_keys_failures_do_not_touch_another(tmp_path, monkeypatch):
    f = tmp_path / "keys.json"
    _ta, ka = _issue(f, "@svc:site-a")
    tb, kb = _issue(f, "@svc:site-b")
    store = JsonFileApiKeyStore(f, clock=_Clock())
    for i in range(50):
        store.resolve(_wrong(ka, i))
    assert store.resolve(tb) is not None  # cold, never verified, a different key


def test_after_the_window_a_cold_token_is_tried_again(tmp_path):
    f = tmp_path / "keys.json"
    token, kid = _issue(f, "@svc:site-a")
    clock = _Clock()
    store = JsonFileApiKeyStore(f, clock=clock)
    for i in range(50):
        store.resolve(_wrong(kid, i))
    assert store.resolve(token) is None  # the budget is spent: refused cheaply
    clock.now += store.failure_window_s + 1
    assert store.resolve(token) is not None
