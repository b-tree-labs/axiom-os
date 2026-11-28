# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit F4: conformance for log extensions, the author guide, and the field
log example.

Ticket: docs/working/logbook-units/F4.md in the product repo that holds the plan. A freshly
scaffolded log extension (`axi ext init <name> --template logbook`) lints and passes the doctor,
is discovered, is signed by a person through the library `axiom.attest` with the real service
and the test node signer, verifies, and is served over HTTP as the stored golden payloads say.
The guide (`docs/guides/write-a-logbook-extension.md`) prints only real commands, and the field
log example lists exactly the keys the guide says are declared but not yet enforced.

`AXIOM_UPDATE_GOLDEN=1` rewrites the goldens under `tests/logbook_acceptance/golden/`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.doctor import run_doctor
from axiom.cli.ext.commands.lint import lint_extension
from axiom.cli.ext.templates import get_template
from axiom.extensions.builtins.attest import registry, roles
from axiom.extensions.builtins.attest.logbooks import load_logbook
from axiom.extensions.builtins.attest.service import AttestRefused
from axiom.infra.principal import PrincipalContext

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden"
GUIDE = ROOT / "docs" / "guides" / "write-a-logbook-extension.md"
FIELD_LOG = ROOT / "docs" / "guides" / "examples" / "field_log"

NAME = "my_notes"
SITE = "site-a"
MEMBER = PrincipalContext(handle="@member1:site-a", posture="attested", assured=True)


def _scaffold(dest: Path) -> Path:
    get_template("logbook").create(
        dest / NAME, name=NAME, owner="example-lab", license="Apache-2.0", description=""
    )
    return dest / NAME


@pytest.fixture
def scaffolded(tmp_path):
    """A scaffolded log extension in a directory of its own (the extension root)."""
    roots = tmp_path / "extensions"
    roots.mkdir()
    return _scaffold(roots)


@pytest.fixture
def served(attest_db, scaffolded, monkeypatch, tmp_path):
    """The scaffolded log discovered on a node that serves one site, with a member."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", SITE)
    # The library's sign reads roles from the node's state dir; a directory of its own, so the
    # HTTP fixture's state (its own roles file) is left alone.
    state = tmp_path / "node-state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    registry.reset()
    registry.discover(roots=[scaffolded.parent])
    roles.grant(MEMBER.handle, SITE, "member", by="@admin:site-a", state_dir=state)
    return scaffolded


def _sign(signer, entry_type, meaning, content):
    """Draft, present, answer and sign through the library, as a person at the prompt."""
    from axiom.attest import confirm, drafts
    from axiom.attest import sign as library_sign
    from axiom.extensions.builtins.attest import signing

    d = drafts.create(
        logbook=NAME, entry_type=entry_type, content=content, origin="human",
        for_principal=MEMBER.handle, entered_by=MEMBER, meaning=meaning,
    )  # fmt: skip
    p = confirm.present(d.id, ["cli"])
    confirm.respond(p.id, principal=MEMBER, answer="sign", via="cli")
    saved = signing._provider
    signing._provider = lambda: signer
    try:
        return library_sign.sign(p.id, principal=MEMBER).record
    finally:
        signing._provider = saved


def _sign_three(signer):
    _sign(signer, "OPEN_PERIOD", "authored", {"title": "Start", "fields": {}})
    fields = {"reading": {"value": "4.2", "unit": "kg"}, "confirmed": True, "note": "fine"}
    _sign(signer, "CHECK", "performed", {"title": "Check", "fields": fields})
    _sign(signer, "NOTE", "authored", {"title": "Note", "fields": {"text": "all quiet"}})


# -- LB-F4-A1: the scaffold lints and passes the doctor -------------------------------------


@pytest.mark.acceptance("LB-F4-A1")
def test_lb_f4_a1_a_scaffolded_log_extension_lints_and_passes_the_doctor(scaffolded):
    errors = [f for f in lint_extension(scaffolded) if f.severity == "error"]
    assert errors == [], errors
    failed = [r for r in run_doctor(scaffolded, skip_tests=True) if not r.ok and not r.not_run]
    assert failed == [], failed


# -- LB-F4-A2: discovered, signed by a person, verified, read back --------------------------


@pytest.mark.acceptance("LB-F4-A2")
def test_lb_f4_a2_discovery_registers_it_and_a_person_signs_three_entries(served, node_signer, monkeypatch):
    from axiom.attest import query, verify
    from axiom.extensions.builtins.attest import signing

    assert registry.get(NAME).id == NAME
    monkeypatch.setattr(signing, "_keys", lambda: {node_signer.key_id: node_signer.public_bytes})
    _sign_three(node_signer)
    report = verify.chain(SITE, NAME)
    assert report.ok and report.head_seq == 3
    records = query.records(NAME)
    assert [r["entry_type"] for r in records] == ["OPEN_PERIOD", "CHECK", "NOTE"]
    assert {r["signer"]["principal"] for r in records} == {MEMBER.handle}


# -- LB-F4-A3: software proposes, a person signs --------------------------------------------


@pytest.mark.acceptance("LB-F4-A3")
def test_lb_f4_a3_an_agent_may_not_supply_the_observed_value_and_no_signing_skill_is_on_mcp(served):
    from axiom.attest import drafts
    from axiom.extensions.builtins.attest.skills import SPECS

    content = {"title": "Check", "fields": {"reading": {"value": "4.2", "unit": "kg"}}}
    with pytest.raises(AttestRefused, match="observe"):
        drafts.create(logbook=NAME, entry_type="CHECK", content=content, origin="agent:x",
                      for_principal=MEMBER.handle)  # fmt: skip
    signing = {"attest.new", "attest.sign", "attest.supplement", "attest.ack", "attest.cosign", "attest.seal"}
    for spec in SPECS:
        if spec.name in signing:
            assert not {"mcp", "agent_tool"} & set(spec.surfaces), spec.name
    reads = {s.name for s in SPECS if "mcp" in s.surfaces}
    assert {"attest.logbook_list", "attest.show", "attest.verify", "attest.obligations"} <= reads


@pytest.mark.acceptance("LB-F4-A3")
def test_lb_f4_a3_the_list_skill_names_the_scaffolded_log(served):
    from axiom.extensions.builtins.attest.skills import logbook

    listed = logbook.list_logbooks({}, None).value["logbooks"]
    assert NAME in {b["id"] for b in listed}


# -- LB-F4-A4: served over HTTP as the goldens say -------------------------------------------

_MASK = "<masked>"
_VOLATILE = {"attestation_id", "occurred_at", "recorded_at", "signed_at", "digest", "prev_digest", "created_at"}


def _mask(value):
    if isinstance(value, dict):
        return {k: (_MASK if k in _VOLATILE else _mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _golden(name: str, got) -> None:
    path = GOLDEN / name
    if os.environ.get("AXIOM_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    assert path.is_file(), f"no golden {path.name}: run with AXIOM_UPDATE_GOLDEN=1"
    assert got == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.acceptance("LB-F4-A4")
def test_lb_f4_a4_the_api_serves_the_log_and_its_records_as_the_goldens_say(
    served, client, node_signer, monkeypatch
):
    from axiom.extensions.builtins.attest import signing
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_

    monkeypatch.setattr(signing, "_keys", lambda: {node_signer.key_id: node_signer.public_bytes})
    _sign_three(node_signer)
    c = as_(client, OP1)
    book = c.get(f"/api/v1/attest/logbooks/{NAME}")
    assert book.status_code == 200, book.text
    _golden("scaffolded_logbook.json", _mask(book.json()))
    recs = c.get(f"/api/v1/attest/{NAME}/records", params={"site": SITE})
    assert recs.status_code == 200, recs.text
    body = recs.json()
    assert [r["entry_type"] for r in body["records"]] == ["OPEN_PERIOD", "CHECK", "NOTE"]
    _golden("records.json", _mask(body["records"]))
    ver = c.post(f"/api/v1/attest/{NAME}/verify", params={"site": SITE})
    assert ver.status_code == 200 and ver.json()["ok"] and ver.json()["checked"] == 3


# -- LB-F4-A5: the field log example ---------------------------------------------------------


def _guide_deferred_keys() -> set[str]:
    """The keys the guide's "Declared but not yet enforced" table lists (its first column)."""
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split("## Declared but not yet enforced", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([^`]+)` \|", section, re.M))


@pytest.mark.acceptance("LB-F4-A5")
def test_lb_f4_a5_the_field_log_lists_exactly_the_deferred_keys_the_guide_names():
    book = load_logbook(FIELD_LOG / "logbook" / "field_log.toml")
    assert book.id == "field_log"
    assert set(book.intervals) == {"season"}
    assert [t.id for t in book.types] == ["SEASON_OPEN", "ACTION", "OBSERVATION", "SEASON_CLOSE"]
    assert [o.id for o in book.obligations] == ["weekly_observation"]
    assert set(book.not_yet_enforced) == _guide_deferred_keys()
    assert book.not_yet_enforced, "the example shows the deferred keys on purpose"


@pytest.mark.acceptance("LB-F4-A5")
def test_lb_f4_a5_the_field_log_manifest_validates_and_names_no_partner():
    from axiom_tests._manifest import validate_manifest

    manifest = tomllib.loads((FIELD_LOG / "axiom-extension.toml").read_text(encoding="utf-8"))
    assert validate_manifest(manifest) == []
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_axiom_names_no_domain import DOMAIN_NOUNS, IDENTITIES
    finally:
        sys.path.pop(0)
    for path in [p for p in FIELD_LOG.rglob("*") if p.is_file()]:
        text = path.read_text(encoding="utf-8")
        for pattern in (IDENTITIES, DOMAIN_NOUNS):
            found = pattern.search(text)
            assert found is None, f"{path.relative_to(ROOT)} says {found.group(0)!r}"


# -- LB-F4-A6: every command the guide prints is real ---------------------------------------


def _guide_commands() -> list[list[str]]:
    text = GUIDE.read_text(encoding="utf-8")
    lines = []
    for block in re.findall(r"```(?:sh|bash)\n(.*?)```", text, re.S):
        lines += [ln.strip() for ln in block.splitlines() if ln.strip().startswith("axi ")]
    lines += re.findall(r"`(axi [^`]+)`", text)
    argvs = [shlex.split(ln.replace("<", "").replace(">", ""))[1:] for ln in lines]
    return [a for a in argvs if len(a) >= 2]  # a bare noun in prose ("after `axi log`") runs nothing


def _parses(parser, argv: list[str]) -> bool:
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code == 0
    return True


@pytest.mark.acceptance("LB-F4-A6")
def test_lb_f4_a6_every_command_the_guide_prints_is_a_real_command(served):
    from axiom.cli.ext.registry import discover_providers
    from axiom.extensions.builtins.attest import log_cli

    ext_verbs = set(discover_providers())
    commands = _guide_commands()
    assert len(commands) >= 6, "the guide prints the commands an author runs"
    for argv in commands:
        noun, rest = argv[0], argv[1:]
        if noun == "ext":
            assert rest and rest[0] in ext_verbs, argv
        elif noun == "log":
            if rest[0] in log_cli.NOUN_VERBS:
                assert _parses(log_cli.noun_parser(), rest), argv
            else:
                handle = rest[0]
                assert handle in (NAME.replace("_", "-"), "log", "field-log"), argv
                assert _parses(log_cli.log_parser(handle), rest[1:]), argv
        else:
            pytest.fail(f"the guide names `axi {noun}`, which this test does not know: {argv}")
