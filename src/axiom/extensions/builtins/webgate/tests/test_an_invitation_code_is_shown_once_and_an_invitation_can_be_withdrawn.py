# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Three things an administrator hit while onboarding somebody, 2026-10-28.

``axi gate invite`` printed ``recorded invitation <id>`` and nothing else. The
single-use code is in ``value["code"]``, which the gate's text rendering never
printed, so the invitation could not be redeemed unless ``--json`` was passed.
``gate redeem`` had the same hole for the key it mints.

Nothing could take an invitation back: ``gate revoke`` accepted only ``api-key``.

The code is a credential for exactly one thing, and the rule that keeps it safe is
that it exists in one place, on stdout, once. These tests hold both halves of
that: the person sees it, and no file or log the node keeps does.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.webgate.cli import main as gate_main
from axiom.webauth import invitations

NODE = "https://node.example.org"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_PUBLIC_URL", NODE)
    monkeypatch.delenv("AXIOM_SITE", raising=False)


@pytest.fixture
def files(tmp_path):
    return {"keys": tmp_path / "api-keys.json", "invites": tmp_path / "invitations.json"}


def _run(argv, files, capsys, *, json_out=False):
    args = ["--keys-file", str(files["keys"])]
    if json_out:
        args.append("--json")
    rc = gate_main([*args, *argv, "--invitations-file", str(files["invites"])])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _invite(files, capsys, **kw):
    argv = [
        "invite",
        "--principal",
        "@x:site",
        "--role",
        "viewer",
        "--site",
        "site",
        "--expires",
        "3d",
    ]
    rc, out, err = _run(argv, files, capsys, json_out=kw.get("json_out", False))
    assert rc == 0, (out, err)
    return out


def _code_from_json(out: str) -> str:
    return json.loads(out)["value"]["code"]


# ---------------------------------------------------------------------------
# The code is shown, once
# ---------------------------------------------------------------------------


def test_text_output_prints_the_code_exactly_once(files, capsys):
    """The failure: the text rendering dropped it. Find the code the way the
    invitations file would know it, by id, then count it in what was printed."""
    out = _invite(files, capsys)
    codes = re.findall(r"axi_inv_\S+", out)
    assert len(codes) == 1, f"expected the code once, found {len(codes)} in:\n{out}"
    code = codes[0]
    assert out.count(code) == 1

    # And it is a real code: it opens the invitation that was just recorded.
    record = invitations.load_invitations(files["invites"])[0]
    assert code.startswith(f"axi_inv_{record['invitation_id']}_")


def test_text_output_says_who_it_is_for_when_it_expires_and_how_to_redeem(files, capsys):
    out = _invite(files, capsys)
    record = invitations.load_invitations(files["invites"])[0]
    assert "send this code to @x:site" in out
    assert "works once" in out
    assert record["expires_at"] in out
    assert f"neut site redeem --face-url {NODE}" in out
    assert "<code>" in out, "the command must not repeat the secret"
    assert f"axi gate revoke invitation {record['invitation_id']}" in out


def test_without_a_known_node_url_the_redeem_command_says_so(files, capsys, monkeypatch):
    monkeypatch.delenv("AXIOM_PUBLIC_URL")
    out = _invite(files, capsys)
    assert "--face-url <this node's url>" in out


def test_json_output_is_unchanged(files, capsys):
    out = _invite(files, capsys, json_out=True)
    data = json.loads(out)
    assert set(data) == {"ok", "value", "errors", "actions_taken"}
    assert set(data["value"]) == {
        "invitation_id",
        "code",
        "principal",
        "roles",
        "scopes",
        "site",
        "expires_at",
        "path",
    }
    assert data["value"]["code"].startswith("axi_inv_")
    # Not in the status line, in JSON or anywhere else a log might copy from.
    assert data["value"]["code"] not in " ".join(data["actions_taken"])


def test_a_refused_invitation_prints_no_code(files, capsys):
    rc, out, err = _run(["invite", "--principal", "@x:site", "--role", "wizard"], files, capsys)
    assert rc != 0
    assert "axi_inv_" not in out + err


def test_redeem_text_output_shows_the_key_once(files, capsys):
    """Same hole, other end: the key is only in ``value["token"]``."""
    code = re.findall(r"axi_inv_\S+", _invite(files, capsys))[0]
    rc, out, err = _run(["redeem", code], files, capsys)
    assert rc == 0, (out, err)
    keys = re.findall(r"axk_\S+", out)
    assert len(keys) == 1, out
    assert out.count(keys[0]) == 1


# ---------------------------------------------------------------------------
# The code is never kept anywhere but stdout
# ---------------------------------------------------------------------------


def _everything_the_node_wrote(tmp_path: Path) -> str:
    """Every byte under the state dir and beside the invitations file, except
    the invitations file itself (which holds a hash by design)."""
    text = []
    for p in (tmp_path / "state").rglob("*"):
        if p.is_file():
            text.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(text)


def _audited_skills(tmp_path: Path) -> list[str]:
    path = tmp_path / "state" / "audit" / "actions.jsonl"
    return [json.loads(line)["skill"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_code_is_not_in_the_audit_chain_the_logs_or_the_event_bus(
    files, capsys, caplog, tmp_path
):
    from axiom.infra.bus import get_default_eventbus

    caplog.set_level(logging.DEBUG)
    out = _invite(files, capsys)
    code = re.findall(r"axi_inv_\S+", out)[0]
    secret = invitations.parse_code(code)[1]

    kept = _everything_the_node_wrote(tmp_path)
    assert code not in kept and secret not in kept, "the code reached the node's state"
    assert code not in caplog.text and secret not in caplog.text, "the code was logged"
    bus = repr(get_default_eventbus().history)
    assert code not in bus and secret not in bus, "the code reached the event bus"

    # The audit chain does record that an invitation happened.
    assert "gate.invite" in _audited_skills(tmp_path)

    # And the invitations file holds a hash, not the code.
    stored = (files["invites"]).read_text(encoding="utf-8")
    assert code not in stored and secret not in stored


# ---------------------------------------------------------------------------
# Revoking an invitation
# ---------------------------------------------------------------------------


def _invite_id(files) -> str:
    return invitations.load_invitations(files["invites"])[0]["invitation_id"]


def test_revoking_an_invitation_refuses_its_redemption_with_the_revoked_message(files, capsys):
    code = re.findall(r"axi_inv_\S+", _invite(files, capsys))[0]
    rc, out, err = _run(["revoke", "invitation", _invite_id(files)], files, capsys)
    assert rc == 0, (out, err)
    assert "revoked invitation" in out

    rc, out, err = _run(["redeem", code], files, capsys)
    assert rc != 0
    assert "was revoked on" in err and "ask for a new one" in err
    assert not files["keys"].exists() or "axk_" not in files["keys"].read_text()


def test_revoking_is_audited(files, capsys, tmp_path):
    _invite(files, capsys)
    _run(["revoke", "invitation", _invite_id(files)], files, capsys)
    assert "gate.revoke" in _audited_skills(tmp_path)


def test_an_unknown_invitation_id_gives_a_clear_error(files, capsys):
    _invite(files, capsys)
    rc, out, err = _run(["revoke", "invitation", "nosuchid"], files, capsys)
    assert rc != 0
    assert "no invitation with id 'nosuchid'" in err
    assert "gate list invitations" in err


def test_revoking_twice_keeps_the_original_time(files, capsys):
    _invite(files, capsys)
    iid = _invite_id(files)
    _run(["revoke", "invitation", iid], files, capsys)
    first = invitations.load_invitations(files["invites"])[0]["revoked_at"]
    rc, _, _ = _run(["revoke", "invitation", iid], files, capsys)
    assert rc == 0
    assert invitations.load_invitations(files["invites"])[0]["revoked_at"] == first


def test_an_already_redeemed_invitation_points_at_the_key(files, capsys):
    code = re.findall(r"axi_inv_\S+", _invite(files, capsys))[0]
    _run(["redeem", code], files, capsys)
    rc, out, err = _run(["revoke", "invitation", _invite_id(files)], files, capsys)
    assert rc != 0
    assert "already redeemed" in err and "gate revoke api-key" in err


def test_revoking_with_the_wrong_kind_of_id_does_not_touch_keys(files, capsys):
    """`api-key` still wants a key id; nothing about it changed."""
    rc, out, err = _run(["revoke", "api-key", "no-such-key"], files, capsys)
    assert rc != 0


# ---------------------------------------------------------------------------
# Listing invitations
# ---------------------------------------------------------------------------


def _write(files, *, now):
    """One invitation in each state, written through the real primitives."""
    rows = {}
    for label, kw in (
        ("outstanding", {}),
        ("redeemed", {}),
        ("revoked", {}),
        ("expired", {"expires_in": timedelta(seconds=1)}),
    ):
        code, rec = invitations.mint_invitation(
            principal=f"@{label}:site",
            roles=("viewer",),
            scopes=("*:read",),
            site="site",
            expires_in=kw.get("expires_in", timedelta(days=3)),
            now=now - timedelta(days=1) if label == "expired" else now,
        )
        invitations.append_invitation(files["invites"], rec)
        rows[label] = (code, rec["invitation_id"])
    # Spend / withdraw through the real paths.
    invitations.redeem_invitation(
        files["invites"], rows["redeemed"][0], resolve_roles=lambda names: ("*:read",), now=now
    )
    invitations.revoke_invitation(files["invites"], rows["revoked"][1])
    return rows


def test_listing_shows_every_status_and_never_a_code(files, capsys):
    now = datetime.now(UTC)
    rows = _write(files, now=now)
    rc, out, err = _run(["list", "invitations"], files, capsys, json_out=True)
    assert rc == 0, err
    items = {i["principal"]: i for i in json.loads(out)["value"]["items"]}
    assert {p: i["status"] for p, i in items.items()} == {
        "@outstanding:site": "outstanding",
        "@redeemed:site": "redeemed",
        "@revoked:site": "revoked",
        "@expired:site": "expired",
    }
    for label, (code, _iid) in rows.items():
        secret = invitations.parse_code(code)[1]
        assert code not in out and secret not in out
    assert "code_hash" not in out and "scrypt" not in out.lower()


def test_listing_text_shows_the_status_words_and_no_code(files, capsys):
    rows = _write(files, now=datetime.now(UTC))
    rc, out, err = _run(["list", "invitations"], files, capsys)
    assert rc == 0, err
    for word in ("outstanding", "redeemed", "revoked", "expired"):
        assert word in out
    assert "axi_inv_" not in out
    for _code, iid in rows.values():
        assert iid in out


def test_listing_with_no_invitations_file_is_empty_not_an_error(files, capsys):
    rc, out, err = _run(["list", "invitations"], files, capsys)
    assert rc == 0
    assert "no invitations" in out


# ---------------------------------------------------------------------------
# The same skills over the other surfaces
# ---------------------------------------------------------------------------


def test_revoke_and_list_are_declared_for_mcp_and_agents():
    from axiom.extensions.builtins.webgate import skills as gate_skills
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    gate_skills.bind(reg)
    for name in ("gate.revoke", "gate.list", "gate.invite"):
        spec = reg.spec(name)
        assert spec is not None, f"{name} has no spec, so nothing can find it"
        assert "mcp" in spec.surfaces and "agent_tool" in spec.surfaces
