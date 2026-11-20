# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit F2: the example log, a domain-free logbook extension anyone can copy.

Ticket: docs/working/logbook-units/F2.md in the product repo that holds the plan. The example lives
at `docs/guides/examples/example_logbook/`; `demo_log.toml` stays exactly as it is.

Records are signed here through `service`, because the library's signing path (`axiom.attest.sign`)
is F1b, which has not merged. Drafts go through `axiom.attest.drafts` and the chain is checked
with `axiom.attest.verify` (F1a). When F1b lands, `_sign` switches to `axiom.attest.sign`.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import obligations, registry, roles, service
from axiom.extensions.builtins.attest.logbooks import LogbookError, load_logbook
from axiom.extensions.builtins.attest.service import AttestRefused
from axiom.infra.principal import PrincipalContext, local_handle

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
EXAMPLE = ROOT / "docs" / "guides" / "examples" / "example_logbook"
LOGBOOK_FILE = EXAMPLE / "logbook" / "example_log.toml"
MANIFEST = EXAMPLE / "axiom-extension.toml"

SITE = "site-a"
LOGBOOK = "example_log"
MEMBER = PrincipalContext(handle="@member1:site-a", posture="attested", assured=True)
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)

#: Words a trade would use. The guard's nouns and partner ids come from the guard itself.
TRADE_WORDS = ("walkdown", "operator", "reactor", "nuclear")


@pytest.fixture
def example(attest_db, monkeypatch, tmp_path):
    """The example registered on a node that serves one site, with a member and a lead."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", SITE)
    # The library's sign looks roles up in the node's state dir, so the roles live there.
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    registry.register_file(LOGBOOK_FILE)
    roles.grant(MEMBER.handle, SITE, "member", by="@admin:site-a", state_dir=tmp_path)
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


def _sign(signer, entry_type, meaning, content, *, who=MEMBER, role="member", origin="human"):
    """Draft, present, answer and sign, all through the library (F1a and F1b). The node
    supplies the signer and the person's roles, so the role is granted where it looks."""
    from axiom.attest import confirm, drafts
    from axiom.attest import sign as library_sign
    from axiom.extensions.builtins.attest import signing
    from axiom.infra.paths import get_user_state_dir

    roles.grant(who.handle, SITE, role, by="@admin:site-a", state_dir=get_user_state_dir())
    d = drafts.create(
        logbook=LOGBOOK, entry_type=entry_type, content=content, origin=origin,
        for_principal=who.handle, entered_by=who if origin == "human" else None, meaning=meaning,
    )  # fmt: skip
    p = confirm.present(d.id, ["cli"])
    confirm.respond(p.id, principal=who, answer="sign", via="cli")
    saved = signing._provider, signing._keys
    signing._provider = lambda: signer
    try:
        return library_sign.sign(p.id, principal=who).record
    finally:
        signing._provider, signing._keys = saved


def _open(signer):
    return _sign(signer, "OPEN_PERIOD", "authored", {"title": "Start", "fields": {}})


def _check(signer):
    fields = {"reading": {"value": "4.2", "unit": "kg"}, "confirmed": True, "note": "fine"}
    return _sign(signer, "CHECK", "performed", {"title": "Check", "fields": fields})


# -- acceptance rows ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-F2-A1")
def test_f2_a1_the_example_loads_and_uses_only_enforced_keys():
    """The example loads with `load_logbook`, its id is `example_log`, and `not_yet_enforced` is
    empty, so the example is a true picture of what is in force."""
    book = load_logbook(LOGBOOK_FILE)
    assert book.id == LOGBOOK and book.display == "Example Log"
    assert book.not_yet_enforced == ()
    assert [t.id for t in book.types] == ["OPEN_PERIOD", "CHECK", "NOTE", "CLOSE_PERIOD"]
    assert book.posture == "attested"
    assert set(book.intervals) == {"period"}
    assert [o.id for o in book.obligations] == ["periodic_check"]
    assert {r for t in book.types for r in t.roles} == {"member", "lead"}
    reading = book.type("CHECK").field("reading")
    assert reading.type == "quantity" and reading.observe and reading.unit


@pytest.mark.acceptance("LB-F2-A2")
def test_f2_a2_the_manifest_validates_and_declares_the_logbook():
    """The manifest validates against the AEOS schema; its `provides` block is `kind = "logbook"`
    with the logbook's id and a `file` that exists."""
    from axiom_tests._manifest import validate_manifest

    manifest = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    assert validate_manifest(manifest) == []
    (block,) = [p for p in manifest["extension"]["provides"] if p["kind"] == "logbook"]
    assert block["id"] == load_logbook(LOGBOOK_FILE).id
    assert (EXAMPLE / block["file"]).is_file()
    assert registry.manifest_logbooks(MANIFEST) == [EXAMPLE / block["file"]]


@pytest.mark.acceptance("LB-F2-A3")
def test_f2_a3_discovery_registers_it_and_refuses_a_second_declaration(tmp_path, monkeypatch):
    """`registry.discover` over a directory holding a copy registers `example_log`; the same id from
    a different extension is refused (ids are unique per node)."""
    monkeypatch.setattr(registry, "_logbooks", {})
    monkeypatch.setattr(registry, "_discovered", False)
    one = tmp_path / "one"
    shutil.copytree(EXAMPLE, one / "example_logbook")
    registry.discover(roots=[one])
    assert registry.get(LOGBOOK).display == "Example Log"

    two = tmp_path / "two"
    shutil.copytree(EXAMPLE, two / "another_logbook")
    with pytest.raises(LogbookError, match="unique per node"):
        registry.discover(roots=[two])


@pytest.mark.acceptance("LB-F2-A4")
def test_f2_a4_the_example_signs_end_to_end(example, node_signer, monkeypatch):
    """Open a period, sign a check, sign a note, close the period; the chain verifies. A `CHECK`
    signed with no open period is refused."""
    from axiom.attest import query, verify
    from axiom.extensions.builtins.attest import signing

    monkeypatch.setattr(signing, "_keys", lambda: {node_signer.key_id: node_signer.public_bytes})
    with pytest.raises(AttestRefused):
        _check(node_signer)
    _open(node_signer)
    _check(node_signer)
    _sign(node_signer, "NOTE", "authored", {"title": "Note", "fields": {"text": "all quiet"}})
    lead = PrincipalContext(handle="@lead1:site-a", posture="attested", assured=True)
    _sign(node_signer, "CLOSE_PERIOD", "authored", {"title": "End", "fields": {}}, who=lead,
          role="lead")  # fmt: skip
    records = query.records(LOGBOOK)
    assert [r["entry_type"] for r in records] == ["OPEN_PERIOD", "CHECK", "NOTE", "CLOSE_PERIOD"]
    assert records[1]["interval"]["kind"] == "period"
    report = verify.chain(SITE, LOGBOOK)
    assert report.ok and report.head_seq == 4


@pytest.mark.acceptance("LB-F2-A5")
def test_f2_a5_software_may_not_supply_the_observed_value(example):
    """An agent origin offering a value for the observed field `reading` is refused (ADR-144)."""
    from axiom.attest import drafts

    content = {"title": "Check", "fields": {"reading": {"value": "4.2", "unit": "kg"}}}
    with pytest.raises(AttestRefused, match="observe"):
        drafts.create(logbook=LOGBOOK, entry_type="CHECK", content=content, origin="agent:x",
                      for_principal=MEMBER.handle)  # fmt: skip


@pytest.mark.acceptance("LB-F2-A6")
def test_f2_a6_the_example_names_no_trade():
    """No file in the example names a guard noun, a partner identity or a trade word."""
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_axiom_names_no_domain import DOMAIN_NOUNS, IDENTITIES
    finally:
        sys.path.pop(0)
    trade = re.compile("|".join(TRADE_WORDS), re.IGNORECASE)
    files = [p for p in EXAMPLE.rglob("*") if p.is_file()]
    assert {"axiom-extension.toml", "example_log.toml", "README.md"} <= {p.name for p in files}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in (DOMAIN_NOUNS, IDENTITIES, trade):
            found = pattern.search(text)
            assert found is None, f"{path.relative_to(ROOT)} says {found.group(0)!r}"


@pytest.mark.acceptance("LB-F2-A7")
def test_f2_a7_the_obligation_is_due_at_the_default_cadence_and_met_after_a_check(
    example, node_signer, clock
):
    """With a period open and no check, `periodic_check` is due 60 minutes after the period opened;
    after a check it is met and the next one is due 60 minutes after it."""
    assert obligations.evaluate(SITE, LOGBOOK, now=T0) == []
    _open(node_signer)
    (due,) = obligations.evaluate(SITE, LOGBOOK, now=T0 + timedelta(minutes=10))
    assert due["obligation"] == "periodic_check" and due["type"] == "CHECK"
    assert due["state"] == "ok" and due["last_met_at"] is None
    assert due["due_at"] == T0 + timedelta(minutes=60)
    assert due["notify"] == ["member"] and due["severity"] == "alarm"
    assert (
        obligations.evaluate(SITE, LOGBOOK, now=T0 + timedelta(minutes=61))[0]["state"] == "missed"
    )
    clock["t"] = T0 + timedelta(minutes=30)
    _check(node_signer)
    (met,) = obligations.evaluate(SITE, LOGBOOK, now=T0 + timedelta(minutes=61))
    assert met["state"] == "ok" and met["last_met_at"] == T0 + timedelta(minutes=30)
    assert met["due_at"] == T0 + timedelta(minutes=90)


# -- A8: a person signs the example at the command line -------------------------------------

#: Runs the real `axi attest` command in a child process with the person's key held in memory, so
#: the developer's keychain is never read or written, and the node key a throwaway one.
_CHILD = textwrap.dedent(
    """
    import sys
    from axiom.attest.chain import Ed25519Signer
    from axiom.extensions.builtins.attest import cli, signing
    from axiom.vega.identity import custody, local
    from axiom.vega.identity.keypair import generate_keypair

    local.KeychainCustody = custody.InMemoryCustody
    node = Ed25519Signer(key_id="node:example", keypair=generate_keypair())
    signing.set_provider(lambda: node)
    sys.exit(cli.main(sys.argv[1:]))
    """
)


def _axi_attest_new(project: Path, state: Path, posture: str) -> tuple[int, str]:
    """`axi attest new --logbook example_log --type OPEN_PERIOD ...`, answering `sign` at a terminal."""
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "AXIOM_ROOT": str(project),
        "AXI_STATE_DIR": str(state),
        "AXIOM_SERVED_SITES": SITE,
        "AXIOM_IDENTITY_POSTURE": posture,
    }
    argv = ["new", "--logbook", LOGBOOK, "--type", "OPEN_PERIOD", "--meaning", "authored",
             "--title", "Start", "--json"]  # fmt: skip
    leader, follower = pty.openpty()
    try:
        os.write(leader, b"sign\n")
        done = subprocess.run(
            [sys.executable, "-c", _CHILD, *argv], stdin=follower, cwd=project, env=env,
            capture_output=True, text=True, timeout=180,
        )  # fmt: skip
    finally:
        os.close(follower)
        os.close(leader)
    return done.returncode, done.stdout + done.stderr


@pytest.mark.acceptance("LB-F2-A8")
def test_f2_a8_a_person_signs_the_example_at_the_command_line(attest_db, tmp_path):
    """The floor is `attested`, so `axi attest new` at posture `attested` signs; at the default
    `open` posture it is refused with `posture 'open' cannot sign`."""
    assert load_logbook(LOGBOOK_FILE).posture == "attested"
    project = tmp_path / "project"
    shutil.copytree(EXAMPLE, project / ".axi" / "extensions" / "example_logbook")
    state = tmp_path / "state"
    roles.grant(local_handle(), SITE, "member", by="@admin:site-a", state_dir=state)

    code, out = _axi_attest_new(project, state, "open")
    assert code == 1, out
    assert "posture 'open' cannot sign" in out

    code, out = _axi_attest_new(project, state, "attested")
    assert code == 0, out
    assert '"status": "signed"' in out
    (record,) = service.records(SITE, LOGBOOK)
    assert record["entry_type"] == "OPEN_PERIOD"
    assert record["signer"]["principal"] == local_handle()
    assert record["assurance"]["posture"] == "attested"
