# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A colleague should be able to get their own key without anybody minting it.

Onboarding three colleagues on 2026-10-01 took this shape: each one needed a
scoped key, so the administrator logged into the node, ran `gate issue api-key`
three times, and sent each person their key. Every key therefore existed in the
administrator's terminal, in the administrator's scrollback, and in whatever
channel carried it. One of them reached a chat paste and had to be rotated.

The administrator never needed to see any of them. What the administrator needs
to decide is *who may have what*; the secret itself only has to reach one
machine, and it is not theirs.

So an invitation. The administrator creates one naming a principal, a role and a
site. The colleague redeems it and the key is minted on their side. The thing
that travels is not a credential for anything except its own redemption, it is
single-use, and it expires.

The rule that makes an unredeemed invitation safe to leave lying around is that
**it can only shrink.** Scopes are recorded when it is created, resolved again
when it is redeemed, and a redemption that would grant more than was approved is
refused. A role widened in between does not widen an invitation nobody approved
at that width; a role narrowed in between does narrow it, which is the direction
that is safe to apply without asking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.webauth import api_keys, invitations


def _bundles(**roles):
    """A stand-in for role resolution: role name -> scopes."""

    def resolve(names):
        out: list[str] = []
        for name in names:
            if name not in roles:
                raise KeyError(name)
            for scope in roles[name]:
                if scope not in out:
                    out.append(scope)
        return tuple(out)

    return resolve


VIEWER = _bundles(viewer=("*:read",))


def _invite(tmp_path: Path, **kw):
    params = dict(
        principal="@someone:example",
        roles=("viewer",),
        scopes=("*:read",),
        site="example",
        name="onboarding",
        invited_by="@approver:example",
        expires_in=timedelta(days=7),
    )
    params.update(kw)
    code, record = invitations.mint_invitation(**params)
    invitations.append_invitation(tmp_path / "invitations.json", record)
    return code


# ---------------------------------------------------------------------------
# What travels, and what does not
# ---------------------------------------------------------------------------


def test_the_code_is_not_stored_only_its_hash(tmp_path):
    """The same property the key store already has, for the same reason: a
    readable invitation file would be a file full of usable invitations."""
    code = _invite(tmp_path)
    stored = invitations.load_invitations(tmp_path / "invitations.json")
    assert len(stored) == 1
    blob = str(stored[0])
    # The secret is urlsafe base64 and may itself contain ``_``; take it the way
    # the module does, not by the last ``_``, or the check sees a one-character
    # fragment that any record matches by chance.
    _, secret = invitations.parse_code(code)
    assert len(secret) >= 40
    assert code not in blob, "the invitation file holds the code in the clear"
    assert secret not in blob, "the invitation file holds the code in the clear"
    assert stored[0]["code_hash"]


def test_redeeming_returns_a_key_the_inviter_never_saw(tmp_path):
    code = _invite(tmp_path)
    token, key_record, _ = invitations.redeem_invitation(
        tmp_path / "invitations.json", code, resolve_roles=VIEWER
    )
    assert token.startswith("axk_")
    assert key_record["principal"] == "@someone:example"
    assert key_record["scopes"] == ["*:read"]
    assert key_record["site"] == "example"
    assert "secret_hash" in key_record
    # Parsed, not ``split("_")[-1]``: the secret may contain ``_``, and a
    # fragment of it is a substring of an innocent record a few percent of the
    # time. The whole secret is what must not be there.
    _, secret = api_keys.parse_token(token)
    assert len(secret) >= 40
    blob = str(key_record)
    assert token not in blob, "the key record holds the token"
    assert secret not in blob, "the key record holds the token"


def test_the_invitation_records_who_approved_it(tmp_path):
    """An audit of "who gave this person access" has to have an answer, and the
    redeeming colleague is not it."""
    code = _invite(tmp_path)
    _, _, spent = invitations.redeem_invitation(
        tmp_path / "invitations.json", code, resolve_roles=VIEWER
    )
    assert spent["invited_by"] == "@approver:example"
    assert spent["redeemed_at"]


# ---------------------------------------------------------------------------
# Single use, and expiry
# ---------------------------------------------------------------------------


def test_an_invitation_can_only_be_redeemed_once(tmp_path):
    code = _invite(tmp_path)
    invitations.redeem_invitation(tmp_path / "invitations.json", code, resolve_roles=VIEWER)
    with pytest.raises(invitations.InvitationRefused) as refused:
        invitations.redeem_invitation(
            tmp_path / "invitations.json", code, resolve_roles=VIEWER
        )
    assert "already" in str(refused.value).lower()


def test_the_second_attempt_mints_nothing(tmp_path):
    """Refusing and minting anyway is the failure worth guarding separately: the
    refusal is what the caller sees, the key is what exists."""
    code = _invite(tmp_path)
    invitations.redeem_invitation(tmp_path / "invitations.json", code, resolve_roles=VIEWER)
    before = invitations.load_invitations(tmp_path / "invitations.json")[0]["redeemed_at"]
    with pytest.raises(invitations.InvitationRefused):
        invitations.redeem_invitation(
            tmp_path / "invitations.json", code, resolve_roles=VIEWER
        )
    after = invitations.load_invitations(tmp_path / "invitations.json")[0]["redeemed_at"]
    assert before == after, "a refused redemption rewrote the record"


def test_an_expired_invitation_is_refused(tmp_path):
    code = _invite(tmp_path, expires_in=timedelta(seconds=-1))
    with pytest.raises(invitations.InvitationRefused) as refused:
        invitations.redeem_invitation(
            tmp_path / "invitations.json", code, resolve_roles=VIEWER
        )
    assert "expired" in str(refused.value).lower()


def test_an_invitation_without_an_expiry_is_refused_at_creation(tmp_path):
    """A credential with no end is one nobody remembers to revoke."""
    with pytest.raises(ValueError) as refused:
        invitations.mint_invitation(
            principal="@someone:example",
            roles=("viewer",),
            scopes=("*:read",),
            site="example",
            name="",
            invited_by="@approver:example",
            expires_in=None,
        )
    assert "expir" in str(refused.value).lower()


def test_a_revoked_invitation_is_refused(tmp_path):
    code = _invite(tmp_path)
    stored = invitations.load_invitations(tmp_path / "invitations.json")
    invitations.revoke_invitation(tmp_path / "invitations.json", stored[0]["invitation_id"])
    with pytest.raises(invitations.InvitationRefused) as refused:
        invitations.redeem_invitation(
            tmp_path / "invitations.json", code, resolve_roles=VIEWER
        )
    assert "revoked" in str(refused.value).lower()


def test_an_unknown_code_is_refused(tmp_path):
    _invite(tmp_path)
    with pytest.raises(invitations.InvitationRefused):
        invitations.redeem_invitation(
            tmp_path / "invitations.json", "axi_inv_deadbeefdead_nonsense",
            resolve_roles=VIEWER,
        )


def test_a_wrong_secret_for_a_real_id_is_refused(tmp_path):
    """The id is in the code and the code is checked by hash, so knowing an id
    must not be enough."""
    code = _invite(tmp_path)
    head, _secret = code.rsplit("_", 1)
    with pytest.raises(invitations.InvitationRefused):
        invitations.redeem_invitation(
            tmp_path / "invitations.json", f"{head}_wrong", resolve_roles=VIEWER
        )


def test_each_refusal_says_which_one_it_is(tmp_path):
    """Specific, not uniform. The code is 32 bytes of entropy, so there is
    nothing to enumerate, and a vague error on a one-shot onboarding step is the
    dead end this whole programme exists to remove — a colleague who cannot tell
    "already used" from "expired" from "wrong code" has to ask somebody."""
    spent = _invite(tmp_path)
    invitations.redeem_invitation(tmp_path / "invitations.json", spent, resolve_roles=VIEWER)
    expired = _invite(tmp_path, expires_in=timedelta(seconds=-1))

    messages = []
    for code in (spent, expired, "axi_inv_000000000000_nope"):
        try:
            invitations.redeem_invitation(
                tmp_path / "invitations.json", code, resolve_roles=VIEWER
            )
        except invitations.InvitationRefused as exc:
            messages.append(str(exc).lower())
    assert len(messages) == 3
    assert len(set(messages)) == 3, f"two refusals read the same: {messages}"


# ---------------------------------------------------------------------------
# It can only shrink
# ---------------------------------------------------------------------------


def test_a_role_narrowed_after_the_invitation_narrows_the_key(tmp_path):
    """Applied without asking, because less is the safe direction."""
    code = _invite(tmp_path, scopes=("*:read", "*:invoke"))
    _, key_record, _ = invitations.redeem_invitation(
        tmp_path / "invitations.json", code, resolve_roles=_bundles(viewer=("*:read",))
    )
    assert key_record["scopes"] == ["*:read"]


def test_a_role_widened_after_the_invitation_is_refused(tmp_path):
    """The rule that makes an unredeemed invitation safe to leave lying around.

    Nobody approved the wider grant, so redeeming at that width would turn a
    change to a role into a change to every invitation anybody is still holding.
    """
    code = _invite(tmp_path, scopes=("*:read",))
    with pytest.raises(invitations.InvitationRefused) as refused:
        invitations.redeem_invitation(
            tmp_path / "invitations.json",
            code,
            resolve_roles=_bundles(viewer=("*:read", "*:govern")),
        )
    said = str(refused.value)
    assert "govern" in said, "the refusal does not name what it would have granted"
    assert "wider" in said.lower() or "more than" in said.lower()


def test_a_role_that_no_longer_exists_is_refused(tmp_path):
    code = _invite(tmp_path)
    with pytest.raises(invitations.InvitationRefused) as refused:
        invitations.redeem_invitation(
            tmp_path / "invitations.json", code, resolve_roles=_bundles(other=("*:read",))
        )
    assert "viewer" in str(refused.value)


def test_nothing_is_granted_that_the_invitation_did_not_name(tmp_path):
    """The ceiling is the recorded scopes, not the role's name. A role that
    resolves to something unrelated cannot slip a scope through."""
    code = _invite(tmp_path, scopes=("rag:read",))
    with pytest.raises(invitations.InvitationRefused):
        invitations.redeem_invitation(
            tmp_path / "invitations.json", code, resolve_roles=_bundles(viewer=("llm:invoke",))
        )


# ---------------------------------------------------------------------------
# Listing, for the administrator who has to clean up
# ---------------------------------------------------------------------------


def test_outstanding_invitations_can_be_listed_without_their_codes(tmp_path):
    _invite(tmp_path, principal="@someone:example")
    _invite(tmp_path, principal="@another:example")
    rows = invitations.outstanding(tmp_path / "invitations.json")
    assert {r["principal"] for r in rows} == {"@someone:example", "@another:example"}
    assert all("code_hash" not in r for r in rows), "a listing exposed the stored hashes"


def test_a_spent_invitation_leaves_the_outstanding_list(tmp_path):
    code = _invite(tmp_path)
    invitations.redeem_invitation(tmp_path / "invitations.json", code, resolve_roles=VIEWER)
    assert invitations.outstanding(tmp_path / "invitations.json") == []


def test_an_expired_invitation_is_reported_as_expired_not_outstanding(tmp_path):
    """Absence has kinds. An administrator reading the list needs to tell "still
    waiting on them" from "they missed it and need another"."""
    _invite(tmp_path, expires_in=timedelta(seconds=-1))
    assert invitations.outstanding(tmp_path / "invitations.json") == []
    rows = invitations.expired(tmp_path / "invitations.json")
    assert len(rows) == 1


def test_a_malformed_invitation_file_is_refused_rather_than_ignored(tmp_path):
    """Fail closed. Treating an unreadable file as "no invitations" would make
    every redemption fail with the wrong reason, and treating it as empty on
    write would discard the rest."""
    path = tmp_path / "invitations.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(invitations.InvitationsFileError):
        invitations.load_invitations(path)


def test_a_fresh_file_is_not_an_error(tmp_path):
    assert invitations.load_invitations(tmp_path / "nothing-here.json") == []
    assert invitations.outstanding(tmp_path / "nothing-here.json") == []


def test_expiry_is_stored_as_an_instant_not_a_duration(tmp_path):
    """A duration would restart every time the record was read."""
    _invite(tmp_path, expires_in=timedelta(days=7))
    row = invitations.load_invitations(tmp_path / "invitations.json")[0]
    when = datetime.fromisoformat(row["expires_at"])
    assert when > datetime.now(UTC)
    assert when - datetime.now(UTC) < timedelta(days=8)


# ---------------------------------------------------------------------------
# End to end, through the two commands a person actually runs.
# ---------------------------------------------------------------------------


def _gate(argv, keys: Path, invites: Path, capsys):
    """Run `axi gate …` and return (exit code, stdout)."""
    from axiom.extensions.builtins.webgate.cli import main as gate_main

    rc = gate_main(
        [
            "--json",
            "--keys-file",
            str(keys),
            *argv,
            "--invitations-file",
            str(invites),
        ]
    )
    return rc, capsys.readouterr().out


def test_an_administrator_invites_and_a_colleague_redeems(tmp_path, capsys):
    """The whole point, end to end: the key appears on the redeeming side."""
    import json as _json

    keys = tmp_path / "api-keys.json"
    invites = tmp_path / "invitations.json"

    rc, out = _gate(
        ["invite", "--principal", "@someone:example", "--role", "viewer",
         "--invited-by", "@approver:example", "--expires", "7d"],
        keys, invites, capsys,
    )
    assert rc == 0, out
    invited = _json.loads(out)["value"]
    code = invited["code"]

    # Nothing is a key yet: the administrator's output has no token in it.
    assert "axk_" not in out, "the invite command printed a key"
    assert not keys.exists() or keys.read_text(encoding="utf-8").strip() in ("", "[]"), (
        "inviting somebody minted a key, which is the thing it exists not to do"
    )

    rc, out = _gate(["redeem", code], keys, invites, capsys)
    assert rc == 0, out
    redeemed = _json.loads(out)["value"]
    assert redeemed["token"].startswith("axk_")
    assert redeemed["principal"] == "@someone:example"
    assert redeemed["invited_by"] == "@approver:example"
    assert "*:read" in redeemed["scopes"]


def test_the_key_the_colleague_minted_actually_opens_something(tmp_path, capsys):
    """A key nobody can use would satisfy every assertion above.

    Driven through the real bearer resolver and the real authz hook, which is
    what a request meets.
    """
    import json as _json

    from axiom.extensions.builtins.http.authz_hook import (
        build_authz_hook,
        build_bearer_resolver,
    )
    from axiom.governance import Decision, Verdict
    from axiom.webauth.api_keys import JsonFileApiKeyStore

    keys = tmp_path / "api-keys.json"
    invites = tmp_path / "invitations.json"
    _, out = _gate(
        ["invite", "--principal", "@someone:example", "--role", "viewer"], keys, invites, capsys
    )
    code = _json.loads(out)["value"]["code"]
    _, out = _gate(["redeem", code], keys, invites, capsys)
    token = _json.loads(out)["value"]["token"]

    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys)),
        decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "ok", "rcpt"),
    )
    from types import SimpleNamespace

    def request(method: str) -> SimpleNamespace:
        return SimpleNamespace(
            method=method,
            url=SimpleNamespace(path="/rag/search"),
            state=SimpleNamespace(mount_extension="rag"),
            headers={"authorization": f"Bearer {token}"},
        )

    assert hook(request("GET")).allow is True, "a viewer's key cannot read"
    assert hook(request("POST")).allow is False, "a viewer's key can write"


def test_redeeming_twice_through_the_cli_refuses_and_says_why(tmp_path, capsys):
    import json as _json

    keys = tmp_path / "api-keys.json"
    invites = tmp_path / "invitations.json"
    _, out = _gate(
        ["invite", "--principal", "@someone:example", "--role", "viewer"], keys, invites, capsys
    )
    code = _json.loads(out)["value"]["code"]
    assert _gate(["redeem", code], keys, invites, capsys)[0] == 0

    rc, out = _gate(["redeem", code], keys, invites, capsys)
    assert rc != 0
    assert "already redeemed" in out.lower()
    assert "new one" in out.lower(), "the refusal does not say what to do next"


def test_inviting_an_unknown_role_names_the_real_ones(tmp_path, capsys):
    rc, out = _gate(
        ["invite", "--principal", "@someone:example", "--role", "wizard"],
        tmp_path / "api-keys.json", tmp_path / "invitations.json", capsys,
    )
    assert rc != 0
    assert "wizard" in out
    assert "viewer" in out, "the refusal does not list the registered roles"


def test_an_unreadable_duration_names_the_accepted_forms(tmp_path, capsys):
    rc, out = _gate(
        ["invite", "--principal", "@someone:example", "--role", "viewer", "--expires", "soon"],
        tmp_path / "api-keys.json", tmp_path / "invitations.json", capsys,
    )
    assert rc != 0
    assert "7d" in out and "12h" in out


def test_an_invitation_with_no_grant_is_refused(tmp_path, capsys):
    rc, out = _gate(
        ["invite", "--principal", "@someone:example"],
        tmp_path / "api-keys.json", tmp_path / "invitations.json", capsys,
    )
    assert rc != 0
    assert "--role" in out
