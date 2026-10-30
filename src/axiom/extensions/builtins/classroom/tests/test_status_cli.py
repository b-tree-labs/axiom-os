# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom status` — instructor dashboard.

Two modes:
- Summary table (no classroom_id) — one row per class, student count,
  server URL, signal to the instructor at a glance.
- Drilldown (with classroom_id) — member roster, status, pending
  invites.

UX invariants tested here: the output on the summary path is not
jargon, empty state is framed as "get started with…" not "not found".
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.cli import main


@pytest.fixture
def home_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        tmp_path / "identity",
    )
    return tmp_path


def _mint(classroom_id: str, url: str = "http://x/classroom/join") -> None:
    """Helper: create a class + one unconsumed invite."""
    rc = main([
        "invite", classroom_id,
        "--coordinator-url", url,
    ])
    assert rc == 0


# ---------------------------------------------------------------------------
# Empty state — hand the instructor the next command
# ---------------------------------------------------------------------------


class TestEmptyState:
    def test_status_with_no_classes_gives_next_step(self, home_tmp, capsys):
        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        # Empty state should show the exact next command.
        assert "axi classroom invite" in out
        assert "No classes" in out or "no classes" in out


# ---------------------------------------------------------------------------
# Bare `axi classroom` — orientation, not argparse error
# ---------------------------------------------------------------------------


class TestBareClassroomOrientation:
    """Running `axi classroom` with no subcommand should be a friendly
    orientation, not an argparse error. Detection is role-aware."""

    def test_new_user_sees_welcome_and_demo_pointer(self, home_tmp, capsys):
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Welcome" in out
        assert "axi classroom demo" in out
        # Both onboarding paths (instructor + student) shown.
        assert "prep init" in out
        assert "join" in out

    def test_instructor_sees_their_classes(self, home_tmp, capsys):
        from axiom.extensions.builtins.classroom.demo import seed_demo

        seed_demo()
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "demo-classical-mechanics-spring" in out
        # Suggested next-step is dashboard + brief, not "welcome".
        assert "Welcome" not in out
        assert "axi classroom status" in out
        assert "axi classroom doctor" in out

    def test_student_sees_their_classes(self, home_tmp, capsys):
        from axiom.extensions.builtins.classroom.classroom_coordinator import (
            sign_membership_manifest,
        )
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
        )
        from axiom.extensions.builtins.classroom.student_membership import (
            MembershipStore,
        )
        from axiom.vega.federation.identity import generate_identity

        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=home_tmp / "coord-keys",
        )
        cohort = create_cohort("NE101", coord.node_id)
        cohort = add_member(cohort, "alice@ut.edu", "alice_node", "tok_a")
        manifest = sign_membership_manifest(
            identity=coord, cohort=cohort, student_id="alice@ut.edu",
        )
        MembershipStore(base_dir=home_tmp / ".axi").save(
            manifest, coord.public_key,
        )

        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NE101" in out
        assert "axi classroom ask" in out
        assert "axi classroom me" in out

    def test_status_drilldown_on_unknown_classroom(self, home_tmp, capsys):
        rc = main(["status", "NE999"])
        assert rc == 1
        # The error path reuses the same "needs bootstrap" message.
        err = capsys.readouterr().err
        assert "axi classroom invite NE999" in err


# ---------------------------------------------------------------------------
# Summary — one row per class
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_lists_each_classroom(self, home_tmp, capsys):
        _mint("NE101")
        _mint("NE102")
        capsys.readouterr()

        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NE101" in out
        assert "NE102" in out
        # Count should be 0 — nobody has joined yet.
        assert "0" in out

    def test_summary_json_shape(self, home_tmp, capsys):
        _mint("NE101", url="http://foo.example/classroom/join")
        capsys.readouterr()

        rc = main(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "classes" in data
        assert len(data["classes"]) == 1
        entry = data["classes"][0]
        assert entry["classroom_id"] == "NE101"
        assert entry["member_count"] == 0
        assert entry["coordinator_url"] == "http://foo.example/classroom/join"


# ---------------------------------------------------------------------------
# Drilldown — one class's members
# ---------------------------------------------------------------------------


class TestDrilldown:
    def test_drilldown_shows_classroom_name(self, home_tmp, capsys):
        _mint("NE101")
        capsys.readouterr()

        rc = main(["status", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NE101" in out

    def test_drilldown_empty_cohort_says_so(self, home_tmp, capsys):
        _mint("NE101")
        capsys.readouterr()

        rc = main(["status", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        # Friendly "nobody yet" message.
        assert "joined" in out.lower() or "students" in out.lower()

    def test_drilldown_reports_pending_invites(self, home_tmp, capsys):
        _mint("NE101")
        _mint("NE101")  # second invite, both unconsumed
        capsys.readouterr()

        rc = main(["status", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        # Should surface that there are outstanding invites.
        assert "outstanding" in out.lower() or "pending" in out.lower() or "invite" in out.lower()

    def test_drilldown_json_shape(self, home_tmp, capsys):
        _mint("NE101", url="http://x/classroom/join")
        capsys.readouterr()

        rc = main(["status", "NE101", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["classroom_id"] == "NE101"
        assert data["coordinator_url"] == "http://x/classroom/join"
        assert data["members"] == []
        assert data["pending_invites"] == 1


# ---------------------------------------------------------------------------
# Jargon discipline on dashboard output
# ---------------------------------------------------------------------------


class TestNoJargon:
    _FORBIDDEN = (
        "coordinator_node",
        "node_id",
        "manifest",
        "POST",
        "JSON envelope",
    )

    def test_summary_has_no_jargon(self, home_tmp, capsys):
        _mint("NE101")
        capsys.readouterr()
        main(["status"])
        out = capsys.readouterr().out
        leaked = [t for t in self._FORBIDDEN if t.lower() in out.lower()]
        assert not leaked, f"summary leaked: {leaked}"

    def test_drilldown_has_no_jargon(self, home_tmp, capsys):
        _mint("NE101")
        capsys.readouterr()
        main(["status", "NE101"])
        out = capsys.readouterr().out
        leaked = [t for t in self._FORBIDDEN if t.lower() in out.lower()]
        assert not leaked, f"drilldown leaked: {leaked}"
