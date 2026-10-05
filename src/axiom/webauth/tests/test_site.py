# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The site on the principal (ADR-050 tenancy): ``@name:context`` where the
context IS the site. Threaded through accounts, sessions, and API keys so a
guest tenant's people and keys can never act under another site's name."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from axiom.webauth import (
    AccountsFileError,
    JsonFileUserStore,
    User,
    issue_session_token,
    load_user_records,
    principal_site,
    upsert_user_record,
    validate_site,
    verify_session_token,
)
from axiom.webauth.api_keys import (
    ApiKeysFileError,
    JsonFileApiKeyStore,
    append_api_key_record,
    bind_principal_to_site,
    load_api_key_records,
    mint_api_key,
)
from axiom.webauth.keys import reset_key_store_for_tests


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


# ---------------------------------------------------------------- grammar


def test_validate_site_and_principal_site():
    assert validate_site(None) is None and validate_site("  ") is None
    assert validate_site(" site-a ") == "site-a"
    assert validate_site("senna.loop_1") == "senna.loop_1"
    with pytest.raises(ValueError):
        validate_site("senna loop")
    with pytest.raises(ValueError):
        validate_site("senna:loop")
    assert principal_site("@svc:senna") == "senna"
    assert principal_site("@svc") is None
    assert principal_site("nonsense") is None


def test_user_carries_site_and_derives_its_handle():
    u = User(user_id="alice@example.edu", email="alice@example.edu", site="site-a")
    assert u.site == "site-a"
    assert u.handle == "@alice_example.edu:site-a"
    assert User(user_id="oid-1", email="a@b.org").handle == "@oid-1"
    with pytest.raises(ValueError):
        User(user_id="x", email="x@y.org", site="bad site")


# ---------------------------------------------------------------- sessions


def test_session_token_carries_the_site_only_when_set():
    with_site = User(user_id="u1", email="u@x.org", site="senna")
    claims = verify_session_token(
        issue_session_token(with_site, ttl=timedelta(hours=1), issuer="http://n"), issuer="http://n"
    )
    assert claims["site"] == "senna"
    without = User(user_id="u2", email="v@x.org")
    claims = verify_session_token(
        issue_session_token(without, ttl=timedelta(hours=1), issuer="http://n"), issuer="http://n"
    )
    assert "site" not in claims


# ---------------------------------------------------------------- accounts file


def test_accounts_file_round_trips_site_and_validates_it(tmp_path):
    p = tmp_path / "accounts.json"
    out = upsert_user_record(
        p, email="a@example.edu", password_hash="scrypt$x", roles=["student"], site="site-a"
    )
    assert out["site"] == "site-a"
    [rec] = load_user_records(p)
    assert rec["site"] == "site-a"
    assert json.loads(p.read_text())[0]["site"] == "site-a"
    assert JsonFileUserStore(p).get_by_email("a@example.edu").site == "site-a"
    # update without site keeps it; explicit site changes it
    upsert_user_record(p, email="a@example.edu", password_hash="scrypt$y", overwrite=True)
    assert load_user_records(p)[0]["site"] == "site-a"
    upsert_user_record(
        p, email="a@example.edu", password_hash="scrypt$y", overwrite=True, site="senna"
    )
    assert load_user_records(p)[0]["site"] == "senna"
    p.write_text(
        json.dumps([{"email": "b@x.org", "password_hash": "scrypt$z", "site": "no spaces"}])
    )
    with pytest.raises(AccountsFileError, match="site"):
        load_user_records(p)


def test_accounts_without_site_serialize_without_the_key(tmp_path):
    p = tmp_path / "accounts.json"
    upsert_user_record(p, email="a@example.edu", password_hash="scrypt$x")
    assert "site" not in json.loads(p.read_text())[0]
    assert load_user_records(p)[0]["site"] is None


# ---------------------------------------------------------------- API keys


def test_bind_principal_to_site_rules():
    assert bind_principal_to_site("@svc", None) == ("@svc", None)
    assert bind_principal_to_site("@svc", "senna") == ("@svc:senna", "senna")
    assert bind_principal_to_site("@svc:senna", None) == ("@svc:senna", "senna")
    assert bind_principal_to_site("@svc:senna", "senna") == ("@svc:senna", "senna")
    with pytest.raises(ValueError, match="names site"):
        bind_principal_to_site("@svc:senna", "prost")


def test_minted_key_records_the_site_and_resolves_with_it(tmp_path):
    f = tmp_path / "keys.json"
    token, record = mint_api_key(principal="@ingest", scopes=("ingest",), site="senna")
    assert record["principal"] == "@ingest:senna" and record["site"] == "senna"
    append_api_key_record(f, record)
    identity = JsonFileApiKeyStore(f).resolve(token)
    assert identity is not None
    assert identity.principal == "@ingest:senna" and identity.site == "senna"
    # a key without a site keeps working and reports none
    token2, record2 = mint_api_key(principal="@svc", scopes=("llm",))
    append_api_key_record(f, record2)
    assert JsonFileApiKeyStore(f).resolve(token2).site is None


def test_keys_file_refuses_a_site_that_disagrees_with_the_handle(tmp_path):
    f = tmp_path / "keys.json"
    _, record = mint_api_key(principal="@svc:senna", scopes=("llm",))
    record["site"] = "prost"
    f.write_text(json.dumps([record]))
    with pytest.raises(ApiKeysFileError, match="disagrees"):
        load_api_key_records(f)


def test_legacy_keys_file_without_site_loads_and_infers_from_handle(tmp_path):
    f = tmp_path / "keys.json"
    _, record = mint_api_key(principal="@svc:org", scopes=("llm",))
    record.pop("site")
    f.write_text(json.dumps([record]))
    [rec] = load_api_key_records(f)
    assert rec["site"] == "org"
