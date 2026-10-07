# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A harness keeps the server it already started, and nothing said so.

The MCP server is spawned per session over stdio, so an upgrade reaches a
harness only when that harness next launches. Upgrade while Code is open and
it keeps serving the old code: the tools look installed, the version command
reports the new number, and the behaviour is the old one.

A colleague hit this during onboarding on 2026-10-01. The answer that worked
was to restart the editor, which is the right remedy and the wrong way to
find it — nothing on any surface mentioned a running server, so the fix
looked like superstition and the next person has no reason to try it.

What this module adds is the missing fact: each server start leaves a stamp
naming its version, and `axi mcp status` compares those stamps against what
is installed now. A stamp older than the install is a harness that has not
restarted, named, with the remedy next to it.

Deliberately no process inspection. Liveness would need a different API on
each platform and the colleagues who hit this are on Windows. The question is
not "is a process alive" but "the last time this harness started a server,
what did it get" — and a stamp answers that on every platform with one file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.mcp import runs


def test_a_start_leaves_a_stamp_naming_its_version(tmp_path):
    runs.record_start(node_root=tmp_path, version="0.34.0", pid=4242)
    found = runs.read_runs(node_root=tmp_path)
    assert len(found) == 1
    assert found[0].version == "0.34.0"
    assert found[0].pid == 4242


def test_the_stamp_says_which_harness_once_the_harness_says_so(tmp_path):
    """A server does not know its client until the client speaks. Before
    that the stamp still exists, unattributed, because an unattributed start
    is information and a missing one is not."""
    runs.record_start(node_root=tmp_path, version="0.34.0", pid=1)
    assert runs.read_runs(node_root=tmp_path)[0].client is None

    runs.note_client(node_root=tmp_path, pid=1, client="Visual Studio Code", client_version="1.9")
    noted = runs.read_runs(node_root=tmp_path)[0]
    assert noted.client == "Visual Studio Code"
    assert noted.client_version == "1.9"
    assert noted.version == "0.34.0", "noting the client must not lose the version"


def test_noting_a_client_for_a_start_that_left_no_stamp_is_quiet(tmp_path):
    """Observation never breaks the server. A stamp that could not be written
    must not turn the first request into an error."""
    runs.note_client(node_root=tmp_path, pid=999, client="Cursor", client_version="1")
    assert runs.read_runs(node_root=tmp_path) == []


def test_one_stamp_per_harness_so_they_cannot_accumulate(tmp_path):
    """A harness restarts many times a day. Keeping every start would make a
    directory nobody prunes; keeping the newest answers the question asked."""
    old = datetime.now(UTC) - timedelta(days=3)
    runs.record_start(node_root=tmp_path, version="0.33.0", pid=1, now=old)
    runs.note_client(node_root=tmp_path, pid=1, client="Cursor", client_version="1")
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=2)
    runs.note_client(node_root=tmp_path, pid=2, client="Cursor", client_version="1")

    found = runs.read_runs(node_root=tmp_path)
    assert len(found) == 1
    assert found[0].version == "0.35.0"
    assert found[0].pid == 2


def test_two_harnesses_are_two_stamps(tmp_path):
    """The case a single most-recent-start cannot express: one harness
    restarted and picked the upgrade up, the other did not."""
    runs.record_start(node_root=tmp_path, version="0.33.0", pid=1)
    runs.note_client(node_root=tmp_path, pid=1, client="Visual Studio Code", client_version="1")
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=2)
    runs.note_client(node_root=tmp_path, pid=2, client="Claude Code", client_version="2")

    by_client = {r.client: r.version for r in runs.read_runs(node_root=tmp_path)}
    assert by_client == {"Visual Studio Code": "0.33.0", "Claude Code": "0.35.0"}


def test_a_stamp_older_than_the_install_is_the_finding(tmp_path):
    runs.record_start(node_root=tmp_path, version="0.33.0", pid=1)
    runs.note_client(node_root=tmp_path, pid=1, client="Visual Studio Code", client_version="1")
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=2)
    runs.note_client(node_root=tmp_path, pid=2, client="Claude Code", client_version="2")

    stale = runs.stale_runs(node_root=tmp_path, installed="0.35.0")
    assert [r.client for r in stale] == ["Visual Studio Code"]
    assert stale[0].version == "0.33.0"


def test_nothing_is_stale_when_every_harness_matches(tmp_path):
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=1)
    runs.note_client(node_root=tmp_path, pid=1, client="Claude Code", client_version="2")
    assert runs.stale_runs(node_root=tmp_path, installed="0.35.0") == []


def test_no_stamps_is_not_a_claim_that_nothing_is_stale(tmp_path):
    """Absence has kinds. Nothing recorded means the question is
    unanswerable, not answered no, and the caller has to be able to tell."""
    assert runs.read_runs(node_root=tmp_path) == []
    assert runs.stale_runs(node_root=tmp_path, installed="0.35.0") == []
    assert runs.anything_recorded(node_root=tmp_path) is False

    runs.record_start(node_root=tmp_path, version="0.35.0", pid=1)
    assert runs.anything_recorded(node_root=tmp_path) is True


def test_an_unreadable_stamp_is_skipped_rather_than_fatal(tmp_path):
    """A half-written stamp from a killed process must not make `status`
    unusable — status is the command somebody runs when things are wrong."""
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=1)
    d = runs.runs_dir(node_root=tmp_path)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    found = runs.read_runs(node_root=tmp_path)
    assert [r.version for r in found] == ["0.35.0"]


def test_stamps_that_nobody_will_ever_ask_about_are_pruned(tmp_path):
    """A harness uninstalled months ago should not keep reporting stale."""
    ancient = datetime.now(UTC) - timedelta(days=90)
    runs.record_start(node_root=tmp_path, version="0.1.0", pid=1, now=ancient)
    runs.note_client(node_root=tmp_path, pid=1, client="Gone", client_version="1")
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=2)
    assert [r.version for r in runs.read_runs(node_root=tmp_path)] == ["0.35.0"]


def test_recording_a_start_never_raises_even_when_it_cannot_write(tmp_path, monkeypatch):
    """The stamp is an observation. It rides in the server's startup path, so
    a read-only or missing node root must cost nothing."""

    def _explode(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(runs.Path, "mkdir", _explode)
    assert runs.record_start(node_root=tmp_path, version="0.35.0", pid=1) is None


# ---------------------------------------------------------------------------
# Through the server: the stamp is written by the thing whose version it
# claims. A helper nobody calls is the failure mode this guards.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_listing_tools_attributes_the_run_to_the_calling_harness(tmp_path, monkeypatch):
    """`tools/list` is the first call every harness makes, which is where the
    client finally names itself."""
    from types import SimpleNamespace

    from axiom.extensions.builtins.mcp.server import build_server

    monkeypatch.setenv("AXIOM_HOME", str(tmp_path))
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=7)

    surface = _empty_surface()
    server = build_server(surface)
    entry = server.get_request_handler("tools/list")
    assert entry is not None

    ctx = SimpleNamespace(
        session=SimpleNamespace(
            client_params=SimpleNamespace(
                clientInfo=SimpleNamespace(name="Visual Studio Code", version="1.99")
            )
        )
    )
    monkeypatch.setattr(runs.os, "getpid", lambda: 7)
    await entry.handler(ctx, None)

    noted = runs.read_runs(node_root=tmp_path)
    assert noted and noted[0].client == "Visual Studio Code"


@pytest.mark.anyio
async def test_a_client_that_names_nothing_still_lists_its_tools(tmp_path, monkeypatch):
    """Attribution is best-effort. A harness that sends no clientInfo, or an
    SDK that shapes the context differently, must not lose `tools/list`."""
    from axiom.extensions.builtins.mcp.server import build_server

    monkeypatch.setenv("AXIOM_HOME", str(tmp_path))
    server = build_server(_empty_surface())
    entry = server.get_request_handler("tools/list")
    result = await entry.handler(object(), None)
    assert result.tools == []


def _empty_surface():
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from axiom.extensions.builtins.mcp.aggregation import MCPSurface

    return MCPSurface(
        tools=[],
        resources=[],
        prompts=[],
        dispatch={},
        content_hash="test",
        generated_at=_dt.now(_UTC),
        sources=[],
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
