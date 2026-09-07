# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SSO-bound accounts in the accounts file: no password, an IdP binding, and a
store that can write them back (just-in-time provisioning + account linking)."""

from __future__ import annotations

import json

import pytest

from axiom.webauth import (
    AccountsFileError,
    JsonFileUserStore,
    User,
    WritableUserStore,
    authenticate,
    get_password_hash,
    load_user_records,
    upsert_user_record,
)
from axiom.webauth.users import InMemoryUserStore


def test_sso_bound_record_may_omit_password(tmp_path):
    p = tmp_path / "accounts.json"
    p.write_text(
        json.dumps(
            [
                {
                    "user_id": "oid-1",
                    "email": "a@example.edu",
                    "password_hash": None,
                    "roles": ["student"],
                    "attributes": {"idp": "entra", "idp_subject": "oid-1"},
                }
            ]
        )
    )
    [rec] = load_user_records(p)
    assert rec["password_hash"] is None
    assert rec["attributes"]["idp"] == "entra"
    store = JsonFileUserStore(p)
    user = store.get_by_id("oid-1")
    assert user.email == "a@example.edu" and user.password_hash is None
    # No password can ever pass the form for this account.
    assert authenticate(store, "a@example.edu", "") is None
    assert authenticate(store, "a@example.edu", "anything") is None


def test_null_password_without_idp_binding_is_refused(tmp_path):
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps([{"email": "a@example.edu", "password_hash": None, "roles": []}]))
    with pytest.raises(AccountsFileError, match="password_hash"):
        load_user_records(p)


def test_attributes_must_be_an_object(tmp_path):
    p = tmp_path / "accounts.json"
    p.write_text(
        json.dumps(
            [{"email": "a@example.edu", "password_hash": "scrypt$x", "attributes": "idp=entra"}]
        )
    )
    with pytest.raises(AccountsFileError, match="attributes"):
        load_user_records(p)


def test_json_file_store_is_writable_and_round_trips_sso_account(tmp_path):
    p = tmp_path / "accounts.json"
    store = JsonFileUserStore(p)
    assert isinstance(store, WritableUserStore)
    written = store.upsert(
        User(
            user_id="oid-1",
            email="A@Example.edu",
            name="Alice",
            roles=("student",),
            attributes={"idp": "entra"},
        )
    )
    assert written.user_id == "oid-1" and written.email == "a@example.edu"
    # Persisted, mode 0600, re-readable by a fresh store.
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    again = JsonFileUserStore(p).get_by_id("oid-1")
    assert again.roles == ("student",) and again.attributes["idp"] == "entra"
    assert again.password_hash is None


def test_upsert_matches_by_user_id_then_email_and_keeps_password(tmp_path):
    p = tmp_path / "accounts.json"
    pw = get_password_hash("pw-000000000000000")
    upsert_user_record(
        p, email="a@example.edu", password_hash=pw, roles=["operator"], user_id="alice-local"
    )
    store = JsonFileUserStore(p)
    # Link by email: id is preserved, hash is preserved, binding is added.
    linked = store.upsert(
        User(
            user_id="alice-local",
            email="a@example.edu",
            password_hash=pw,
            name="Alice",
            roles=("operator",),
            attributes={"idp": "entra", "idp_subject": "oid-1"},
        )
    )
    assert len(store) == 1
    assert linked.password_hash == pw
    assert authenticate(store, "a@example.edu", "pw-000000000000000") is not None
    # An email change on a subject-keyed account replaces, never duplicates.
    store.upsert(
        User(
            user_id="alice-local",
            email="alice.new@example.edu",
            password_hash=pw,
            attributes={"idp": "entra"},
        )
    )
    assert len(store) == 1
    assert store.get_by_email("a@example.edu") is None
    assert store.get_by_email("alice.new@example.edu").user_id == "alice-local"


def test_upsert_user_record_keeps_prior_hash_and_attributes_when_none(tmp_path):
    p = tmp_path / "accounts.json"
    pw = get_password_hash("pw-000000000000000")
    upsert_user_record(p, email="a@example.edu", password_hash=pw, attributes={"idp": "entra"})
    out = upsert_user_record(
        p, email="a@example.edu", password_hash=None, roles=["admin"], overwrite=True
    )
    assert out["roles"] == ["admin"]
    [rec] = load_user_records(p)
    assert rec["password_hash"] == pw
    assert rec["attributes"] == {"idp": "entra"}


def test_store_without_file_refuses_to_write():
    with pytest.raises(AccountsFileError):
        JsonFileUserStore(None).upsert(
            User(user_id="x", email="x@y.org", attributes={"idp": "entra"})
        )


def test_in_memory_upsert_drops_stale_email_key():
    store = InMemoryUserStore([User(user_id="u1", email="old@x.org")])
    store.upsert(User(user_id="u1", email="new@x.org"))
    assert store.get_by_email("old@x.org") is None
    assert store.get_by_email("new@x.org").user_id == "u1"
    assert len(store) == 1
