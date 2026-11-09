# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who called what, over which surface — including an external harness.

An operator asked whether an external harness talking to a service is
tracked anywhere. It is: `record_capability_event` runs under the CLI, the
root MCP server and the memory MCP server, so an MCP call is captured with
`surface="mcp"`. Nothing had ever shown it, which is why neither the
operator nor the harness reading the status output knew.

The ledger is PARTIAL by construction — it sees what passes through
`invoke_capability`, and roughly half the builtin CLIs do not route there
yet. That is the property most worth testing, because a partial ledger
presented as a total is what turns "no rows" into "it never ran".
"""

from __future__ import annotations

import json

from axiom.infra.capability_telemetry import access_ledger


def _write(tmp_path, rows):
    p = tmp_path / "telemetry" / "capabilities.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


class TestItSeparatesTheSurfaces:
    def test_an_mcp_call_is_attributed_to_mcp(self, tmp_path):
        _write(tmp_path, [
            {"ts": 100.0, "tool_name": "vault.audit", "surface": "mcp",
             "principal": "@harness:x", "ok": True},
        ])
        (row,) = access_ledger(state_dir=tmp_path)
        assert row["surfaces"] == {"mcp": 1}
        assert row["principals"] == ["@harness:x"]

    def test_both_surfaces_on_one_extension_are_counted_apart(self, tmp_path):
        _write(tmp_path, [
            {"ts": 1.0, "tool_name": "data.list", "surface": "cli", "ok": True},
            {"ts": 2.0, "tool_name": "data.list", "surface": "mcp", "ok": True},
            {"ts": 3.0, "tool_name": "data.list", "surface": "mcp", "ok": True},
        ])
        (row,) = access_ledger(state_dir=tmp_path)
        assert row["surfaces"] == {"cli": 1, "mcp": 2}
        assert row["calls"] == 3

    def test_rows_are_grouped_by_extension_not_by_verb(self, tmp_path):
        _write(tmp_path, [
            {"ts": 1.0, "tool_name": "secrets.set", "surface": "cli", "ok": True},
            {"ts": 2.0, "tool_name": "secrets.list", "surface": "cli", "ok": True},
        ])
        (row,) = access_ledger(state_dir=tmp_path)
        assert row["extension"] == "secrets" and row["calls"] == 2


class TestItCarriesNothingSensitive:
    def test_no_arguments_or_digests_reach_the_summary(self, tmp_path):
        """A row upstream holds an args_digest. A summary does not need even
        that, and a status view is pasted into tickets."""
        _write(tmp_path, [
            {"ts": 1.0, "tool_name": "secrets.get", "surface": "cli", "ok": True,
             "args_digest": "deadbeef", "error": "boom"},
        ])
        (row,) = access_ledger(state_dir=tmp_path)
        assert "args_digest" not in row
        assert "error" not in row


class TestFailuresAndRecency:
    def test_a_failed_call_is_counted_as_one(self, tmp_path):
        _write(tmp_path, [
            {"ts": 1.0, "tool_name": "vault.issue", "surface": "cli", "ok": False},
            {"ts": 2.0, "tool_name": "vault.issue", "surface": "cli", "ok": True},
        ])
        (row,) = access_ledger(state_dir=tmp_path)
        assert row["failures"] == 1 and row["calls"] == 2

    def test_most_recently_used_comes_first(self, tmp_path):
        _write(tmp_path, [
            {"ts": 1.0, "tool_name": "old.verb", "surface": "cli", "ok": True},
            {"ts": 9.0, "tool_name": "new.verb", "surface": "cli", "ok": True},
        ])
        assert [r["extension"] for r in access_ledger(state_dir=tmp_path)] == ["new", "old"]

    def test_a_since_filter_bounds_the_window(self, tmp_path):
        _write(tmp_path, [
            {"ts": 1.0, "tool_name": "old.verb", "surface": "cli", "ok": True},
            {"ts": 9.0, "tool_name": "new.verb", "surface": "cli", "ok": True},
        ])
        assert [r["extension"] for r in access_ledger(state_dir=tmp_path, since=5.0)] == ["new"]


class TestItSurvivesABadLedger:
    def test_an_unparseable_line_does_not_lose_the_rest(self, tmp_path):
        p = tmp_path / "telemetry" / "capabilities.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text('{"broken\n{"ts": 2.0, "tool_name": "a.b", "surface": "cli"}\n',
                     encoding="utf-8")
        assert [r["extension"] for r in access_ledger(state_dir=tmp_path)] == ["a"]

    def test_a_row_with_no_timestamp_is_skipped_not_fatal(self, tmp_path):
        _write(tmp_path, [{"tool_name": "a.b", "surface": "cli", "ok": True}])
        assert access_ledger(state_dir=tmp_path) == []

    def test_no_ledger_at_all_is_an_empty_answer(self, tmp_path):
        assert access_ledger(state_dir=tmp_path) == []
