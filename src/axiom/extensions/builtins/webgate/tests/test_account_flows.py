# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The account lifecycle flows (SoilMetrix parity): forgot/reset,
magic link, email verification, self-signup, logout, password change,
provider listing, dev email log — each with the property that made it
hard-won (enumeration-proof generics, single-use epoch-bound tokens,
policy-gated signup, dev-only token log)."""

from __future__ import annotations

import re
import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate import email_outbox
from axiom.extensions.builtins.webgate.api.account_flows import (
    GENERIC_MAGIC,
    GENERIC_RESET,
    GENERIC_SIGNUP,
)
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.email_outbox import FileOutboxSender
from axiom.webauth import SESSION_COOKIE, get_password_hash
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")

PW = "Correct-Horse7"
NEW = "Sunlit-Meadow7"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    reset_key_store_for_tests()
    monkeypatch.setenv(email_outbox.OUTBOX_ENV, str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("AXIOM_GATE_MAGIC_LINKS_FILE", str(tmp_path / "magic.json"))
    monkeypatch.delenv("AXIOM_GATE_SELF_SIGNUP", raising=False)
    monkeypatch.delenv("AXIOM_ENVIRONMENT", raising=False)
    email_outbox.reset_sender()
    yield
    email_outbox.reset_sender()
    reset_key_store_for_tests()


def _store():
    return InMemoryUserStore(
        [
            User(
                user_id="u1",
                email="alice@example.org",
                password_hash=get_password_hash(PW),
                name="Alice",
                roles=("user",),
            )
        ]
    )


def _client(store=None):
    app = FastAPI()
    the_store = store if store is not None else _store()
    app.include_router(build_webgate_router(the_store, secure_cookies=False))
    c = TestClient(app, base_url="http://gate.example", follow_redirects=False)
    c.the_store = the_store  # type: ignore[attr-defined]
    return c


def _login(c, email="alice@example.org", password=PW):
    return c.post("/gate/login", data={"email": email, "password": password, "next": "/"})


def _outbox() -> list[dict]:
    return email_outbox.get_sender().sent()  # type: ignore[union-attr]


def _link(kind: str) -> str:
    mails = [m for m in _outbox() if m["kind"] == kind]
    assert mails, f"no {kind} mail in the outbox"
    m = re.search(r"(http://\S+)", mails[-1]["body"])
    assert m, mails[-1]["body"]
    return m.group(1)


# --------------------------------------------------------------- forgot/reset
def test_forgot_is_enumeration_proof():
    c = _client()
    known = c.post("/gate/forgot", json={"email": "alice@example.org"}).json()
    unknown = c.post("/gate/forgot", json={"email": "nobody@example.org"}).json()
    assert known == unknown == {"detail": GENERIC_RESET}
    # ... but only the real account got mail.
    assert len([m for m in _outbox() if m["kind"] == "reset"]) == 1


def test_reset_round_trip_and_single_use():
    c = _client()
    c.post("/gate/forgot", json={"email": "alice@example.org"})
    token = _link("reset").split("token=")[1]

    # The emailed page renders a real form.
    page = c.get(f"/gate/reset?token={token}")
    assert page.status_code == 200
    assert "new password" in page.text.lower()

    r = c.post("/gate/reset", json={"token": token, "new_password": NEW})
    assert r.status_code == 200
    # Old password dead (401 re-render, no cookie), new password lives.
    dead = _login(c, password=PW)
    assert dead.status_code == 401
    assert SESSION_COOKIE not in dead.cookies
    assert SESSION_COOKIE in _login(c, password=NEW).cookies
    # Single use: the same link again is refused (epoch bumped).
    again = c.post("/gate/reset", json={"token": token, "new_password": "Other-Word9x"})
    assert again.status_code == 401
    # The changed-password notice went out.
    assert any(m["kind"] == "notice" for m in _outbox())


def test_reset_rejects_weak_password():
    c = _client()
    c.post("/gate/forgot", json={"email": "alice@example.org"})
    token = _link("reset").split("token=")[1]
    r = c.post("/gate/reset", json={"token": token, "new_password": "weak"})
    assert r.status_code == 400


# ----------------------------------------------------------------- magic link
def test_magic_link_signs_in_exactly_once_and_verifies_email():
    c = _client()
    generic = c.post("/gate/magic-link", json={"email": "alice@example.org"}).json()
    assert generic == {"detail": GENERIC_MAGIC}
    url = _link("magic_link")
    token = url.split("token=")[1]

    r = c.get(f"/gate/magic?token={token}")
    assert r.status_code == 303
    assert SESSION_COOKIE in r.cookies
    # Clicking the emailed link proved the address.
    user = c.the_store.get_by_email("alice@example.org")
    assert (user.attributes or {}).get("email_verified_at")
    # Replay: refused.
    again = c.get(f"/gate/magic?token={token}")
    assert again.status_code == 401


def test_magic_link_never_creates_accounts_or_leaks_existence():
    c = _client()
    r = c.post("/gate/magic-link", json={"email": "nobody@example.org"}).json()
    assert r == {"detail": GENERIC_MAGIC}
    assert not [m for m in _outbox() if m["kind"] == "magic_link"]
    assert c.the_store.get_by_email("nobody@example.org") is None


# ----------------------------------------------------------- email verification
def test_verify_email_round_trip():
    c = _client()
    c.post("/gate/verify-email/resend", json={"email": "alice@example.org"})
    url = _link("verify")
    token = url.split("token=")[1]
    page = c.get(f"/gate/verify-email?token={token}")
    assert page.status_code == 200
    assert "verified" in page.text.lower()
    assert (c.the_store.get_by_email("alice@example.org").attributes or {}).get("email_verified_at")


def test_verify_email_bad_token_is_401_page():
    c = _client()
    assert c.get("/gate/verify-email?token=garbage").status_code == 401


# ---------------------------------------------------------------- self-signup
def test_signup_is_policy_gated_off_by_default():
    c = _client()
    r = c.post(
        "/gate/register",
        json={"email": "new@example.org", "password": NEW, "name": "New"},
    )
    assert r.status_code == 404
    assert c.the_store.get_by_email("new@example.org") is None


def test_signup_creates_verifies_and_never_confirms_existence(monkeypatch):
    monkeypatch.setenv("AXIOM_GATE_SELF_SIGNUP", "1")
    c = _client()
    r = c.post(
        "/gate/register",
        json={"email": "new@example.org", "password": NEW, "name": "New"},
    )
    assert r.json() == {"detail": GENERIC_SIGNUP}
    user = c.the_store.get_by_email("new@example.org")
    assert user is not None
    assert any(m["kind"] == "verify" for m in _outbox())
    assert SESSION_COOKIE in _login(c, email="new@example.org", password=NEW).cookies

    # Registering an EXISTING email answers identically and clobbers nothing.
    before_hash = c.the_store.get_by_email("alice@example.org").password_hash
    r2 = c.post(
        "/gate/register",
        json={"email": "alice@example.org", "password": "Another-Word5", "name": "X"},
    )
    assert r2.json() == {"detail": GENERIC_SIGNUP}
    assert c.the_store.get_by_email("alice@example.org").password_hash == before_hash


# ------------------------------------------------------- logout + password ops
def test_logout_clears_the_session_cookie():
    # Logout is gate-core (303 + cookie clear); parity-check it here since
    # SoilMetrix counts it in the login feature set.
    c = _client()
    _login(c)
    assert c.get("/gate/me").status_code == 200
    assert c.post("/gate/logout").status_code == 303
    assert c.get("/gate/me").status_code == 401


def test_change_password_requires_current_and_notifies():
    c = _client()
    _login(c)
    wrong = c.put("/gate/password", json={"current_password": "Nope-Wrong1", "new_password": NEW})
    assert wrong.status_code == 400
    ok = c.put("/gate/password", json={"current_password": PW, "new_password": NEW})
    assert ok.status_code == 200
    assert SESSION_COOKIE in _login(c, password=NEW).cookies
    assert any(m["kind"] == "notice" for m in _outbox())


def test_password_probe_names_the_rule():
    c = _client()
    weak = c.post("/gate/password/validate", json={"password": "weak"}).json()
    assert weak["ok"] is False and weak["message"]
    strong = c.post("/gate/password/validate", json={"password": NEW}).json()
    assert strong == {"ok": True, "message": ""}


# ------------------------------------------------------------------- profile
def test_profile_update():
    c = _client()
    _login(c)
    r = c.put("/gate/profile", json={"name": "Alice B"})
    assert r.status_code == 200
    assert c.the_store.get_by_email("alice@example.org").name == "Alice B"
    # The live session's claims are from login time; a fresh login carries
    # the new name (sessions are tokens, not DB reads — same as the gate's
    # /verify contract).
    c.post("/gate/logout")
    _login(c)
    assert c.get("/gate/me").json()["name"] == "Alice B"


# ------------------------------------------------- providers + dev email log
def test_provider_listing_carries_signup_flag(monkeypatch):
    c = _client()
    r = c.get("/gate/oidc/providers").json()
    assert r == {"providers": [], "self_signup": False}
    monkeypatch.setenv("AXIOM_GATE_SELF_SIGNUP", "yes")
    assert c.get("/gate/oidc/providers").json()["self_signup"] is True


def test_dev_email_log_is_development_only(monkeypatch):
    c = _client()
    c.post("/gate/forgot", json={"email": "alice@example.org"})
    assert c.get("/gate/dev/email-log").status_code == 404  # closed by default
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "development")
    r = c.get("/gate/dev/email-log")
    assert r.status_code == 200
    assert any(m["kind"] == "reset" for m in r.json()["emails"])


def test_forgot_page_offers_both_flows():
    c = _client()
    page = c.get("/gate/forgot").text
    assert "reset link" in page.lower()
    assert "sign-in link" in page.lower()


def test_file_outbox_is_real(tmp_path):
    sender = FileOutboxSender(tmp_path / "o.jsonl")
    sender.send(email_outbox.OutboundEmail(to="a@b", subject="s", body="b", kind="notice"))
    assert (tmp_path / "o.jsonl").is_file()
    assert sender.sent()[0]["to"] == "a@b"
