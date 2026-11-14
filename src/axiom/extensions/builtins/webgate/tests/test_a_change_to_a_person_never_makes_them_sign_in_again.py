# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A change to a person applies on their next request, and never makes them sign in.

The rows (spec-webapp-buildout-mvp.md):

- MVP-07-A2: an attribute change (clearance, scope) takes effect on the next request
  without sign-out.
- MVP-14-A1: upgrading the node keeps every session valid; nobody signs in again.
- MVP-14-A2: an invitation expires and is single-use; redeeming it twice is refused.
- MVP-14-A3: suspending a person ends their sessions and keys at once.

A session used to carry the account as it was at sign-in for twelve hours, so a
role removed, a site moved or a suspension applied only when the person signed in
again. Each test here holds one browser cookie or one key across a change and
asserts what the next request sees.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.webgate import skills as gate_skills  # noqa: E402
from axiom.infra.skills import SkillContext, SkillRegistry  # noqa: E402
from axiom.webauth import (  # noqa: E402
    SESSION_COOKIE,
    JsonFileUserStore,
    User,
    get_password_hash,
    issue_session_token,
    load_user_records,
    upsert_user_record,
)
from axiom.webauth import keys as keys_mod  # noqa: E402
from axiom.webauth import live  # noqa: E402
from axiom.webauth.api_keys import append_api_key_record, mint_api_key  # noqa: E402

EMAIL = "ana@example.org"
#: What the gate under test calls itself (TestClient's base URL); a session is
#: minted for, and verified against, its issuer.
ISSUER = "http://testserver"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WEBAUTH_SIGNING_KEY", raising=False)
    monkeypatch.delenv("WEBAUTH_RETIRED_KEYS", raising=False)
    keys_mod.reset_key_store_for_tests()
    live._reset_for_tests()
    yield
    keys_mod.reset_key_store_for_tests()
    live._reset_for_tests()


@pytest.fixture
def accounts(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "gate-users.json"
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(path))
    upsert_user_record(path, email=EMAIL, password_hash=get_password_hash("Correct-Horse-9"),
                       name="Ana", roles=["reader"], site="site-a",
                       attributes={"clearance": "internal"})
    return path


@pytest.fixture
def key_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "api-keys.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(path))
    return path


def _client(accounts: Path) -> TestClient:
    from axiom.extensions.builtins.webgate.api.routers import build_webgate_router

    app = FastAPI()
    app.include_router(build_webgate_router(
        user_store=JsonFileUserStore(accounts), oidc=None, secure_cookies=False))
    return TestClient(app)


def _cookie(accounts: Path) -> str:
    (rec,) = [r for r in load_user_records(accounts) if r["email"] == EMAIL]
    return issue_session_token(User(**rec), issuer=ISSUER)


def _verify(client: TestClient, cookie: str):
    client.cookies.set(SESSION_COOKIE, cookie)
    return client.get("/gate/verify")


def _gate(verb: str, params: dict):
    reg = SkillRegistry()
    gate_skills.bind(reg)
    ctx = SkillContext(registry=reg, state_dir=Path(__import__("tempfile").mkdtemp()), logger=__import__("logging").getLogger("t"),
                       actor="@admin:site-a")
    return reg.invoke(f"gate.{verb}", params, ctx)


def _ana_key(key_file: Path) -> str:
    token, record = mint_api_key(principal="@ana", scopes=["chat:invoke"], site="site-a")
    append_api_key_record(key_file, record)
    return token


# ---------------------------------------------------------------------------
# MVP-07-A2: the next request sees the change
# ---------------------------------------------------------------------------


def test_mvp_07_a2_a_role_change_applies_on_the_next_request_with_the_same_cookie(accounts):
    client, cookie = _client(accounts), _cookie(accounts)
    assert _verify(client, cookie).headers["X-Axiom-User-Roles"] == "reader"

    assert _gate("role", {"email": EMAIL, "set": ["site-admin"]}).ok

    r = _verify(client, cookie)
    assert r.status_code == 200
    assert r.headers["X-Axiom-User-Roles"] == "site-admin"


def test_mvp_07_a2_a_scope_change_moves_the_session_to_the_new_site(accounts):
    client, cookie = _client(accounts), _cookie(accounts)
    assert _verify(client, cookie).headers["X-Axiom-User-Site"] == "site-a"

    upsert_user_record(accounts, email=EMAIL, password_hash=None, site="site-b", overwrite=True)

    r = _verify(client, cookie)
    assert r.status_code == 200 and r.headers["X-Axiom-User-Site"] == "site-b"


def test_mvp_07_a2_a_clearance_change_is_what_the_next_request_carries(accounts):
    from axiom.webauth import session_from_cookies

    cookie = _cookie(accounts)
    assert session_from_cookies({SESSION_COOKIE: cookie})["attributes"] == {"clearance": "internal"}

    upsert_user_record(accounts, email=EMAIL, password_hash=None,
                       attributes={"clearance": "controlled"}, overwrite=True)

    assert session_from_cookies({SESSION_COOKIE: cookie})["attributes"] == {"clearance": "controlled"}


def test_mvp_07_a2_an_account_the_file_does_not_hold_keeps_its_signed_claims(accounts):
    """Nobody is logged out because their session came from somewhere else."""
    from axiom.webauth import session_from_cookies

    other = issue_session_token(User(user_id="sso-subject-1", email="bo@example.org",
                                     roles=("viewer",), site="site-a"))
    claims = session_from_cookies({SESSION_COOKIE: other})
    assert claims is not None and claims["roles"] == ["viewer"] and claims["site"] == "site-a"


# ---------------------------------------------------------------------------
# MVP-14-A3: suspend ends sessions and keys at once; resume restores them in place
# ---------------------------------------------------------------------------


def test_mvp_14_a3_suspending_ends_the_session_and_the_keys_at_once(accounts, key_file):
    client, cookie, token = _client(accounts), _cookie(accounts), _ana_key(key_file)
    assert _verify(client, cookie).status_code == 200
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get("/gate/verify", headers=bearer).status_code == 200

    result = _gate("suspend", {"email": EMAIL, "reason": "left the project"})
    assert result.ok, result.errors
    assert len(result.value["keys_refused"]) == 1

    assert _verify(client, cookie).status_code == 401
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/gate/me").status_code == 401
    client.cookies.clear()
    assert client.get("/gate/verify", headers=bearer).status_code == 401


def test_mvp_14_a3_a_suspended_person_cannot_sign_in_again(accounts):
    client = _client(accounts)
    assert _gate("suspend", {"email": EMAIL}).ok
    r = client.post("/gate/login", data={"email": EMAIL, "password": "Correct-Horse-9"},
                    follow_redirects=False)
    assert SESSION_COOKIE not in r.cookies


def test_mvp_14_a3_resuming_lets_the_same_session_and_key_back_in(accounts, key_file):
    client, cookie, token = _client(accounts), _cookie(accounts), _ana_key(key_file)
    assert _gate("suspend", {"email": EMAIL}).ok
    assert _verify(client, cookie).status_code == 401

    assert _gate("reinstate", {"email": EMAIL}).ok

    assert _verify(client, cookie).status_code == 200
    client.cookies.clear()
    assert client.get("/gate/verify", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_mvp_14_a3_suspending_one_person_leaves_everyone_else_working(accounts, key_file):
    upsert_user_record(accounts, email="bo@example.org", password_hash=get_password_hash("Other-Pass-7"),
                       roles=["reader"], site="site-a")
    (bo,) = [r for r in load_user_records(accounts) if r["email"] == "bo@example.org"]
    bo_cookie = issue_session_token(User(**bo), issuer=ISSUER)
    token, record = mint_api_key(principal="@bo", scopes=["chat:invoke"], site="site-a")
    append_api_key_record(key_file, record)
    client = _client(accounts)

    assert _gate("suspend", {"email": EMAIL}).ok

    assert _verify(client, bo_cookie).status_code == 200
    client.cookies.clear()
    assert client.get("/gate/verify", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_mvp_14_a3_the_suspension_is_kept_where_every_release_can_read_it(accounts):
    """A rollback must not lock the accounts file: no new top-level field is written."""
    assert _gate("suspend", {"email": EMAIL, "reason": "audit"}).ok
    (entry,) = json.loads(accounts.read_text())
    assert set(entry) <= {"user_id", "email", "password_hash", "name", "roles", "attributes", "site"}
    assert entry["attributes"]["suspended_at"] and entry["attributes"]["suspended_reason"] == "audit"
    assert entry["attributes"]["suspended_by"] == "@admin:site-a"


# ---------------------------------------------------------------------------
# MVP-14-A1: an upgrade keeps every session valid
# ---------------------------------------------------------------------------


def _pem() -> str:
    return keys_mod.SigningKey.generate().to_pem()


def test_mvp_14_a1_a_new_release_process_accepts_the_sessions_the_old_one_issued(
        accounts, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("WEBAUTH_SIGNING_KEY", _pem())
    keys_mod.reset_key_store_for_tests()
    cookie = _cookie(accounts)

    # The new slot: a fresh process (no cached key store, no cached account
    # store), the same configuration, a freshly built gate.
    keys_mod.reset_key_store_for_tests()
    live._reset_for_tests()
    r = _verify(_client(accounts), cookie)

    assert r.status_code == 200
    assert r.headers["X-Axiom-User-Email"] == EMAIL


def test_mvp_14_a1_rotating_the_signing_key_keeps_the_sessions_signed_by_the_old_one(
        accounts, monkeypatch):
    old = _pem()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("WEBAUTH_SIGNING_KEY", old)
    keys_mod.reset_key_store_for_tests()
    cookie = _cookie(accounts)

    monkeypatch.setenv("WEBAUTH_SIGNING_KEY", _pem())
    monkeypatch.setenv("WEBAUTH_RETIRED_KEYS", json.dumps([old]))
    keys_mod.reset_key_store_for_tests()

    assert _verify(_client(accounts), cookie).status_code == 200


def test_mvp_14_a1_a_deployed_node_never_signs_with_a_key_that_dies_at_restart(monkeypatch):
    """An ephemeral key would log everyone out at the next restart, so it is refused."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    keys_mod.reset_key_store_for_tests()
    with pytest.raises(RuntimeError, match="WEBAUTH_SIGNING_KEY"):
        keys_mod.load_key_store()


def test_mvp_14_a1_an_account_written_by_this_release_still_loads_and_signs_in(accounts):
    """The accounts file stays readable by the code that reads it at startup."""
    assert _gate("suspend", {"email": EMAIL}).ok
    assert _gate("reinstate", {"email": EMAIL}).ok
    store = JsonFileUserStore(accounts)
    assert store.get_by_email(EMAIL) is not None and not store.get_by_email(EMAIL).disabled
    r = _client(accounts).post("/gate/login", data={"email": EMAIL, "password": "Correct-Horse-9"},
                               follow_redirects=False)
    assert SESSION_COOKIE in r.cookies


# ---------------------------------------------------------------------------
# MVP-14-A2: an invitation expires and is single-use
# ---------------------------------------------------------------------------


def _roles(names):
    return ["chat:invoke"]


def _invite(tmp_path, *, now=None, expires=timedelta(days=7)):
    from axiom.webauth import invitations

    code, record = invitations.mint_invitation(
        principal="@cy:site-a", roles=["reader"], scopes=["chat:invoke"], site="site-a",
        expires_in=expires, now=now)
    path = tmp_path / "invitations.json"
    invitations.append_invitation(path, record)
    return path, code


def test_mvp_14_a2_an_invitation_past_its_expiry_is_refused(tmp_path):
    from axiom.webauth import invitations

    issued = datetime.now(UTC) - timedelta(days=8)
    path, code = _invite(tmp_path, now=issued)
    with pytest.raises(invitations.InvitationRefused, match="expired"):
        invitations.redeem_invitation(path, code, resolve_roles=_roles)
    assert invitations.list_invitations(path)[0]["status"] == "expired"


def test_mvp_14_a2_redeeming_an_invitation_twice_is_refused(tmp_path):
    from axiom.webauth import invitations

    path, code = _invite(tmp_path)
    token, key, _row = invitations.redeem_invitation(path, code, resolve_roles=_roles)
    assert token and key["principal"] == "@cy:site-a"

    with pytest.raises(invitations.InvitationRefused, match="already redeemed"):
        invitations.redeem_invitation(path, code, resolve_roles=_roles)
    assert invitations.list_invitations(path)[0]["status"] == "redeemed"
