# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test for the non-blocking update nudge (L1 canary fix).

The pre-v0.10.0 update check called `input()` on every interactive CLI
invocation, blocking the user's workflow mid-task. This is the kind of
UX cognitive load we're trying to eliminate. Guard that it never returns.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_update_nudge_does_not_call_input(monkeypatch, capsys):
    """The banner must not block on input() — ever."""
    from axiom import axiom_cli

    # TTY required for the nudge path to even run
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("AXIOM_DISABLE_UPDATE_NUDGE", raising=False)

    fake_info = MagicMock(current="0.9.0", available="0.10.0", is_newer=True)
    fake_checker = MagicMock()
    fake_checker.check_remote_version.return_value = fake_info

    # Make input() raise so the test fails loudly if the nudge ever calls it.
    def _boom(*a, **kw):
        raise AssertionError("update nudge must not call input()")

    with (
        patch(
            "axiom.extensions.builtins.update.version_check.VersionChecker",
            return_value=fake_checker,
        ),
        patch("builtins.input", _boom),
    ):
        axiom_cli._check_and_prompt_update()

    out = capsys.readouterr().out
    assert "update available" in out
    assert "0.9.0 → 0.10.0" in out


def test_update_nudge_respects_opt_out(monkeypatch, capsys):
    from axiom import axiom_cli

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("AXIOM_DISABLE_UPDATE_NUDGE", "1")

    # If the opt-out works, we never hit VersionChecker at all.
    with patch(
        "axiom.extensions.builtins.update.version_check.VersionChecker",
        side_effect=AssertionError("should not call checker when opt-out is set"),
    ):
        axiom_cli._check_and_prompt_update()

    assert capsys.readouterr().out == ""


def test_update_nudge_silent_when_up_to_date(monkeypatch, capsys):
    from axiom import axiom_cli

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("AXIOM_DISABLE_UPDATE_NUDGE", raising=False)

    fake_checker = MagicMock()
    fake_checker.check_remote_version.return_value = MagicMock(is_newer=False)

    with patch(
        "axiom.extensions.builtins.update.version_check.VersionChecker",
        return_value=fake_checker,
    ):
        axiom_cli._check_and_prompt_update()

    assert capsys.readouterr().out == ""
