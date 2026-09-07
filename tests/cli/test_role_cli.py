# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi role` — user role membership management.

Covers list/which/add/remove/set lifecycle + the auto-bump of
`global_tier` from `starter` to `core` when a non-basic role is added
(per the 2026-05-03 design: declaring a role IS a competency claim).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.cli import help_engine, role


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".axi"


def _read(state_dir: Path) -> dict:
    return json.loads((state_dir / "competency.json").read_text())


# ---------------------------------------------------------------------------
# axi role list / which
# ---------------------------------------------------------------------------


class TestList:
    def test_list_shows_all_roles(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = role.main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        for r in ("basic", "researcher", "instructor", "student",
                  "operator", "builder", "admin", "steward"):
            assert r in out

    def test_list_marks_default(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        role.main(["list"])
        out = capsys.readouterr().out
        assert "(default)" in out

    def test_list_shows_intents_per_role(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        role.main(["list"])
        out = capsys.readouterr().out
        # Researcher row should mention its intents.
        assert "research" in out and "investigate" in out


class TestWhich:
    def test_default_install_shows_basic(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = role.main(["which"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "basic" in out
        assert "starter" in out  # default tier

    def test_after_add_shows_updated_roles(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        role.main(["add", "researcher"])
        capsys.readouterr()
        rc = role.main(["which"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "researcher" in out


# ---------------------------------------------------------------------------
# axi role add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_add_persists_role(self, state_dir: Path) -> None:
        rc = role.main(["add", "researcher"])
        assert rc == 0
        data = _read(state_dir)
        assert "researcher" in data["roles"]

    def test_add_duplicate_is_idempotent(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        role.main(["add", "researcher"])
        capsys.readouterr()
        rc = role.main(["add", "researcher"])
        assert rc == 0
        data = _read(state_dir)
        # Should still be exactly one researcher entry.
        assert data["roles"].count("researcher") == 1

    def test_add_non_basic_role_bumps_tier_starter_to_core(
        self, state_dir: Path
    ) -> None:
        """Per the 2026-05-03 design: declaring a role IS a competency
        claim.  Without this auto-bump, a starter user adds a role and
        sees no change because most role verbs are at `core` tier."""
        role.main(["add", "researcher"])
        data = _read(state_dir)
        assert data["global"] == "core"

    def test_add_does_not_lower_existing_tier(self, state_dir: Path) -> None:
        # Pre-seed with advanced.
        help_engine.save_competency(
            help_engine.UserCompetency(roles=("basic",), global_tier="advanced"),
            state_dir,
        )
        role.main(["add", "researcher"])
        data = _read(state_dir)
        assert data["global"] == "advanced"  # auto-bump never demotes

    def test_add_invalid_role_errors_via_argparse(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            role.main(["add", "wizard"])


# ---------------------------------------------------------------------------
# axi role remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_persists(self, state_dir: Path) -> None:
        role.main(["add", "researcher"])
        rc = role.main(["remove", "researcher"])
        assert rc == 0
        data = _read(state_dir)
        assert "researcher" not in data["roles"]

    def test_remove_keeps_basic_floor(self, state_dir: Path) -> None:
        """Removing the last non-basic role leaves the user with basic
        so they still have a usable surface."""
        # Pre-seed with researcher only.
        help_engine.save_competency(
            help_engine.UserCompetency(roles=("researcher",), global_tier="core"),
            state_dir,
        )
        role.main(["remove", "researcher"])
        data = _read(state_dir)
        assert data["roles"] == ["basic"]

    def test_remove_unknown_role_is_noop(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = role.main(["remove", "researcher"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "not active" in out


# ---------------------------------------------------------------------------
# axi role set
# ---------------------------------------------------------------------------


class TestSet:
    def test_set_replaces_role_list(self, state_dir: Path) -> None:
        role.main(["add", "researcher"])
        role.main(["add", "builder"])
        rc = role.main(["set", "instructor"])
        assert rc == 0
        data = _read(state_dir)
        assert data["roles"] == ["instructor"]

    def test_set_dedupes(self, state_dir: Path) -> None:
        rc = role.main(["set", "researcher", "builder", "researcher"])
        assert rc == 0
        data = _read(state_dir)
        assert data["roles"] == ["researcher", "builder"]

    def test_set_preserves_global_tier(self, state_dir: Path) -> None:
        help_engine.save_competency(
            help_engine.UserCompetency(roles=("basic",), global_tier="advanced"),
            state_dir,
        )
        role.main(["set", "researcher"])
        data = _read(state_dir)
        assert data["global"] == "advanced"
