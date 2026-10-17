# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Telling you which checkout you are actually running.

One venv serves every worktree, so ``pip install -e`` anchors the package to one
of them. Standing in another and typing ``axi`` runs the anchor's code, and the
symptom — a verb that "does not exist", a fix that "did not work" — points
nowhere near the cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infra.source_provenance import (
    SUPPRESS_ENV,
    foreign_checkout_warning,
    warn_if_foreign_checkout,
)


@pytest.fixture(autouse=True)
def _unsuppressed(monkeypatch):
    monkeypatch.delenv(SUPPRESS_ENV, raising=False)


def _checkout(root: Path) -> Path:
    (root / "src" / "axiom").mkdir(parents=True)
    (root / "src" / "axiom" / "__init__.py").write_text("")
    return root


class TestItNoticesTheMismatch:
    def test_running_one_checkout_while_standing_in_another(self, tmp_path):
        here = _checkout(tmp_path / "mine")
        anchor = _checkout(tmp_path / "anchor")

        message = foreign_checkout_warning(
            str(anchor / "src" / "axiom" / "__init__.py"), here
        )

        assert message is not None
        assert str(anchor) in message
        assert str(here) in message

    def test_it_says_what_to_do_about_it(self, tmp_path):
        """A diagnostic that names a problem and no remedy is half a diagnostic."""
        message = foreign_checkout_warning(
            str(_checkout(tmp_path / "anchor") / "src" / "axiom" / "__init__.py"),
            _checkout(tmp_path / "mine"),
        )
        assert "worktree-env.sh" in message

    def test_it_finds_the_checkout_from_a_subdirectory(self, tmp_path):
        """You are rarely standing at the root when you run a command."""
        here = _checkout(tmp_path / "mine")
        deep = here / "src" / "axiom" / "extensions"
        deep.mkdir(parents=True)

        assert foreign_checkout_warning(
            str(_checkout(tmp_path / "anchor") / "src" / "axiom" / "__init__.py"), deep
        )


class TestItStaysQuietWhenItShould:
    def test_nothing_to_say_when_they_match(self, tmp_path):
        here = _checkout(tmp_path / "mine")
        assert (
            foreign_checkout_warning(str(here / "src" / "axiom" / "__init__.py"), here)
            is None
        )

    def test_nothing_to_say_outside_a_checkout(self, tmp_path):
        """Running the installed build from anywhere else is the normal case."""
        anchor = _checkout(tmp_path / "anchor")
        elsewhere = tmp_path / "somewhere"
        elsewhere.mkdir()

        assert (
            foreign_checkout_warning(
                str(anchor / "src" / "axiom" / "__init__.py"), elsewhere
            )
            is None
        )

    def test_it_can_be_silenced(self, tmp_path, monkeypatch):
        """Comparing the release against a checkout is a legitimate thing to do."""
        monkeypatch.setenv(SUPPRESS_ENV, "1")

        assert (
            foreign_checkout_warning(
                str(_checkout(tmp_path / "anchor") / "src" / "axiom" / "__init__.py"),
                _checkout(tmp_path / "mine"),
            )
            is None
        )

    def test_a_wheel_install_is_not_a_checkout(self, tmp_path):
        """site-packages/axiom/__init__.py has no ``src`` parent, so no claim."""
        installed = tmp_path / "site-packages" / "axiom" / "__init__.py"
        installed.parent.mkdir(parents=True)

        assert foreign_checkout_warning(str(installed), _checkout(tmp_path / "mine")) is None


class TestItNeverBreaksTheCommand:
    def test_a_broken_lookup_is_swallowed(self, monkeypatch):
        """A CLI that failed to start because it could not locate its own source
        would be a worse bug than the one this reports."""
        monkeypatch.setattr(
            "axiom.infra.source_provenance.foreign_checkout_warning",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert warn_if_foreign_checkout() is False

    def test_no_module_file_is_not_a_warning(self, tmp_path):
        assert foreign_checkout_warning(None, _checkout(tmp_path / "mine")) is None
