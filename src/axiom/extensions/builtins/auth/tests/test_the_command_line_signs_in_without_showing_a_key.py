# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One command, a browser sign-in, and a key in the vault that nobody saw.

Until this existed, a colleague signed in on the node's Keys page, pressed a button,
copied the key off the page and pasted it into ``vault set``. Every key passed
through a clipboard and a person's eyes, and the step where most of them stalled was
the paste.

The loopback pattern removes the copy: the node hands a one-time code to a listener
on the person's own computer, and the command line trades it for the key, which goes
straight into the vault. These tests run that round trip for real: the node's actual
routes in process, a real listener on 127.0.0.1, and a scripted browser that does
what a person's browser does (follow the page, press the button, follow the
redirect). Nothing in the path is replaced except the person.

The refusals are the security argument, each tested by name: a wrong ``state``, a
code used twice, an expired code, a verifier that does not match, and a return
address that is not this computer.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import timedelta
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FACE = "http://testserver"


# --------------------------------------------------------------------------- the node


@pytest.fixture
def keys(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "node" / "api-keys.json"
    path.parent.mkdir()
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(path))
    return path


@pytest.fixture
def node(keys) -> TestClient:
    from axiom.extensions.builtins.webgate.api.routers import build_webgate_router

    app = FastAPI()
    app.include_router(build_webgate_router(oidc=None, secure_cookies=False))
    return TestClient(app, follow_redirects=False)


def _signed_in(monkeypatch, **claims):
    """A gate session, as ``session_from_cookies`` would return one."""
    from axiom.extensions.builtins.webgate.api import routers

    base = {"sub": "u-1", "email": "someone@example.edu", "name": "Someone",
            "roles": ["viewer"], "site": "example"}
    base.update(claims)
    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: base)
    return base


def _signed_out(monkeypatch):
    from axiom.extensions.builtins.webgate.api import routers

    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: None)


# --------------------------------------------------------------------------- this computer


@pytest.fixture
def vault(tmp_path):
    """This computer's vault: the real named store over a file custody backend in tmp."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.extensions.builtins.secrets.providers.localfile import FileSecretStoreProvider

    values = FileSecretStoreProvider(
        {"name": "test-vault", "path": str(tmp_path / "laptop" / "values.json")}
    ).open()
    return ForeignCredentialStore(tmp_path / "laptop", value_store=values)


def _post_json(node: TestClient):
    def post(url: str, body: dict, _timeout: float):
        reply = node.post(urlsplit(url).path, json=body)
        return reply.status_code, reply.json()

    return post


def _follow_to_loopback(location: str) -> int:
    """What the person's browser does with the node's redirect: open it."""
    try:
        with urllib.request.urlopen(location, timeout=10) as resp:  # noqa: S310 - 127.0.0.1
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _form_fields(page: str) -> dict:
    return {
        unescape(m.group(1)): unescape(m.group(2))
        for m in re.finditer(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page)
    }


def _browser_that_approves(node: TestClient, seen: dict):
    """Open the page, read it, press Connect, follow the redirect."""

    def browse(url: str) -> bool:
        page = node.get(urlsplit(url).path + "?" + urlsplit(url).query)
        seen["page"] = page
        assert page.status_code == 200, page.text
        approved = node.post("/gate/cli/approve", data=_form_fields(page.text))
        seen["approve"] = approved
        assert approved.status_code == 303, approved.text
        seen["listener_status"] = _follow_to_loopback(approved.headers["location"])
        return True

    return browse


def _browser_with_invitation(node: TestClient, code: str, seen: dict):
    def browse(url: str) -> bool:
        page = node.get(urlsplit(url).path + "?" + urlsplit(url).query)
        seen["page"] = page
        fields = _form_fields(page.text)
        sent = node.post("/gate/cli/invitation", data={**fields, "invitation_code": code})
        seen["invitation"] = sent
        if sent.status_code == 303:
            seen["listener_status"] = _follow_to_loopback(sent.headers["location"])
        return True

    return browse


def _sign_in(node, vault, browser, capsys=None, **kw):
    from axiom.extensions.builtins.auth.node_signin import sign_in

    said: list[str] = []
    result = sign_in(
        FACE, open_browser=browser, post_json=_post_json(node), store=vault,
        say=said.append, timeout_s=kw.pop("timeout_s", 10), **kw,
    )
    return result, said


# --------------------------------------------------------------------------- the point


def test_a_signed_in_person_gets_a_working_key_in_their_vault(node, keys, vault, monkeypatch):
    from axiom.webauth.api_keys import JsonFileApiKeyStore

    _signed_in(monkeypatch)
    seen: dict = {}
    result, _said = _sign_in(node, vault, _browser_that_approves(node, seen))

    assert result.ok, (result.detail, result.remedy)
    assert seen["listener_status"] == 200
    assert result.stored_as == "testserver-key"
    assert result.token_ref == "foreign://testserver-key"
    with vault.get("testserver-key") as secret:
        stored = secret.as_str()
    identity = JsonFileApiKeyStore(keys).resolve(stored)
    assert identity is not None, "the vault holds a value the node does not recognise"
    assert identity.scopes == ("*:read",)
    assert result.site == "example"


def test_the_key_is_never_printed_or_put_in_a_page(node, keys, vault, monkeypatch, capsys):
    """The key reaches exactly one place: the vault."""
    _signed_in(monkeypatch)
    seen: dict = {}
    result, said = _sign_in(node, vault, _browser_that_approves(node, seen))
    assert result.ok
    with vault.get(result.stored_as) as secret:
        token = secret.as_str()
    out = capsys.readouterr()
    for surface in (out.out, out.err, "\n".join(said), seen["page"].text,
                    seen["approve"].headers.get("location", ""), repr(result)):
        assert token not in surface


def test_an_invitation_code_signs_in_the_same_way(node, keys, vault, monkeypatch):
    """For people without a university account: same command, same result."""
    from axiom.webauth.invitations import append_invitation, load_invitations, mint_invitation

    _signed_out(monkeypatch)
    invites = keys.with_name("invitations.json")
    code, record = mint_invitation(
        principal="@guest:example", roles=["viewer"], scopes=["*:read"], site="example",
        expires_in=timedelta(days=7),
    )
    append_invitation(invites, record)

    seen: dict = {}
    result, _ = _sign_in(node, vault, _browser_with_invitation(node, code, seen))

    assert result.ok, (result.detail, result.remedy)
    assert seen["invitation"].status_code == 303
    assert "invitation" in seen["page"].text.lower()
    assert result.principal.startswith("@guest")
    assert load_invitations(invites)[0]["redeemed_at"], "the invitation was not spent"


# --------------------------------------------------------------------------- refusals


def test_a_return_address_that_is_not_this_computer_is_refused_in_place(node, monkeypatch):
    """Refused with a page, never redirected: the route cannot become an open redirect."""
    _signed_in(monkeypatch)
    base = {"state": "s" * 32, "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    for bad in (
        "http://evil.example/callback",
        "https://127.0.0.1:5000/callback",
        "http://localhost:5000/callback",
        "http://127.0.0.1@evil.example:5000/callback",
        "http://127.0.0.1:5000/elsewhere",
        "http://127.0.0.1:5000/callback?next=http://evil.example",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:80/callback",
    ):
        reply = node.get("/gate/cli/authorize", params={**base, "redirect_uri": bad})
        assert reply.status_code == 400, bad
        assert "location" not in reply.headers, bad
        approved = node.post("/gate/cli/approve", data={**base, "redirect_uri": bad})
        assert approved.status_code == 400, bad
        assert "location" not in approved.headers, bad


def test_only_the_s256_challenge_is_accepted(node, monkeypatch):
    _signed_in(monkeypatch)
    reply = node.get("/gate/cli/authorize", params={
        "redirect_uri": "http://127.0.0.1:5000/callback", "state": "s" * 32,
        "code_challenge": "c" * 43, "code_challenge_method": "plain",
    })
    assert reply.status_code == 400


def test_an_answer_with_the_wrong_state_is_ignored_and_the_right_one_still_lands():
    """A stray or hostile request to the port can neither end the sign-in nor
    substitute its own code."""
    from axiom.extensions.builtins.auth.loopback import LoopbackListener

    with LoopbackListener("the-real-state-0123456789") as listener:
        base = listener.redirect_uri
        assert _follow_to_loopback(f"{base}?code=forged&state=wrong-state-000000000") == 400
        assert listener.wait(0.2) is None, "a wrong state ended the sign-in"
        assert _follow_to_loopback(f"{base}?code=real&state=the-real-state-0123456789") == 200
        got = listener.wait(2)
        assert got is not None and got.code == "real"
        # One answer only: a second, even with the right state, is turned away.
        assert _follow_to_loopback(f"{base}?code=second&state=the-real-state-0123456789") == 400
        assert listener.wait(0.1).code == "real"


def test_the_listener_answers_only_on_this_computer():
    from axiom.extensions.builtins.auth.loopback import LoopbackListener

    with LoopbackListener("x" * 24) as listener:
        assert urlsplit(listener.redirect_uri).hostname == "127.0.0.1"


def _approved_code(node, monkeypatch, *, verifier_challenge=None):
    """Run the browser leg by hand and return (code, redirect_uri, verifier)."""
    from axiom.extensions.builtins.auth.pkce import generate_pkce

    _signed_in(monkeypatch)
    verifier, challenge = verifier_challenge or generate_pkce()
    redirect = "http://127.0.0.1:45678/callback"
    reply = node.post("/gate/cli/approve", data={
        "redirect_uri": redirect, "state": "s" * 32,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    assert reply.status_code == 303, reply.text
    q = parse_qs(urlsplit(reply.headers["location"]).query)
    assert q["state"] == ["s" * 32]
    return q["code"][0], redirect, verifier


def test_a_code_works_once(node, keys, monkeypatch):
    code, redirect, verifier = _approved_code(node, monkeypatch)
    body = {"code": code, "code_verifier": verifier, "redirect_uri": redirect}
    first = node.post("/gate/cli/token", json=body)
    assert first.status_code == 200, first.text
    assert first.headers.get("cache-control") == "no-store"
    again = node.post("/gate/cli/token", json=body)
    assert again.status_code == 400
    assert again.json()["code"] == "invalid_grant"
    assert len(json.loads(keys.read_text())) == 1, "a replayed code minted a second key"


def test_a_code_presented_with_the_wrong_verifier_is_refused_and_burned(node, keys, monkeypatch):
    code, redirect, verifier = _approved_code(node, monkeypatch)
    wrong = node.post("/gate/cli/token", json={
        "code": code, "code_verifier": "w" * 43, "redirect_uri": redirect})
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_grant"
    right = node.post("/gate/cli/token", json={
        "code": code, "code_verifier": verifier, "redirect_uri": redirect})
    assert right.status_code == 400, "a code survived a failed presentation"
    assert not keys.exists() or json.loads(keys.read_text()) == []


def test_a_code_for_another_return_address_is_refused(node, monkeypatch):
    code, _redirect, verifier = _approved_code(node, monkeypatch)
    reply = node.post("/gate/cli/token", json={
        "code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:1111/callback"})
    assert reply.status_code == 400 and reply.json()["code"] == "invalid_grant"


def test_an_expired_code_is_refused(keys, monkeypatch):
    from axiom.extensions.builtins.auth.pkce import generate_pkce
    from axiom.extensions.builtins.webgate.api.cli_signin_route import register_cli_signin_routes

    clock = {"t": 1_000_000.0}
    router = APIRouter()
    claims = {"sub": "u-1", "email": "someone@example.edu", "roles": ["viewer"], "site": "example"}
    register_cli_signin_routes(
        router, keys_path=lambda: keys, invitations_path=lambda: keys.with_name("inv.json"),
        claims_of=lambda _r: claims,
        shell=lambda *, title, inner, status=200: HTMLResponse(inner, status_code=status),
        now=lambda: clock["t"],
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, follow_redirects=False)
    verifier, challenge = generate_pkce()
    redirect = "http://127.0.0.1:45678/callback"
    reply = client.post("/gate/cli/approve", data={
        "redirect_uri": redirect, "state": "s" * 32,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    code = parse_qs(urlsplit(reply.headers["location"]).query)["code"][0]
    clock["t"] += 121
    late = client.post("/gate/cli/token", json={
        "code": code, "code_verifier": verifier, "redirect_uri": redirect})
    assert late.status_code == 400 and late.json()["code"] == "invalid_grant"


def test_a_person_with_no_role_hears_why_in_their_terminal(node, vault, monkeypatch):
    """The refusal travels back to the waiting command line, so the reason is
    printed where they are looking, not left on a browser tab."""
    _signed_in(monkeypatch, roles=[])

    def browse(url: str) -> bool:
        reply = node.get(urlsplit(url).path + "?" + urlsplit(url).query)
        assert reply.status_code == 303
        _follow_to_loopback(reply.headers["location"])
        return True

    result, _ = _sign_in(node, vault, browse)
    assert not result.ok
    assert "no role" in result.detail
    assert "administrator" in result.remedy
    assert vault.list() == []


def test_a_wrong_invitation_code_is_refused_on_the_page_and_spends_nothing(node, keys, monkeypatch):
    from axiom.webauth.invitations import append_invitation, load_invitations, mint_invitation

    _signed_out(monkeypatch)
    invites = keys.with_name("invitations.json")
    code, record = mint_invitation(
        principal="@guest:example", roles=["viewer"], scopes=["*:read"], site="example",
        expires_in=timedelta(days=7))
    append_invitation(invites, record)
    form = {"redirect_uri": "http://127.0.0.1:45678/callback", "state": "s" * 32,
            "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    reply = node.post("/gate/cli/invitation", data={**form, "invitation_code": code + "x"})
    assert reply.status_code == 401
    assert "not recognised" in reply.text
    assert code not in reply.text
    assert load_invitations(invites)[0]["redeemed_at"] is None


def test_an_abandoned_sign_in_mints_nothing_and_spends_no_invitation(node, keys, monkeypatch):
    """The key is minted when the code is collected. A browser leg nobody finished
    leaves no key on file and no invitation used up."""
    from axiom.webauth.invitations import append_invitation, load_invitations, mint_invitation

    _approved_code(node, monkeypatch)
    _signed_out(monkeypatch)
    invites = keys.with_name("invitations.json")
    code, record = mint_invitation(
        principal="@guest:example", roles=["viewer"], scopes=["*:read"], site="example",
        expires_in=timedelta(days=7))
    append_invitation(invites, record)
    form = {"redirect_uri": "http://127.0.0.1:45678/callback", "state": "s" * 32,
            "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    assert node.post("/gate/cli/invitation", data={**form, "invitation_code": code}).status_code == 303

    assert not keys.exists() or json.loads(keys.read_text()) == []
    assert load_invitations(invites)[0]["redeemed_at"] is None


def test_the_code_store_holds_no_secret_at_rest(node, keys, monkeypatch):
    """Only hashes of codes, and an invitation sealed under a code the node never kept."""
    from axiom.webauth.invitations import append_invitation, mint_invitation

    _signed_out(monkeypatch)
    invites = keys.with_name("invitations.json")
    invitation, record = mint_invitation(
        principal="@guest:example", roles=["viewer"], scopes=["*:read"], site="example",
        expires_in=timedelta(days=7))
    append_invitation(invites, record)
    form = {"redirect_uri": "http://127.0.0.1:45678/callback", "state": "s" * 32,
            "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    reply = node.post("/gate/cli/invitation", data={**form, "invitation_code": invitation})
    one_time = parse_qs(urlsplit(reply.headers["location"]).query)["code"][0]
    at_rest = keys.with_name("cli-sign-ins.json").read_text()
    assert invitation not in at_rest
    assert one_time not in at_rest
    assert oct(keys.with_name("cli-sign-ins.json").stat().st_mode & 0o777) == "0o600"


def test_a_session_cannot_widen_its_key_through_the_form(node, keys, monkeypatch):
    """The approve form carries a site and nothing else a caller may choose."""
    from axiom.extensions.builtins.auth.pkce import generate_pkce

    _signed_in(monkeypatch)
    verifier, challenge = generate_pkce()
    redirect = "http://127.0.0.1:45678/callback"
    reply = node.post("/gate/cli/approve", data={
        "redirect_uri": redirect, "state": "s" * 32, "code_challenge": challenge,
        "code_challenge_method": "S256", "scopes": "*:govern", "site": "somewhere-else"})
    assert reply.status_code == 303
    q = parse_qs(urlsplit(reply.headers["location"]).query)
    assert q.get("error") == ["access_denied"], "a site outside the role was accepted"
    reply = node.post("/gate/cli/approve", data={
        "redirect_uri": redirect, "state": "s" * 32, "code_challenge": challenge,
        "code_challenge_method": "S256", "scopes": "*:govern"})
    code = parse_qs(urlsplit(reply.headers["location"]).query)["code"][0]
    body = node.post("/gate/cli/token", json={
        "code": code, "code_verifier": verifier, "redirect_uri": redirect}).json()
    assert body["scopes"] == ["*:read"]


def test_a_node_address_that_is_not_a_web_address_is_refused_before_anything_opens(vault):
    from axiom.extensions.builtins.auth.node_signin import sign_in

    opened: list[str] = []
    result = sign_in("node.example.org", open_browser=lambda u: opened.append(u) or True,
                     store=vault, say=lambda _m: None, timeout_s=1)
    assert not result.ok and not opened


def test_the_sign_in_pages_cannot_be_framed_by_another_site(node, monkeypatch):
    """A Connect button inside somebody else's frame could be clicked for you."""
    params = {"redirect_uri": "http://127.0.0.1:45678/callback", "state": "s" * 32,
              "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    _signed_in(monkeypatch)
    consent = node.get("/gate/cli/authorize", params=params)
    _signed_out(monkeypatch)
    sign_in = node.get("/gate/cli/authorize", params=params)
    for page in (consent, sign_in):
        assert page.status_code == 200
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"
