# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data orchestrator-install``: the one way a node runs PULSE's tick loop.

The orchestrator is the scheduler's only production host, and nothing started it
by default, so a cadence (a backup, a follow-up, a kit automation) registered on
a node without it never fired. The verb writes a per-user systemd unit that runs
it, refuses to start a second copy beside one that is already running, and never
writes a credential: the database settings stay in the env file the operator
already has, which the unit references.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.extensions.builtins.data_platform.services import orchestrator_unit as ou
from axiom.infra.skills import SkillContext, SkillRegistry


def _ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


def test_the_unit_runs_the_orchestrator_with_the_operators_env_file():
    text = ou.render_unit(python="/opt/venv/bin/python", env_file="/home/op/.config/node.env")
    assert "ExecStart=/opt/venv/bin/python -m axiom.extensions.builtins.data_platform.orchestration" in text
    assert "EnvironmentFile=/home/op/.config/node.env" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert "AXIOM_DB_URL=" not in text and "postgresql://" not in text, "no credential in the unit"
    assert "EnvironmentFile" not in ou.render_unit(python="/p", env_file=None)


def test_write_only_writes_the_unit_and_runs_nothing(tmp_path):
    calls = []
    r = ou.install(home=tmp_path, python="/p", env_file=None, write_only=True,
                   run=lambda argv: calls.append(argv) or (0, ""), running=lambda: [])
    unit = tmp_path / ".config" / "systemd" / "user" / ou.UNIT
    assert unit.is_file() and r["unit"] == str(unit)
    assert calls == [] and r["started"] is False
    assert any("systemctl --user enable --now" in s for s in r["next"])


def test_install_enables_and_starts_it(tmp_path):
    calls = []
    r = ou.install(home=tmp_path, python="/p", env_file=None, write_only=False,
                   run=lambda argv: calls.append(argv) or (0, ""), running=lambda: [])
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", ou.UNIT] in calls
    assert r["started"] is True


def test_it_refuses_a_second_orchestrator_beside_a_running_one(tmp_path):
    r = ou.install(home=tmp_path, python="/p", env_file=None, write_only=False,
                   run=lambda argv: (0, ""), running=lambda: [4242])
    assert r["started"] is False and r["already_running"] == [4242]
    assert not (tmp_path / ".config" / "systemd" / "user" / ou.UNIT).exists()
    assert "restart" in r["refused"]


def test_the_skill_names_a_missing_env_file(tmp_path, monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import orchestrator_install

    monkeypatch.setenv("HOME", str(tmp_path))
    r = orchestrator_install.run({"env_file": str(tmp_path / "nope.env"), "write_only": True}, _ctx(tmp_path))
    assert not r.ok and "nope.env" in r.errors[0]
    env = tmp_path / "node.env"
    env.write_text("AXIOM_DB_URL=postgresql://x\n")
    monkeypatch.setattr(ou, "running_orchestrators", lambda: [])
    r = orchestrator_install.run({"env_file": str(env), "write_only": True}, _ctx(tmp_path))
    assert r.ok, r.errors
    assert Path(r.value["unit"]).read_text().count(f"EnvironmentFile={env}") == 1
