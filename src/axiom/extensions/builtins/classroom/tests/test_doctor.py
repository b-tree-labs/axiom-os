# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom doctor` — per-classroom self-diagnostic.

Doctor is read-only: it reports state and points at the next command,
never mutates. The tests below pin a few invariants:

- Unknown classroom (no local state) returns ``role=unknown`` and a
  fail check with both instructor + student onboarding hints.
- Demo classroom passes the instructor-side artifact + materials
  checks out of the box (smoke for the seed_demo materials fix).
- Student-side checks turn ``warn`` when the coordinator URL sidecar
  is missing — they don't silently report ``ok``.
- Archived classroom is reported as a warn with a clone hint, not a
  hard fail (it's a valid terminal state).
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.doctor import (
    detect_role,
    run_diagnostics,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------


class TestDetectRole:
    def test_unknown_when_no_local_state(self):
        assert detect_role("never-heard-of-it") == "unknown"

    def test_instructor_when_coordinator_dir_exists(self, tmp_path):
        coord = (
            tmp_path / ".axi" / "coordinator"
            / "classrooms" / "NE101"
        )
        coord.mkdir(parents=True)
        assert detect_role("NE101") == "instructor"

    def test_student_when_membership_present(self, tmp_path):
        student_dir = tmp_path / ".axi" / "classrooms" / "NE101"
        student_dir.mkdir(parents=True)
        (student_dir / "membership.json").write_text("{}")
        assert detect_role("NE101") == "student"


# ---------------------------------------------------------------------------
# Unknown classroom — fail with onboarding hint
# ---------------------------------------------------------------------------


class TestUnknownClassroom:
    def test_overall_is_fail(self):
        report = run_diagnostics("nope")
        assert report.role == "unknown"
        assert report.overall == "fail"

    def test_hint_offers_both_onboarding_paths(self):
        report = run_diagnostics("nope")
        names = [c.name for c in report.checks]
        assert "local_role" in names
        local = next(c for c in report.checks if c.name == "local_role")
        assert "prep init" in local.hint
        assert "join" in local.hint


# ---------------------------------------------------------------------------
# Instructor-side — demo classroom passes the artifact + materials checks
# ---------------------------------------------------------------------------


class TestInstructorDemoClassroom:
    def _seed(self):
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            seed_demo,
        )
        seed_demo()
        return DEMO_CLASSROOM_ID

    def test_role_detected_as_instructor(self):
        cid = self._seed()
        report = run_diagnostics(cid)
        assert report.role == "instructor"

    def test_classroom_artifact_check_ok(self):
        cid = self._seed()
        report = run_diagnostics(cid)
        c = next(c for c in report.checks if c.name == "classroom_artifact")
        assert c.status == "ok"

    def test_course_artifact_check_ok(self):
        cid = self._seed()
        report = run_diagnostics(cid)
        c = next(c for c in report.checks if c.name == "course_artifact")
        assert c.status == "ok"

    def test_coordinator_materials_check_ok(self):
        """Regression for the demo-materials-sync fix: seed_demo must
        populate the coordinator-side materials store, so doctor sees
        the 10 demo docs."""
        cid = self._seed()
        report = run_diagnostics(cid)
        c = next(
            c for c in report.checks if c.name == "coordinator_materials"
        )
        assert c.status == "ok"
        assert "10" in c.detail

    def test_prep_publishable_when_demo_ready(self):
        cid = self._seed()
        report = run_diagnostics(cid)
        c = next(c for c in report.checks if c.name == "prep_publishable")
        assert c.status == "ok"


# ---------------------------------------------------------------------------
# Instructor-side — archived classroom is warn with clone hint
# ---------------------------------------------------------------------------


class TestArchivedClassroom:
    def test_archived_state_reported_as_warn(self):
        from axiom.extensions.builtins.classroom.archive import archive_classroom
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            seed_demo,
        )
        from axiom.extensions.builtins.classroom.publish import publish_classroom

        seed_demo()
        publish_classroom(classroom_id=DEMO_CLASSROOM_ID, approver="@ben:ut")
        archive_classroom(
            classroom_id=DEMO_CLASSROOM_ID, archiver="@ben:ut", reason="done",
        )
        report = run_diagnostics(DEMO_CLASSROOM_ID)
        c = next(c for c in report.checks if c.name == "classroom_artifact")
        assert c.status == "warn"
        assert "archived" in c.detail or "clone" in c.hint.lower()


# ---------------------------------------------------------------------------
# Student-side — membership present, coordinator URL missing → warn
# ---------------------------------------------------------------------------


class TestStudentChecksWarnPaths:
    def _setup_partial_student(self, tmp_path, classroom_id="NE101"):
        """Set up a student dir with membership but missing coordinator
        URL sidecar — the kind of partial state that breaks me/threads
        but that doctor must surface clearly."""
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
            owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys",
        )
        cohort = create_cohort(classroom_id, coord.node_id)
        cohort = add_member(cohort, "alice@ut.edu", "alice_node", "tok_a")
        manifest = sign_membership_manifest(
            identity=coord, cohort=cohort, student_id="alice@ut.edu",
        )
        store = MembershipStore(base_dir=tmp_path / ".axi")
        store.save(manifest, coord.public_key)
        # Note: deliberately not writing coordinator_url.txt — that's
        # the partial state we want to detect.

    def test_membership_ok_url_warn(self, tmp_path):
        self._setup_partial_student(tmp_path)
        report = run_diagnostics("NE101")
        assert report.role == "student"
        membership = next(
            c for c in report.checks if c.name == "membership"
        )
        assert membership.status == "ok"
        url_check = next(
            c for c in report.checks if c.name == "coordinator_url"
        )
        assert url_check.status == "warn"
        assert "join" in url_check.hint.lower()


# ---------------------------------------------------------------------------
# Report shape — JSON-serializable
# ---------------------------------------------------------------------------


class TestReportSerialization:
    def test_to_dict_round_trip(self):
        report = run_diagnostics("never-heard-of")
        d = report.to_dict()
        # Must be JSON-serializable for the --json CLI flag.
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded["classroom_id"] == "never-heard-of"
        assert decoded["role"] == "unknown"
        assert decoded["overall"] in ("ok", "warn", "fail")
        assert isinstance(decoded["checks"], list)
        assert all("name" in c and "status" in c for c in decoded["checks"])


# ---------------------------------------------------------------------------
# CLI smoke — exit codes match overall status
# ---------------------------------------------------------------------------


class TestCLIExitCodes:
    def test_unknown_classroom_exits_2(self, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["doctor", "nope"])
        assert rc == 2  # fail
        capsys.readouterr()  # drain

    def test_demo_classroom_warn_exits_1_or_0(self, capsys):
        """Freshly-seeded demo (no identity yet) → warn (rc=1).
        Once identity exists (after first invite/join), demo → ok (rc=0).
        Either is acceptable here; we just want a non-fail exit."""
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            seed_demo,
        )

        seed_demo()
        rc = main(["doctor", DEMO_CLASSROOM_ID])
        assert rc in (0, 1)
        capsys.readouterr()

    def test_json_output_parses(self, capsys):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            seed_demo,
        )

        seed_demo()
        main(["doctor", DEMO_CLASSROOM_ID, "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["classroom_id"] == DEMO_CLASSROOM_ID
        assert data["role"] == "instructor"
