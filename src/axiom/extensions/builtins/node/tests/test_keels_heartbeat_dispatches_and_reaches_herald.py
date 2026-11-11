# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL's soundings run unattended, and a breach they find reaches HERALD.

Two separate failures hid behind one declared heartbeat. ``startup = "lazy"`` fails
``is_always_on``, so the Background Service never dispatched it. And the ``axi node`` CLI
bound only KEEL's own skills, so ``--alert`` found HERALD unreachable from the very
command the heartbeat runs.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.agents import background_service
from axiom.extensions.builtins.agents import consent as consent_mod
from axiom.extensions.builtins.node import collect
from axiom.extensions.builtins.node.collect import Collectors
from axiom.extensions.builtins.node.commands import cli
from axiom.extensions.builtins.node.skills import bind
from axiom.extensions.contracts import AgentConfig
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

MANIFEST = Path(__file__).resolve().parents[1] / "axiom-extension.toml"
FULL_DISK = "Filesystem 1-blocks Used Available Use% Mounted on\n/dev/sda1 1000 990 10 99% /\n"


def _agent() -> AgentConfig:
    a = tomllib.loads(MANIFEST.read_text())["agent"]
    return AgentConfig(
        startup=a["startup"],
        heartbeat_interval=a["heartbeat_interval"],
        heartbeat_command=a["heartbeat_command"],
    )


def _registry_with_herald(sent: list[dict]) -> SkillRegistry:
    reg = SkillRegistry()
    bind(reg)

    def alert(params, ctx):
        sent.append(params)
        return SkillResult(ok=True, value={"sent": True})

    reg.register_skill(
        SkillSpec(
            name="notifications.alert",
            fn=alert,
            description="alert",
            surfaces=("cli",),
            risk="reversible-write",
            side_effects=True,
        ),
        mutating=True,
    )
    return reg


def test_keel_is_registrable_on_a_fifteen_minute_heartbeat():
    a = _agent()
    assert a.is_always_on and a.is_registrable
    assert a.heartbeat_interval == 900 and a.heartbeat_command == "node vitals --alert --reap"


def test_the_service_dispatches_keels_heartbeat(tmp_path, monkeypatch):
    runs: list[list[str]] = []
    monkeypatch.setattr(
        background_service,
        "_run_heartbeat",
        lambda cmd, agent: runs.append(cmd) or SimpleNamespace(returncode=0),
    )
    store = background_service.StateStore(tmp_path / "state.json")
    ext = SimpleNamespace(name="node", agent=_agent())
    assert background_service.dispatch_due_agents([ext], store, "axi", enabled={"node"}) == ["node"]
    assert runs[-1][-4:] == ["node", "vitals", "--alert", "--reap"]
    # and not again until the next sounding is due
    assert background_service.dispatch_due_agents([ext], store, "axi", enabled={"node"}) == []


def test_a_consent_list_decided_before_keel_existed_still_dispatches_it(tmp_path, monkeypatch):
    """The real shape on a developer machine: core agents named, ``node`` never offered."""
    monkeypatch.setattr(consent_mod, "consent_path", lambda: tmp_path / "agents_consent.json")
    consent_mod.save_consent(
        consent_mod.AgentConsent(
            decided=True,
            enabled=["hygiene", "release", "diagnostics", "vault"],
            known=["diagnostics", "directory", "hygiene", "program", "release", "vault"],
        )
    )
    enabled = consent_mod.load_consent().enabled
    assert {"node", "hygiene", "diagnostics"} <= set(enabled)


def test_alert_on_the_cli_reaches_herald(tmp_path, monkeypatch):
    sent: list[dict] = []
    reg = _registry_with_herald(sent)
    monkeypatch.setattr("axiom.infra.skills_emit.bind_all_skills", lambda: reg)
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
    with collect.use(Collectors(df=lambda: FULL_DISK, gpus=lambda: "")):
        assert cli.main(["vitals", "--alert"]) == 0
    assert sent and sent[0]["priority"] == "high" and "critical" in sent[0]["summary"]


def test_the_whole_platform_binding_really_holds_herald():
    from axiom.infra.skills_emit import bind_all_skills

    assert bind_all_skills().has("notifications.alert")


def test_a_plain_reading_does_not_pay_for_binding_the_platform(tmp_path, monkeypatch):
    def boom():
        pytest.fail("a reading with no hand-off bound the whole platform")

    monkeypatch.setattr("axiom.infra.skills_emit.bind_all_skills", boom)
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
    logging.disable(logging.CRITICAL)
    try:
        with collect.use(Collectors(gpus=lambda: "")):
            assert cli.main(["vitals"]) == 0
    finally:
        logging.disable(logging.NOTSET)
