# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A source clone is never told to update from the package index (#1821).

In an editable install the code is the checkout, so `update` does not apply,
yet every command printed "update available (<clone version> → <newest
wheel>)": the index's newest release compared against a version string the
clone had not bumped. The offer already declined to ask in a checkout; the
one-line notice it fell back to still printed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _run(monkeypatch, state_dir, *, checkout: bool) -> None:
    from axiom import axiom_cli
    from axiom.extensions.builtins.update import offer

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("AXIOM_DISABLE_UPDATE_NUDGE", raising=False)
    monkeypatch.setattr(offer, "_running_from_a_checkout", lambda: checkout)
    # Never read or leave an "asked today" file in the checkout's state dir.
    monkeypatch.setattr("axiom.infra.paths.get_project_state_dir", lambda *a, **k: state_dir)

    checker = MagicMock()
    checker.check_remote_version.return_value = MagicMock(
        current="1.17.9", available="1.17.13", is_newer=True
    )
    with patch(
        "axiom.extensions.builtins.update.version_check.VersionChecker",
        return_value=checker,
    ):
        axiom_cli._check_and_prompt_update()


def test_an_editable_install_prints_no_index_notice(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["axi", "--help"])
    _run(monkeypatch, tmp_path, checkout=True)
    assert "update available" not in capsys.readouterr().out


def test_an_installed_wheel_still_gets_the_notice(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["axi", "--help"])
    _run(monkeypatch, tmp_path, checkout=False)
    assert "update available (1.17.9 → 1.17.13)" in capsys.readouterr().out
