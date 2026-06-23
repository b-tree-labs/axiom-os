# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cross-process persistence regression for recipient preferences.

HERALD-2a symptom: prefs ``set`` by one process were invisible to a
second process (CLI write → daemon read). Root cause was
``default_store()`` returning ``InMemoryRecipientPreferenceStore``
unconditionally, so the write never touched durable storage.

This test:

* Writes a profile in subprocess A via the CLI smoke pattern
  (``python -m axiom.extensions.builtins.notifications.cli recipient set``).
* Reads it in subprocess B via ``recipient show``.
* Asserts the channels round-trip.

Skipped without a Postgres reachable at ``AXIOM_DB_URL`` — the in-memory
fallback is correctly process-local and would (by design) fail the
assertion. The whole point of this test is that the Postgres path keeps
prefs visible across process boundaries.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

CLI_MODULE = "axiom.extensions.builtins.notifications.cli"


def _pg_available() -> bool:
    try:
        import psycopg2  # type: ignore

        url = os.environ.get(
            "AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db"
        )
        conn = psycopg2.connect(url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pg_only = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault(
        "AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db"
    )
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pg_only
def test_recipient_prefs_persist_across_processes() -> None:
    handle = f"@xproc-{uuid.uuid4().hex[:8]}"
    spec = "slack=#alerts,inbox"

    try:
        # Process A: write the preference.
        set_result = _run("recipient", "set", handle, spec)
        assert set_result.returncode == 0, set_result.stderr
        assert handle in set_result.stdout

        # Process B: read it back. If prefs were in-memory only, this
        # would fail with the missing-recipient exit code (1) — the
        # HERALD-2a symptom that drove this fix.
        show_result = _run("recipient", "show", handle)
        assert show_result.returncode == 0, (
            f"cross-process read failed — prefs did not persist.\n"
            f"stdout={show_result.stdout!r}\nstderr={show_result.stderr!r}"
        )
        assert "slack" in show_result.stdout
        assert "#alerts" in show_result.stdout
        assert "inbox" in show_result.stdout
    finally:
        # No ``recipient delete`` verb yet — clean up via the store API
        # so the test leaves no residue in the shared dev Postgres.
        from axiom.extensions.builtins.notifications.preferences import (
            PostgresRecipientPreferenceStore,
        )

        PostgresRecipientPreferenceStore().delete(handle)
