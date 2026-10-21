# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`update` must report the version it ended on, not the one it hoped for.

`_update_deps` decided whether anything changed by looking for the string
"Successfully installed" in pip's stdout. Every install path in
`_resolve_install_target` passes `-q`, and measured against real pip, `-q`
produces *zero bytes* of output — so that substring can never appear and
`changed` was always False. An operator who upgraded successfully was told
"Dependencies already current".

That is the benign half. The damaging half is that pip exits 0 when the
resolver picks an *older* version than the one on the index — a pin, a
yanked release, a platform with no wheel for the newest build. The update
then reports success while leaving the operator on the version they were
trying to get off, with nothing in the output to say so.

The only honest answer comes from asking what is installed afterwards, and
from a subprocess: this process imported the old module at startup and will
keep reporting the old version for its whole life.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from axiom.extensions.builtins.update.cli import Updater


@dataclass
class _FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def updater(tmp_path):
    # No .git, no pyproject -> the packaged (pypi) path, not editable.
    return Updater(repo_root=tmp_path)


def _run_update(updater, monkeypatch, *, install, versions):
    """Drive _update_deps with a scripted pip and a scripted version probe."""
    seen = iter(versions)

    def fake_run(cmd, **kwargs):
        return install

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        updater, "_installed_version", lambda: next(seen, versions[-1])
    )
    updater._update_deps()
    return updater.results[-1]


def test_a_quiet_pip_that_really_upgraded_is_reported_as_changed(updater, monkeypatch):
    """The regression: -q means no output to match on, so ask the package."""
    result = _run_update(
        updater,
        monkeypatch,
        install=_FakeCompleted(returncode=0, stdout="", stderr=""),
        versions=["1.9.0", "1.11.2"],
    )
    assert result.success
    assert result.changed, "an upgrade with no pip output was reported as no-op"
    assert "1.11.2" in result.message


def test_an_upgrade_that_moved_backwards_is_not_a_success(updater, monkeypatch):
    """pip exits 0 having resolved to an older version. That is not updated."""
    result = _run_update(
        updater,
        monkeypatch,
        install=_FakeCompleted(returncode=0, stdout="", stderr=""),
        versions=["1.11.2", "1.9.0"],
    )
    assert not result.success, "a downgrade was reported as a successful update"
    assert "1.9.0" in result.message or "1.9.0" in result.details


def test_genuinely_current_is_still_reported_as_current(updater, monkeypatch):
    """The no-op case must stay a quiet, honest no-op."""
    result = _run_update(
        updater,
        monkeypatch,
        install=_FakeCompleted(returncode=0, stdout="", stderr=""),
        versions=["1.11.2", "1.11.2"],
    )
    assert result.success
    assert not result.changed


def test_an_unanswerable_version_probe_does_not_invent_an_answer(updater, monkeypatch):
    """If we cannot read the version, say so rather than claiming either way."""
    result = _run_update(
        updater,
        monkeypatch,
        install=_FakeCompleted(returncode=0, stdout="", stderr=""),
        versions=[None, None],
    )
    assert result.success, "an unreadable version is not an install failure"
    assert "could not" in (result.message + result.details).lower()


def test_a_failing_pip_is_still_a_failure(updater, monkeypatch):
    """The existing contract must survive the change."""
    result = _run_update(
        updater,
        monkeypatch,
        install=_FakeCompleted(returncode=1, stderr="no matching distribution"),
        versions=["1.9.0", "1.9.0"],
    )
    assert not result.success
