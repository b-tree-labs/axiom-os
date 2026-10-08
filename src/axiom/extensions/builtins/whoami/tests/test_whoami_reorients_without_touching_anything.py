# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``whoami`` tells someone where they are without changing anything or leaking anything.

The facts it reports were scattered across five commands and two files, and
three of them disagreed on the machine where it was written: the acting
principal came from the OS user while the identity file named someone else,
the install record said one version while another ran from a checkout, and
nothing said either. These tests pin what makes the report safe to call at any
time, including by an agent: it never creates a keypair, never reads a secret's
value, never prints an environment value, and one broken source never hides
the rest.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.whoami import cli, sections
from axiom.extensions.builtins.whoami.skills import bind, run
from axiom.infra.skills import SkillContext, SkillRegistry

QUICK = tuple(s for s in sections.ALL if s != "llm")


@pytest.fixture
def home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "identity").mkdir(parents=True)
    (state / "identity" / "identity.json").write_text(
        json.dumps(
            {
                "owner": "someone@example.org",
                "aliases": ["alias@example.org"],
                "display_name": "laptop:someone",
                "node_id": "abc123",
                "profile": "standard",
                "private_key_path": "/never/printed",
            }
        )
    )
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.delenv("AXIOM_IDENTITY_POSTURE", raising=False)
    monkeypatch.chdir(tmp_path)
    return state


def test_the_default_report_answers_the_orienting_questions(home):
    report = sections.collect(sections.DEFAULT)
    assert set(sections.DEFAULT) <= set(report)
    assert report["you"]["owner"] == "someone@example.org"
    assert report["node"]["node_id"] == "abc123"
    assert isinstance(report["warnings"], list)


def test_acting_as_someone_other_than_the_identity_owner_is_said(home):
    report = sections.collect(("you",))
    assert any("not as the identity owner someone@example.org" in w for w in report["warnings"])


def test_an_attested_posture_is_reported_without_touching_the_keychain(home, monkeypatch):
    monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "attested")

    def refuse(*a, **k):
        raise AssertionError("whoami must not load or create a keypair")

    import axiom.vega.identity.local as local

    monkeypatch.setattr(local, "load_or_create_local_keypair", refuse)
    you = sections.collect(("you",))["you"]
    assert you["posture"] == "attested" and "not resolved" in you["principal"]


def test_no_secret_value_or_environment_value_appears(home, monkeypatch):
    store = ForeignCredentialStore(home)
    store.set("a-credential", b"SENTINEL-SECRET-VALUE")
    monkeypatch.setenv("AXIOM_SOMETHING_SECRET", "SENTINEL-ENV-VALUE")
    text = json.dumps(sections.collect(QUICK), default=str)
    assert "SENTINEL-SECRET-VALUE" not in text and "SENTINEL-ENV-VALUE" not in text
    assert "AXIOM_SOMETHING_SECRET" in text  # the name is shown, the value never
    assert "/never/printed" not in text  # the private key's path is not identity


def test_an_expired_credential_is_a_warning(home):
    store = ForeignCredentialStore(home)
    past = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    store.set("old-token", b"x", expires_at=past)
    report = sections.collect(("access", "node"))
    assert report["access"]["expired"] == ["old-token"]
    assert any("old-token" in w for w in report["warnings"])


def test_one_broken_source_never_hides_the_rest(home, monkeypatch):
    def broken():
        raise RuntimeError("source unavailable")

    monkeypatch.setitem(sections.BUILTIN, "node", broken)
    report = sections.collect(("you", "node", "site"))
    assert "source unavailable" in report["node"]["error"]
    assert report["you"]["owner"] == "someone@example.org" and "site" in report


def test_a_consumer_can_add_a_section_without_the_platform_naming_it(home, monkeypatch):
    monkeypatch.setattr(
        sections, "contributed", lambda: [("binding", lambda: ("Site binding", {"bound": "x"}))]
    )
    assert sections.collect(("you",))["Site binding"] == {"bound": "x"}


def test_the_skill_refuses_an_unknown_section(home):
    ctx = SkillContext(registry=None, state_dir=home, logger=logging.getLogger("t"))
    result = run({"sections": ["nope"]}, ctx)
    assert not result.ok and "nope" in result.errors[0]


def test_a_flag_narrows_the_report_to_its_section(home, capsys):
    assert cli.main(["--identity"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("You") and "This node" not in out


def test_json_is_one_object(home, capsys):
    assert cli.main(["--json", "--node"]) == 0
    assert json.loads(capsys.readouterr().out)["node"]["node_id"] == "abc123"


def test_the_skill_is_read_only_and_reaches_agents():
    registry = SkillRegistry()
    bind(registry)
    spec = registry.spec("whoami.report")
    assert spec.side_effects is False and {"mcp", "agent_tool"} <= set(spec.surfaces)


def test_a_warning_is_only_about_a_section_that_was_read(home, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(home.parent / "empty"))
    monkeypatch.setenv("HOME", str(home.parent / "empty-home"))  # no fallback identity either
    assert sections.collect(("env",))["warnings"] == []
    assert any("no node identity" in w for w in sections.collect(("node",))["warnings"])
