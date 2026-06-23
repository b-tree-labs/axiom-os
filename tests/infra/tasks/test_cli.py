# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi tasks`` CLI verbs."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Force tasks/store under tmp_path — never touch the real $AXI_STATE_DIR."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))


def test_list_empty(capsys):
    from axiom.infra.tasks.cli import main

    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No tasks" in out


def test_spawn_then_list_then_show(capsys, tmp_path):
    from axiom.infra.tasks.cli import main

    rc = main(["spawn", "--cwd", str(tmp_path), "--principal", "@a:b",
               "echo-hello", "echo", "hello-world"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Spawned task" in out
    # Extract task_id from "Spawned task <id> (...)".
    line = next(line for line in out.splitlines() if "Spawned task" in line)
    task_id = line.split("Spawned task ", 1)[1].split(" ", 1)[0]

    # Wait briefly for the subprocess to finish.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        from axiom.infra.tasks.runner import TaskRunner
        from axiom.infra.tasks.store import TaskStore

        s = TaskStore()
        r = TaskRunner(s)
        # Create a fresh runner means no in-memory Popen — fall through
        # to the cross-CLI path. Wait for the original CLI's runner to
        # have noticed the process exit; we simulate by calling status
        # via the new runner. With detached process group + brief sleep
        # this should mark the task done within the timeout.
        time.sleep(0.1)
        t = r.status(task_id)
        if t.status in ("done", "failed"):
            break

    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert task_id in out
    assert "echo-hello" in out

    rc = main(["show", task_id, "--tail", "10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert task_id in out
    # The output file may not have flushed yet on very fast systems —
    # we just assert show succeeded.


def test_spawn_with_no_command_errors(capsys):
    """argparse exits 2 with stderr complaint when command is missing."""
    from axiom.infra.tasks.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["spawn", "broken"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "command" in err.lower()


def test_cancel_unknown_task(capsys):
    from axiom.infra.tasks.cli import main

    rc = main(["cancel", "no-such-id"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "Unknown" in out or "not" in out.lower()


def test_clear_returns_count(capsys, tmp_path):
    from axiom.infra.tasks.cli import main
    from axiom.infra.tasks.store import TaskStore

    s = TaskStore()
    a = s.create(name="x", command=["true"], cwd=tmp_path, principal="@a:b")
    s.update(a.task_id, status="done")

    rc = main(["clear"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1" in out  # cleared 1 terminal task


def test_principal_default_includes_at_sign():
    """Even without an identity, the default principal must be a valid
    Matrix-style @host:context so federation peer-query stays consistent."""
    from axiom.infra.tasks.cli import _principal_default

    p = _principal_default()
    assert p.startswith("@")
    assert ":" in p


def test_axiom_cli_dispatches_tasks_verb():
    """SUBCOMMANDS includes 'tasks'."""
    from axiom.axiom_cli import SUBCOMMANDS

    assert "tasks" in SUBCOMMANDS
    assert SUBCOMMANDS["tasks"] == "axiom.infra.tasks.cli"
