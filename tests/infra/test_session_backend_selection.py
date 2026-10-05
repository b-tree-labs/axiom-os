# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where a session is stored, and why it is not a file by default.

The session plane is the one a second surface has to reach: continuing a
conversation on a phone, or in a web harness, means both are clients of one
store. JSON files under `runtime/sessions/` cannot be that — each machine has
its own.

Ben's rule: SQLite (and the file store) are for LOCAL TESTING ONLY; the
properly described IaC PostgreSQL is the default otherwise. So the backend is
chosen by whether a database is configured, not by a flag someone remembers
to set, and the plane reports honestly which one answered.
"""
from __future__ import annotations

import pytest

from axiom.infra.orchestrator.session_backend import (
    BackendChoice,
    choose_session_backend,
)


class TestSelection:
    def test_a_configured_database_wins(self, monkeypatch):
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://user:pw@db.example:5432/axiom")
        choice = choose_session_backend()
        assert choice.kind == "database"
        assert choice.local_only is False

    def test_no_database_falls_back_to_files(self, monkeypatch):
        """A laptop with nothing configured keeps working exactly as before."""
        monkeypatch.delenv("AXIOM_DB_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        choice = choose_session_backend(default_url="")
        assert choice.kind == "files"
        assert choice.local_only is True

    def test_the_file_backend_says_it_is_local_only(self, monkeypatch):
        """It must be visibly a testing backend, so nobody mistakes a laptop
        for a deployment."""
        monkeypatch.delenv("AXIOM_DB_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert "local" in choose_session_backend(default_url="").reason.lower()

    def test_a_localhost_database_is_not_a_shared_store(self, monkeypatch):
        """Postgres on 127.0.0.1 is still one machine's database. It is the
        right backend, but it does not make the plane `served` — claiming a
        shared store that no second surface can reach is the failure this
        whole thread is about."""
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://a:b@localhost:5432/axiom_db")
        choice = choose_session_backend()
        assert choice.kind == "database"
        assert choice.shared is False

    def test_a_remote_database_is_shared(self, monkeypatch):
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://a:b@db.example.edu:5432/axiom")
        assert choose_session_backend().shared is True

    def test_the_reason_never_carries_the_password(self, monkeypatch):
        """The reason string reaches `/planes` and logs."""
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://user:hunter2@db.example:5432/x")
        choice = choose_session_backend()
        assert "hunter2" not in choice.reason
        assert "hunter2" not in choice.source


class TestThePlaneKind:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("postgresql://a:b@db.example.edu/x", "served"),
            ("postgresql://a:b@localhost/x", "local"),
            ("", "local"),
        ],
    )
    def test_kind_reflects_reachability_not_merely_configuration(
        self, url, expected, monkeypatch
    ):
        if url:
            monkeypatch.setenv("AXIOM_DB_URL", url)
        else:
            monkeypatch.delenv("AXIOM_DB_URL", raising=False)
            monkeypatch.delenv("DATABASE_URL", raising=False)
        assert choose_session_backend(default_url="").plane_kind == expected


class TestItIsADataclassNotAGlobal:
    def test_choices_are_independent(self, monkeypatch):
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://a:b@db.example.edu/x")
        first = choose_session_backend()
        monkeypatch.delenv("AXIOM_DB_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        second = choose_session_backend(default_url="")
        assert isinstance(first, BackendChoice) and isinstance(second, BackendChoice)
        assert first.kind != second.kind
