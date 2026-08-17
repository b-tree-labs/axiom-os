# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom serve` — instructor's long-running coordinator.

Server-logic tests live in `test_coordinator_server.py`. This file only
covers what the CLI adds: argument parsing, bootstrap path, startup
messaging, and the error path when a class hasn't been set up yet.
"""

from __future__ import annotations

import threading
from http.server import HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_client import (
    ClassroomJoinClient,
)
from axiom.extensions.builtins.classroom.classroom_join_http import UrllibTransport
from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.coordinator_server import (
    make_coordinator_handler,
)
from axiom.extensions.builtins.classroom.student_membership import MembershipStore
from axiom.vega.federation.identity import generate_identity, load_identity


@pytest.fixture
def home_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        tmp_path / "identity",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Error path — no class set up yet
# ---------------------------------------------------------------------------


class TestNoClassYet:
    def test_serve_without_setup_tells_instructor_what_to_run(
        self, home_tmp, capsys
    ):
        rc = main(["serve", "NE101"])
        assert rc == 1
        err = capsys.readouterr().err
        # The message should give the EXACT next command — the instructor
        # shouldn't have to think.
        assert "axi classroom invite NE101" in err
        # No jargon.
        assert "manifest" not in err.lower()
        assert "cohort" not in err.lower()


# ---------------------------------------------------------------------------
# Happy path — CLI bootstrap + running server accepts real join
# ---------------------------------------------------------------------------


class TestBootstrapFromInvite:
    def test_invite_then_serve_end_to_end(self, home_tmp, capsys, tmp_path):
        """Instructor runs invite (one terminal), serve (another), student
        joins. This is the demo-loop smoke test — if this passes, the
        end-to-end ceremony is wired."""
        # Terminal A: instructor mints the first invite. This bootstraps
        # identity + cohort + stores the coordinator URL.
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "http://127.0.0.1:0/classroom/join",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        import re
        match = re.search(r"axi classroom join (\S+)", out)
        assert match
        encoded_invite = match.group(1)

        # The bootstrap wrote everything to ~/.axi/coordinator. Instead of
        # actually invoking `main(["serve", ...])` (which blocks), we
        # re-use the same file-backed state to spin up the HTTPServer
        # directly in a thread. This proves: the files that `invite`
        # wrote are sufficient for `serve`-style operation.
        coord_dir = home_tmp / ".axi" / "coordinator"
        identity = load_identity()
        assert identity is not None, "invite should have created identity"
        handler_cls = make_coordinator_handler(
            coordinator_identity=identity,
            classroom_id="NE101",
            cohort_store=FileCohortStore(coord_dir),
            invite_registry=FileInviteRegistry(coord_dir / "invites.json"),
        )
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            real_url = f"http://127.0.0.1:{server.server_port}/classroom/join"

            # The student's transport posts to real_url (the test server),
            # so we override the URL the client uses.
            student_identity = generate_identity(
                owner="alice@example.org",
                keys_dir=tmp_path / "student_keys",
            )
            student_store = MembershipStore(base_dir=tmp_path / "student_axi")
            client = ClassroomJoinClient(
                student_identity=student_identity,
                transport=UrllibTransport(),
                store=student_store,
            )
            result = client.join(
                encoded_invite=encoded_invite,
                student_id="alice",
                coordinator_url=real_url,
            )
            assert result.accepted is True
            assert student_store.load("NE101").student_id == "alice"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Startup messaging — instructor sees exactly what to do next
# ---------------------------------------------------------------------------


class TestStartupMessage:
    def test_serve_startup_message_is_helpful(self, home_tmp, capsys):
        """Run serve in a thread, wait for it to print, then shut it down."""
        # Bootstrap first so serve doesn't short-circuit with the
        # helpful "run invite first" message.
        main([
            "invite", "NE101",
            "--coordinator-url", "http://127.0.0.1:12345/classroom/join",
        ])
        capsys.readouterr()  # drain

        rc_container: dict[str, int | None] = {"rc": None}
        def _run():
            # Use port 0 so we don't collide with anything on 8787.
            rc_container["rc"] = main(["serve", "NE101", "--port", "0"])

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        # Give the thread a moment to print startup + block in serve_forever.
        thread.join(timeout=1)
        # It's still running (thread hasn't finished) — that's correct.
        # We can't cleanly shut it down from the test without keyboard
        # interrupt, but we've captured stdout already.
        out = capsys.readouterr().out
        assert "NE101" in out
        assert "axi classroom invite NE101" in out  # hint for next terminal
        assert "Ctrl-C" in out  # instructor knows how to stop
        # No jargon in startup output.
        assert "manifest" not in out.lower()
        assert "POST" not in out
