# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A withhold names the code that produced it, and says when that code is stale (ADR-202).

An MCP server imports its code once and the install moves underneath it. On
2026-10-27 the server serving a session had been started on 2026-10-05, three
weeks earlier, and was still withholding memory writes on a rule ADR-175 had
replaced the next day. The response said "classifier: ollama" and nothing else;
the person found out by comparing process start times with commit dates.

Pinned here:

1. Every withhold carries the server's revision, and the same revision lands in
   the audit record for the id the client is handed.
2. When the install has moved since the server loaded, the withhold says so and
   names both revisions and the remedy (restart the harness).
3. A fresh server is not called stale.
4. A failed audit write is reported in the envelope, never swallowed.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp import routing as R
from axiom.extensions.builtins.mcp import server
from axiom.infra import revision, routing_audit


def _run(coro):
    return asyncio.run(coro)


def _withhold(audit_path):
    async def _disp(name, args):
        return {"payload": "x", "classification": "export_controlled"}

    return _run(
        R.gate_result_for_client(
            tool_name="some_read", arguments={"q": "x"}, dispatcher=_disp,
            client_ec_capable=False, client_name="claude-code",
        )
    )


def test_a_withhold_carries_the_servers_revision(tmp_path, monkeypatch):
    audit = tmp_path / "routing_audit.jsonl"
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", audit)

    env = _withhold(audit)
    routing = env["routing"]
    assert "result" not in env
    srv = routing["server"]
    assert srv is not None and srv["revision"], "a withhold names the code that produced it"
    assert srv["revision"] == revision.loaded_revision().short
    assert srv["stale"] is False
    found = routing_audit.find_routing_decision(routing["routing_event_id"])
    assert found is not None
    assert found["revision"] == srv["revision"]
    assert found["stale"] is False


def test_a_withhold_from_stale_code_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", tmp_path / "routing_audit.jsonl")
    old = revision.Revision(version="0.70.2", source="/old/src/axiom", commit="a" * 40)
    new = revision.Revision(version="0.70.2", source="/old/src/axiom", commit="b" * 40)
    monkeypatch.setattr(revision, "_loaded", old)
    monkeypatch.setattr(revision, "current_revision", lambda: new)

    env = _withhold(tmp_path)
    srv = env["routing"]["server"]
    assert srv["stale"] is True
    assert srv["revision"] == old.short and srv["current"] == new.short

    payload = server._withheld_payload(env["routing"])
    error = payload["error"]
    assert error.startswith("export-controlled content withheld")
    assert old.short in error and new.short in error
    assert "restart" in error.lower()
    assert "replaced rule" in error


def test_a_fresh_server_is_not_called_stale():
    state = revision.staleness()
    assert state["stale"] is False
    assert state["loaded"] == state["current"]


def test_the_same_version_at_another_commit_is_stale(monkeypatch):
    monkeypatch.setattr(
        revision, "_loaded", revision.Revision(version="1.0.0", source="s", commit="1" * 40)
    )
    monkeypatch.setattr(
        revision, "current_revision",
        lambda: revision.Revision(version="1.0.0", source="s", commit="2" * 40),
    )
    assert revision.staleness()["stale"] is True


def test_an_unknown_commit_is_not_a_finding(monkeypatch):
    monkeypatch.setattr(
        revision, "_loaded", revision.Revision(version="1.0.0", source="s", commit=None)
    )
    monkeypatch.setattr(
        revision, "current_revision",
        lambda: revision.Revision(version="1.0.0", source="s", commit="2" * 40),
    )
    assert revision.staleness()["stale"] is False


def test_an_audit_write_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(routing_audit, "log_routing_decision", _boom)
    env = _withhold(tmp_path)
    routing = env["routing"]
    assert "result" not in env, "the withhold stands whether or not it was recorded"
    assert routing["audit_recorded"] is False
    assert "OSError" in routing["audit_error"]
    assert "not on the audit record" in server._withheld_payload(routing)["error"]


def test_an_unwritable_audit_log_is_reported_not_swallowed(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed", encoding="utf-8")
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", blocker / "routing_audit.jsonl")

    with caplog.at_level("WARNING", logger="axiom.infra.routing_audit"):
        env = _withhold(tmp_path)
    routing = env["routing"]
    assert routing["audit_recorded"] is False
    assert routing["audit_error"]
    assert any("routing audit" in rec.getMessage() for rec in caplog.records)
