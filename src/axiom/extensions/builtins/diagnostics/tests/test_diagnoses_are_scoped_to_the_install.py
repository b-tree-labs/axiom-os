# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A pending diagnosis belongs to the install that produced it.

Found by the partner onboarding smoke test. That test provisions a clean
venv, installs the published wheel and runs the sequence a partner is told
to run — and the very first command printed this, to stderr, ahead of the
partner's own output::

    [TRIAGE] 5 pending diagnoses from an earlier command (not this one):
      • e1dfcf93f2b8  The CLI failed to authenticate with the PostgreSQL
                      server due to an incorrect password for the 'axiom' user
      • 910e6994470d  ...the state directory is not properly initialized or
                      referenced in the vault ...

None of those failures happened in that venv. They happened weeks earlier,
on the operator's own node, and they name that node's Postgres role and
vault state. The queue was keyed on ``$HOME``, so every install on the
machine read the same file and inherited every other install's failures.

Home is the wrong key. The docstring on :func:`pending_path` already said
what the right one was — "a failure of *this install* on this machine" —
and ``$HOME`` was standing in for it. It is a poor stand-in: it cannot tell
a freshly provisioned partner venv from the node that has been accruing
operator failures since install.

The earlier fix these tests must not undo: ``axi`` and ``neut`` are two
aliases of one install and must keep sharing one queue. Keying on the
environment the CLI runs from keeps that true — same venv, same queue —
while separating installs that genuinely are different.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.diagnostics import cli_listener


def _record(fp: str = "abc123", summary: str = "a failure") -> dict:
    return {
        "fingerprint": fp,
        "summary": summary,
        "remedy": "do the thing",
        "confidence": 0.9,
        "pattern_id": "p1",
        "matched_at": "2026-09-21T00:00:00Z",
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    """One machine, one user, one home — shared by every install on it."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _as_install(monkeypatch, prefix: Path) -> None:
    """Run the next calls as though the CLI came from ``prefix``'s venv."""
    prefix.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))


class TestTwoInstallsOnOneMachineDoNotShareAQueue:
    def test_a_fresh_install_starts_empty_beside_a_busy_one(self, home, monkeypatch):
        """The bug, stated as a test.

        The operator's node has five pending diagnoses. A partner installs
        the published wheel into a clean venv on the same machine and runs
        their first command. They must see none of it.
        """
        _as_install(monkeypatch, home / "operator-venv")
        for i in range(5):
            cli_listener.append_diagnosis(None, _record(fp=f"op{i}"))
        assert len(cli_listener.read_pending()) == 5

        _as_install(monkeypatch, home / "partner-venv")
        assert cli_listener.read_pending() == [], (
            "a clean install inherited another install's failures — this is "
            "what the partner smoke test caught, verbatim"
        )

    def test_the_operator_keeps_their_own_queue(self, home, monkeypatch):
        """Isolation must not cost the operator the queue they rely on."""
        _as_install(monkeypatch, home / "operator-venv")
        cli_listener.append_diagnosis(None, _record(fp="mine"))

        _as_install(monkeypatch, home / "partner-venv")
        cli_listener.append_diagnosis(None, _record(fp="theirs"))

        _as_install(monkeypatch, home / "operator-venv")
        assert [d["fingerprint"] for d in cli_listener.read_pending()] == ["mine"]

    def test_clearing_one_install_leaves_the_other_alone(self, home, monkeypatch):
        _as_install(monkeypatch, home / "a-venv")
        cli_listener.append_diagnosis(None, _record(fp="aaa"))
        _as_install(monkeypatch, home / "b-venv")
        cli_listener.append_diagnosis(None, _record(fp="bbb"))

        assert cli_listener.clear_pending(None, fingerprint=None) == 1
        _as_install(monkeypatch, home / "a-venv")
        assert [d["fingerprint"] for d in cli_listener.read_pending()] == ["aaa"]


class TestOneInstallUnderTwoBrandsStillSharesOneQueue:
    """The earlier fix, guarded. Do not regress it while fixing the above."""

    def test_the_same_venv_reads_one_queue_whichever_alias_was_typed(
        self, home, monkeypatch
    ):
        _as_install(monkeypatch, home / "one-venv")
        cli_listener.append_diagnosis(None, _record(fp="shared"))

        # A second alias of the SAME install: same venv, different brand.
        import axiom.infra.branding as branding

        monkeypatch.setattr(branding.BrandingConfig, "cli_name", "neut", raising=False)
        assert [d["fingerprint"] for d in cli_listener.read_pending()] == ["shared"], (
            "axi and neut are two names for one install; splitting their "
            "queues is the bug that machine-scoping was introduced to fix"
        )


class TestNothingAlreadyFiledIsLost:
    def test_a_machine_scoped_queue_is_adopted_by_the_running_install(
        self, home, monkeypatch
    ):
        """Records filed before install-scoping are still real failures.

        They were written by whichever install was in use, and there is no
        way to tell which. The running install adopts them rather than
        stranding them where nothing will ever read them again.
        """
        _as_install(monkeypatch, home / "operator-venv")
        legacy = Path(cli_listener.pending_path()).parents[2] / cli_listener.PENDING_FILENAME
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps(_record(fp="old")) + "\n")

        cli_listener._adopt_legacy_diagnoses()
        assert [d["fingerprint"] for d in cli_listener.read_pending()] == ["old"]
        assert not legacy.exists(), (
            "leaving the machine-scoped file in place resurrects the leak on "
            "the next read"
        )

    def test_adoption_does_not_duplicate_what_is_already_here(self, home, monkeypatch):
        _as_install(monkeypatch, home / "operator-venv")
        cli_listener.append_diagnosis(None, _record(fp="dup"))
        legacy = Path(cli_listener.pending_path()).parents[2] / cli_listener.PENDING_FILENAME
        legacy.write_text(json.dumps(_record(fp="dup")) + "\n")

        cli_listener._adopt_legacy_diagnoses()
        assert len(cli_listener.read_pending()) == 1
