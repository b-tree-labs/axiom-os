# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Running `axi` somewhere leaves that place as it found it, and says nothing it was not asked.

Found 2026-10-29 checking a fresh repository with `axi hygiene` (#1520). Two
things went wrong before the check said a word about the repository:

* every command, `--help` included, opened with sixteen stale TRIAGE
  diagnoses, seven of them test fixtures leaked into the real queue;
* the repository being checked gained `.axi/update-state.json` and
  `runtime/logs/cli_events.jsonl` (each with a `.lock`), because the update
  cache and the CLI event log both hung off the working directory.

A hygiene check that litters the repository it checks, behind a wall of
somebody else's old failures, is the first impression this file pins down.

Each case runs the real CLI as a bounded subprocess with HOME pointed at a
temporary directory, so nothing here can touch the developer's own ``~/.axi``.
The queue is seeded with one pending diagnosis so that "no banner" means the
banner was withheld, not that there was nothing to show.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: Representative verbs: the bare help, the hygiene noun TIDY answers to, a
#: subcommand's help, a verb that reads the repository, and the triage surface.
COMMANDS = [
    ["--help"],
    ["hygiene"],
    ["hygiene", "--help"],
    ["context", "check"],
    ["program", "--help"],
    ["triage", "pending"],
]


def _seed_a_pending_diagnosis(home: Path) -> None:
    """File one diagnosis in the queue the CLI subprocess will read."""
    code = (
        "from axiom.extensions.builtins.diagnostics import cli_listener as c;"
        "c.append_diagnosis(None, {'fingerprint': 'seeded000001',"
        " 'summary': 'an earlier failure', 'confidence': 0.6,"
        " 'matched_at': '2099-01-01T00:00:00+00:00'})"
    )
    subprocess.run([sys.executable, "-c", code], env=_env(home), check=True, timeout=60)


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Exercise the real default (HOME-derived state dir), not the session pin.
    env.pop("AXI_STATE_DIR", None)
    for quiet in ("AXI_DIAGNOSES_QUIET", "PYTEST_CURRENT_TEST"):
        env.pop(quiet, None)
    env["AXI_NO_SOURCE_WARNING"] = "1"
    env["AXIOM_DISABLE_UPDATE_NUDGE"] = "1"
    env["COLUMNS"] = "100"
    return env


def _snapshot(root: Path) -> set[str]:
    return {
        str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.relative_to(root).parts
    }


@pytest.fixture(params=["empty-dir", "git-repo"])
def workdir(request, tmp_path: Path) -> Path:
    where = tmp_path / request.param
    where.mkdir()
    if request.param == "git-repo":
        subprocess.run(["git", "init", "-q", str(where)], check=True, timeout=30)
        (where / "README.md").write_text("a repository someone cares about\n")
    return where


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    _seed_a_pending_diagnosis(h)
    return h


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_axi_writes_nothing_into_the_directory_it_runs_in(argv, workdir, home):
    before = _snapshot(workdir)
    result = subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", *argv],
        cwd=workdir,
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    created = sorted(_snapshot(workdir) - before)
    assert created == [], (
        f"`axi {' '.join(argv)}` wrote into its working directory: {created}\n"
        f"stderr tail: {result.stderr[-600:]}"
    )


@pytest.mark.parametrize(
    "argv", [a for a in COMMANDS if a[0] != "triage"], ids=lambda a: " ".join(a)
)
def test_axi_prints_no_triage_banner_ahead_of_another_command(argv, workdir, home):
    result = subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", *argv],
        cwd=workdir,
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    shown = result.stdout + result.stderr
    assert "[TRIAGE]" not in shown and "seeded000001" not in shown, (
        f"`axi {' '.join(argv)}` printed the pending-diagnoses banner:\n{shown[:800]}"
    )


def test_the_seeded_diagnosis_is_still_there_to_read_on_purpose(tmp_path, home):
    """The control: withholding the banner must not lose the diagnosis."""
    result = subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", "triage", "pending", "--format", "json"],
        cwd=tmp_path,
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert [d["fingerprint"] for d in payload["pending"]] == ["seeded000001"]
