# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Guard against the silent-failure bug where deps-install failure still
resulted in `validate: Installation validated` being printed.

Bug repro (pre-fix):
    axi update
      ✗ deps: Dependency installation failed
      ✓ migrations: Database not available, skipping migrations
      ↑ agents: 3/3 agent service(s) registered
      ✓ validate: Installation validated   <-- LIE

Automation scripts that grep for "Installation validated" got a false
positive. Fix: deps failure must short-circuit the remaining steps.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from axiom.extensions.builtins.update.cli import Updater


def _fake_failed_pip(*args, **kwargs):
    return subprocess.CompletedProcess(
        args=args[0] if args else [],
        returncode=1,
        stdout="",
        stderr="ERROR: could not resolve dependency",
    )


def test_deps_failure_short_circuits_remaining_steps(tmp_path):
    """When deps fails, migrations/agents/validate must not run."""
    updater = Updater(repo_root=tmp_path)
    # Make it look like a non-editable install so no git check is needed
    # (repo_root/.git missing → is_editable=False)
    with (
        patch("subprocess.run", side_effect=_fake_failed_pip),
        patch.object(updater, "_run_migrations") as mig,
        patch.object(updater, "_register_agents") as agents,
        patch.object(updater, "_validate") as val,
    ):
        updater.update_all(pull=False)

    # Only the (failed) deps step should have recorded a result
    steps = [r.step for r in updater.results]
    assert "deps" in steps
    deps_result = next(r for r in updater.results if r.step == "deps")
    assert deps_result.success is False

    # The other steps must NOT have been called
    mig.assert_not_called()
    agents.assert_not_called()
    val.assert_not_called()


def test_deps_failure_summary_mentions_abort(tmp_path):
    """The user-facing summary should make ABORTED state unmistakable."""
    updater = Updater(repo_root=tmp_path)
    with patch("subprocess.run", side_effect=_fake_failed_pip):
        updater.update_all(pull=False)

    summary = updater.summary()
    assert "ABORTED" in summary or "aborted" in summary.lower()
    # Must NOT falsely claim validation succeeded
    assert "Installation validated" not in summary


def test_deps_failure_exit_code_nonzero(tmp_path):
    """main() should exit non-zero when deps fails."""
    from axiom.extensions.builtins.update import cli as update_cli

    with (
        patch("subprocess.run", side_effect=_fake_failed_pip),
        patch.object(update_cli, "Updater") as UpdaterCls,
    ):
        inst = UpdaterCls.return_value
        inst.dry_run = False
        inst.results = []

        def _run_all(pull=False):
            from axiom.extensions.builtins.update.cli import UpdateResult

            inst.results.append(
                UpdateResult(step="deps", success=False, message="failed"),
            )
            return inst.results

        inst.update_all.side_effect = _run_all
        inst.summary.return_value = "failed"

        rc = update_cli.main([])
    assert rc == 1


def test_deps_success_runs_all_steps(tmp_path):
    """Sanity: when deps succeeds we proceed to the other steps."""
    updater = Updater(repo_root=tmp_path)

    def _fake_ok_pip(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="Successfully installed foo-1.0",
            stderr="",
        )

    with (
        patch("subprocess.run", side_effect=_fake_ok_pip),
        patch.object(updater, "_run_migrations") as mig,
        patch.object(updater, "_register_agents") as agents,
        patch.object(updater, "_validate") as val,
    ):
        updater.update_all(pull=False)

    mig.assert_called_once()
    agents.assert_called_once()
    val.assert_called_once()
