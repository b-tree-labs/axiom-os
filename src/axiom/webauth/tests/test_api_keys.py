# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for :mod:`axiom.webauth.api_keys` — non-human API-principal keys.

The store parallels :mod:`axiom.webauth.file_store` (fail-closed validation,
atomic writes, mtime hot-reload) but holds *bearer API keys* bound to service
principals (``@name:context``) with scopes, instead of password accounts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.webauth.api_keys import (
    ApiKeysFileError,
    JsonFileApiKeyStore,
    TOKEN_PREFIX,
    append_api_key_record,
    load_api_key_records,
    mint_api_key,
    parse_token,
    revoke_api_key_record,
)


def _keys(tmp_path: Path) -> Path:
    return tmp_path / "gate-api-keys.json"


def _issue(path: Path, principal="@svc:org", scopes=("llm",), name="svc key"):
    token, record = mint_api_key(principal=principal, scopes=scopes, name=name)
    append_api_key_record(path, record)
    return token, record


# ---------- minting --------------------------------------------------------


def test_mint_returns_token_and_hashed_record():
    token, record = mint_api_key(principal="@svc:org", scopes=("llm",), name="x")
    assert token.startswith(TOKEN_PREFIX)
    # the id is auto-generated, embedded in the token, echoed in the record
    key_id, secret = parse_token(token)
    assert record["key_id"] == key_id
    assert secret and secret not in record.values()
    # hashed at rest with webauth's existing scheme (self-describing scrypt)
    assert record["secret_hash"].startswith("scrypt$")
    assert token not in json.dumps(record)
    assert record["principal"] == "@svc:org"
    assert list(record["scopes"]) == ["llm"]


def test_mint_two_keys_get_distinct_ids_and_secrets():
    t1, r1 = mint_api_key(principal="@svc:org", scopes=("llm",))
    t2, r2 = mint_api_key(principal="@svc:org", scopes=("llm",))
    assert t1 != t2
    assert r1["key_id"] != r2["key_id"]


def test_mint_rejects_bad_principal_handle():
    with pytest.raises(ValueError):
        mint_api_key(principal="svc@org", scopes=("llm",))
    with pytest.raises(ValueError):
        mint_api_key(principal="@a@b", scopes=("llm",))


def test_mint_rejects_empty_scopes():
    with pytest.raises(ValueError):
        mint_api_key(principal="@svc:org", scopes=())


def test_parse_token_shapes():
    assert parse_token("not-a-key") is None
    assert parse_token(f"{TOKEN_PREFIX}onlyid") is None
    kid, secret = parse_token(f"{TOKEN_PREFIX}abc123_s3cr_et")
    assert kid == "abc123"
    assert secret == "s3cr_et"  # secret may itself contain separators


# ---------- file validation (fail-closed) ----------------------------------


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(ApiKeysFileError):
        load_api_key_records(_keys(tmp_path))


def test_load_rejects_plaintext_secret(tmp_path: Path):
    f = _keys(tmp_path)
    f.write_text(json.dumps([{
        "key_id": "k1", "principal": "@svc:org", "secret": "oops",
        "secret_hash": "scrypt$x", "scopes": ["llm"],
    }]))
    with pytest.raises(ApiKeysFileError, match="plaintext"):
        load_api_key_records(f)


def test_load_rejects_unknown_fields_and_duplicates(tmp_path: Path):
    f = _keys(tmp_path)
    f.write_text(json.dumps([{
        "key_id": "k1", "principal": "@svc:org",
        "secret_hash": "scrypt$x", "scopes": ["llm"], "surprise": 1,
    }]))
    with pytest.raises(ApiKeysFileError, match="unknown field"):
        load_api_key_records(f)

    rec = {"key_id": "k1", "principal": "@svc:org",
           "secret_hash": "scrypt$x", "scopes": ["llm"]}
    f.write_text(json.dumps([rec, rec]))
    with pytest.raises(ApiKeysFileError, match="duplicate"):
        load_api_key_records(f)


def test_load_rejects_bad_principal_and_bad_scopes(tmp_path: Path):
    f = _keys(tmp_path)
    f.write_text(json.dumps([{
        "key_id": "k1", "principal": "no-at",
        "secret_hash": "scrypt$x", "scopes": ["llm"],
    }]))
    with pytest.raises(ApiKeysFileError):
        load_api_key_records(f)

    f.write_text(json.dumps([{
        "key_id": "k1", "principal": "@svc:org",
        "secret_hash": "scrypt$x", "scopes": "llm",
    }]))
    with pytest.raises(ApiKeysFileError, match="scopes"):
        load_api_key_records(f)

    f.write_text(json.dumps([{
        "key_id": "k1", "principal": "@svc:org",
        "secret_hash": "scrypt$x", "scopes": [],
    }]))
    with pytest.raises(ApiKeysFileError, match="scopes"):
        load_api_key_records(f)


def test_append_refuses_duplicate_key_id(tmp_path: Path):
    f = _keys(tmp_path)
    _token, record = mint_api_key(principal="@svc:org", scopes=("llm",))
    append_api_key_record(f, record)
    with pytest.raises(ApiKeysFileError, match="already exists"):
        append_api_key_record(f, record)


def test_file_never_contains_plaintext_token(tmp_path: Path):
    f = _keys(tmp_path)
    token, _record = _issue(f)
    assert token not in f.read_text()


# ---------- store: resolve / revoke ----------------------------------------


def test_store_resolves_valid_token(tmp_path: Path):
    f = _keys(tmp_path)
    token, record = _issue(f, principal="@svc:org", scopes=("llm", "rag:read"))
    store = JsonFileApiKeyStore(f)
    ident = store.resolve(token)
    assert ident is not None
    assert ident.principal == "@svc:org"
    assert ident.scopes == ("llm", "rag:read")
    assert ident.key_id == record["key_id"]


def test_store_rejects_unknown_and_tampered_tokens(tmp_path: Path):
    f = _keys(tmp_path)
    token, record = _issue(f)
    store = JsonFileApiKeyStore(f)
    assert store.resolve("garbage") is None
    assert store.resolve(f"{TOKEN_PREFIX}nope_nope") is None
    # right key id, wrong secret
    assert store.resolve(f"{TOKEN_PREFIX}{record['key_id']}_wrong") is None


def test_revocation_is_immediate(tmp_path: Path):
    f = _keys(tmp_path)
    token, record = _issue(f)
    store = JsonFileApiKeyStore(f)
    assert store.resolve(token) is not None  # warm the verified cache
    out = revoke_api_key_record(f, record["key_id"])
    assert out["revoked_at"]
    store.reload()  # force bypass of the mtime cache (same-second writes)
    assert store.resolve(token) is None


def test_revoke_unknown_id_raises(tmp_path: Path):
    f = _keys(tmp_path)
    _issue(f)
    with pytest.raises(ApiKeysFileError, match="no such key"):
        revoke_api_key_record(f, "does-not-exist")


def test_revoke_is_idempotent(tmp_path: Path):
    f = _keys(tmp_path)
    _token, record = _issue(f)
    first = revoke_api_key_record(f, record["key_id"])
    second = revoke_api_key_record(f, record["key_id"])
    assert second["revoked_at"] == first["revoked_at"]


def test_store_fails_closed_on_broken_file(tmp_path: Path):
    f = _keys(tmp_path)
    token, _record = _issue(f)
    store = JsonFileApiKeyStore(f)
    assert store.resolve(token) is not None
    f.write_text("{not json")
    store.reload()
    assert store.resolve(token) is None  # deny-all until the file is fixed


def test_store_missing_file_denies(tmp_path: Path):
    store = JsonFileApiKeyStore(_keys(tmp_path))
    assert store.resolve(f"{TOKEN_PREFIX}k_s") is None


def test_store_picks_up_newly_issued_key(tmp_path: Path):
    f = _keys(tmp_path)
    t1, _ = _issue(f)
    store = JsonFileApiKeyStore(f)
    assert store.resolve(t1) is not None
    t2, _ = _issue(f, principal="@other:org")
    store.reload()
    ident = store.resolve(t2)
    assert ident is not None and ident.principal == "@other:org"
