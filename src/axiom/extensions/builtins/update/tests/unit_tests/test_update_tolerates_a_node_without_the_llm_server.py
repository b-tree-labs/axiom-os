# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`update infra` must not fail a node for lacking a pod it never had.

The ``llm-server`` pod is an Edge-profile opt-in (ADR-019): the signals
bootstrap emits it only for an Edge node that asked. On every other node the
Deployment does not exist. `_update_llm_server` reported that as a failed step
telling the operator to run the installer, and on a node with no cluster at all
the ``kubectl`` probe raised out of the update entirely.

Absence is the expected state on most nodes. The step records a one-line note,
counts as success with nothing changed, and never reaches the image check or
the rollout.
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
    return Updater(repo_root=tmp_path)


def _scripted_run(monkeypatch, *, on_probe):
    """A subprocess.run that answers the Deployment probe and refuses the rest.

    Anything past the probe is the update path — image inspect, image import,
    rollout — and reaching it on an absent Deployment is the regression.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["kubectl", "get", "deployment"]:
            if isinstance(on_probe, BaseException):
                raise on_probe
            return on_probe
        raise AssertionError(f"the update path ran against an absent Deployment: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _assert_quiet_note(result):
    assert result.step == "llm-server"
    assert result.success, "a node without the pod was reported as a failed update"
    assert result.changed is False
    assert "nothing to update" in result.message
    assert "\n" not in result.message, "the note is one line"


def test_a_node_without_the_deployment_is_a_quiet_success(updater, monkeypatch):
    calls = _scripted_run(monkeypatch, on_probe=_FakeCompleted(returncode=1, stderr="NotFound"))

    updater._update_llm_server()

    _assert_quiet_note(updater.results[-1])
    assert len(calls) == 1, "only the probe ran"


def test_a_node_without_kubectl_has_no_deployment_either(updater, monkeypatch):
    """No cluster on the node: the probe cannot even start. Still not a failure."""
    calls = _scripted_run(monkeypatch, on_probe=FileNotFoundError("kubectl"))

    updater._update_llm_server()  # must not raise

    _assert_quiet_note(updater.results[-1])
    assert "kubectl" in updater.results[-1].message
    assert len(calls) == 1


def test_update_infra_returns_normally_on_such_a_node(updater, monkeypatch):
    """The public verb, end to end: one result, success, nothing raised."""
    _scripted_run(monkeypatch, on_probe=_FakeCompleted(returncode=1))

    results = updater.update_infra()

    assert len(results) == 1
    _assert_quiet_note(results[0])


def test_a_node_with_the_deployment_still_reaches_the_update_path(updater, monkeypatch):
    """The tolerance is for absence only; a present pod is still updated."""
    updater.dry_run = True
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    updater._update_llm_server()

    result = updater.results[-1]
    assert result.success and result.changed
    assert "Would rolling-update" in result.message
    assert [c[:3] for c in calls] == [
        ["kubectl", "get", "deployment"],
        ["docker", "image", "inspect"],
    ]
