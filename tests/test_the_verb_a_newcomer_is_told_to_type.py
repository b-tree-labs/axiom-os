# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`start` is the verb a newcomer reaches for, so it has to be there.

A colleague followed an onboarding guide that opened with `start`, got
`unknown subcommand`, and stopped. The guide was wrong — the verb had been
agreed in a design discussion and never built — but the lesson is not only that
a guide must name real verbs. It is that `start` is the word somebody types
when they have just installed something and want it to set itself up. Nobody
arriving for the first time guesses `config`, which reads like a verb for
changing a setting you already have.

So `start` dispatches to the setup wizard, alongside the `config` spelling that
already worked and the `setup` alias the quickstart used. Three spellings of one
act is not elegant, and it is cheaper than a newcomer stopping at the first
line.

It also has to be *visible*. A hidden alias satisfies anybody who already knows
to type it, which is the one group that does not need it.
"""

from __future__ import annotations

import subprocess
import sys


def test_start_is_a_real_verb() -> None:
    from axiom.axiom_cli import SUBCOMMANDS

    assert "start" in SUBCOMMANDS, "the verb a newcomer types does not exist"


def test_start_runs_the_same_thing_config_does() -> None:
    """Not a near-miss that lands somewhere else."""
    from axiom.axiom_cli import SUBCOMMANDS

    assert SUBCOMMANDS["start"] == SUBCOMMANDS["config"]


def test_start_is_listed_where_a_newcomer_will_look() -> None:
    """A newcomer runs the bare command and reads what comes back. An alias
    that works but is not printed only helps somebody who already knew."""
    done = subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", "--help"],
        capture_output=True, text=True, timeout=180,
    )
    shown = done.stdout + done.stderr
    lines = [ln for ln in shown.splitlines() if ln.strip().startswith("start")]
    assert lines, f"`start` is not in the help a newcomer reads:\n{shown[-1500:]}"


def test_start_dispatches_rather_than_being_rejected() -> None:
    """The failure this test exists for printed `unknown subcommand`."""
    done = subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", "start", "--help"],
        capture_output=True, text=True, timeout=180,
    )
    said = (done.stdout + done.stderr).lower()
    assert "unknown subcommand" not in said, said[-800:]
