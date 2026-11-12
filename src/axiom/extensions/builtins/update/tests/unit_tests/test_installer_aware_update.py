# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`update` upgrades with the tool that installed the package, not always pip.

Live failure: a `uv tool install` environment has no pip, so the upgrade died
with "No module named pip". `--check` also said "Everything up to date" while a
newer release existed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from axiom.extensions.builtins.update import installer
from axiom.extensions.builtins.update.cli import Updater
from axiom.extensions.builtins.update.installer import plan_upgrade
from axiom.extensions.builtins.update.version_check import VersionInfo

DIST = "example-app"
EXE = "/x/bin/python"


@pytest.fixture(autouse=True)
def _tools_on_path(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda t: f"/usr/bin/{t}")


def _uv_tool_prefix(tmp_path, extras=()):
    prefix = tmp_path / "uv" / "tools" / DIST
    prefix.mkdir(parents=True)
    ex = f", extras = {list(extras)!r}".replace("'", '"') if extras else ""
    (prefix / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "{DIST}"{ex} }}]\n'
    )
    return prefix


def test_uv_tool_env_uses_uv_tool_upgrade_not_pip(tmp_path):
    plan = plan_upgrade(DIST, executable=EXE, prefix=_uv_tool_prefix(tmp_path), has_pip=False)
    assert plan.method == "uv-tool"
    assert plan.cmd == ["uv", "tool", "upgrade", DIST]


def test_uv_tool_receipt_extras_are_read(tmp_path):
    prefix = _uv_tool_prefix(tmp_path, extras=["telemetry"])
    assert installer.installed_extras(DIST, prefix) == ["telemetry"]
    plan = plan_upgrade(
        DIST, source_spec=f"{DIST} @ git+https://h/r", executable=EXE,
        prefix=prefix, has_pip=False,
    )
    assert plan.cmd == [
        "uv", "tool", "install", "--force", f"{DIST}[telemetry] @ git+https://h/r"
    ]


def test_pipx_env(tmp_path):
    prefix = tmp_path / "pipx" / "venvs" / DIST
    prefix.mkdir(parents=True)
    plan = plan_upgrade(DIST, executable=EXE, prefix=prefix, has_pip=True, extras=[])
    assert plan.cmd == ["pipx", "upgrade", DIST]


def test_conda_or_plain_venv_with_pip_keeps_extras(tmp_path):
    plan = plan_upgrade(
        DIST, executable=EXE, prefix=tmp_path / "envs" / "conda", has_pip=True,
        extras=["telemetry", "x"],
    )
    assert plan.method == "pip"
    assert plan.cmd[:5] == [EXE, "-m", "pip", "install", "--upgrade"]
    assert f"{DIST}[telemetry,x]" in plan.cmd


def test_venv_without_pip_but_uv_on_path(tmp_path):
    plan = plan_upgrade(DIST, executable=EXE, prefix=tmp_path / "venv", has_pip=False, extras=["t"])
    assert plan.method == "uv-pip"
    assert plan.cmd[:5] == ["uv", "pip", "install", "--python", EXE]
    assert f"{DIST}[t]" in plan.cmd


def test_unknown_prints_a_command_and_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda t: None)
    plan = plan_upgrade(DIST, executable=EXE, prefix=tmp_path / "venv", has_pip=False, extras=[])
    assert plan.cmd is None and DIST in plan.manual


@dataclass
class _Done:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _updater(tmp_path, monkeypatch, plan):
    monkeypatch.setattr(installer, "plan_upgrade", plan_upgrade)  # same module object
    monkeypatch.setattr(
        "axiom.extensions.builtins.update.installer.plan_upgrade", lambda *a, **k: plan
    )
    return Updater(repo_root=tmp_path)


def test_update_runs_the_planned_command_with_no_pip_involved(tmp_path, monkeypatch):
    plan = installer.InstallPlan("uv-tool", ["uv", "tool", "upgrade", DIST], "uv tool upgrade example-app")
    u = _updater(tmp_path, monkeypatch, plan)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: ran.append(cmd) or _Done())
    versions = iter(["1.17.5", "1.17.6"])
    monkeypatch.setattr(u, "_installed_version", lambda: next(versions))
    u._update_deps()
    assert ran == [["uv", "tool", "upgrade", DIST]]
    assert u.results[-1].success and u.results[-1].changed


def test_unupgradable_install_names_method_and_command_without_traceback(tmp_path, monkeypatch):
    plan = installer.InstallPlan("unknown", None, "uv pip install --python /x --upgrade example-app")
    u = _updater(tmp_path, monkeypatch, plan)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    u._update_deps()
    r = u.results[-1]
    assert not r.success
    assert "unknown" in r.message and "uv pip install --python /x" in r.message
    assert "Traceback" not in r.message + r.details


def test_failed_upgrade_includes_manual_command(tmp_path, monkeypatch):
    plan = installer.InstallPlan("pipx", ["pipx", "upgrade", DIST], "pipx upgrade example-app")
    u = _updater(tmp_path, monkeypatch, plan)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done(1, stderr="boom"))
    monkeypatch.setattr(u, "_installed_version", lambda: "1.0")
    u._update_deps()
    r = u.results[-1]
    assert not r.success and "pipx" in r.message and "pipx upgrade example-app" in r.details


def test_editable_checkout_is_not_sent_through_a_tool_upgrade(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / ".git").mkdir()
    called = []
    monkeypatch.setattr(
        "axiom.extensions.builtins.update.installer.plan_upgrade",
        lambda *a, **k: called.append(1),
    )
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _Done())
    monkeypatch.setattr(Updater, "_installed_version", lambda self: "1")
    u = Updater(repo_root=tmp_path)
    u._update_deps()
    assert not called


# --- `--check` must notice a newer release --------------------------------


def _check(tmp_path, monkeypatch, info):
    monkeypatch.setattr(
        "axiom.extensions.builtins.update.version_check.VersionChecker.check_remote_version",
        lambda self, timeout=5.0: info,
    )
    plan = installer.InstallPlan("uv-tool", ["uv", "tool", "upgrade", DIST], "uv tool upgrade example-app")
    monkeypatch.setattr(
        "axiom.extensions.builtins.update.installer.plan_upgrade", lambda *a, **k: plan
    )
    u = Updater(repo_root=tmp_path, dry_run=True)
    u._check_package_version()
    return u


def test_check_reports_newer_version_method_and_command(tmp_path, monkeypatch):
    u = _check(tmp_path, monkeypatch, VersionInfo("1.17.5", "1.17.6", True, "now", "pypi"))
    r = u.results[-1]
    assert r.changed and "1.17.5 -> 1.17.6" in r.message and "uv tool" in r.message
    assert "uv tool upgrade example-app" in r.details
    assert "up to date" not in u.summary()


def test_check_says_latest_only_when_it_is(tmp_path, monkeypatch):
    u = _check(tmp_path, monkeypatch, VersionInfo("1.17.6", "1.17.6", False, "now", "pypi"))
    assert not u.results[-1].changed and "latest" in u.results[-1].message
    assert "up to date" in u.summary()


def test_check_that_cannot_reach_the_source_does_not_claim_latest(tmp_path, monkeypatch):
    u = _check(tmp_path, monkeypatch, VersionInfo("1.17.5", None, False, "now", "pypi"))
    assert "could not" in u.results[-1].message
