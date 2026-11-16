# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi memory`` CLI.

Read-only surface over a principal's CompositionService ledger.
Uses a classroom composition service (the natural runtime container)
for test isolation; ``axi memory show --classroom-id <id>`` is the
test-friendly path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.memory.cli import main


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    yield


def _seed_classroom_history(classroom_id: str, principal: str, events: list[str]) -> None:
    """Write one episodic fragment per event via the classroom composition."""
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    comp = build_classroom_composition(classroom_id)
    for msg in events:
        comp.write(
            content={
                "event_time": datetime.now(UTC).isoformat(),
                "fact_kind": "session_event",
                "summary": msg,
            },
            cognitive_type="episodic",
            principal_id=principal,
            agents=set(),
            resources=set(),
        )


class TestMemoryShowCLI:
    def test_empty_principal_returns_zero_fragments(self, capsys):
        rc = main(
            [
                "show", "@alice:demo",
                "--classroom-id", "c1",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["principal"] == "@alice:demo"
        assert data["fragment_count"] == 0
        assert data["summary"] == ""

    def test_shows_principal_fragments(self, capsys):
        _seed_classroom_history(
            "c1", "@alice:demo",
            [
                "Worked on Newton's 2nd law",
                "Quiz: conservation of momentum",
            ],
        )
        rc = main(
            [
                "show", "@alice:demo",
                "--classroom-id", "c1",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fragment_count"] == 2
        summaries = {f["summary"] for f in data["fragments"]}
        assert "Worked on Newton's 2nd law" in summaries

    def test_respects_limit_flag(self, capsys):
        _seed_classroom_history(
            "c1", "@alice:demo",
            [f"session {i}" for i in range(5)],
        )
        rc = main(
            [
                "show", "@alice:demo",
                "--classroom-id", "c1",
                "--limit", "2",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fragment_count"] == 2

    def test_isolation_between_principals(self, capsys):
        _seed_classroom_history("c1", "@alice:demo", ["alice topic"])
        _seed_classroom_history("c1", "@bob:demo", ["bob topic"])

        rc = main(
            ["show", "@alice:demo", "--classroom-id", "c1", "--json"]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fragment_count"] == 1
        assert data["fragments"][0]["summary"] == "alice topic"

    def test_markdown_output_lists_events(self, capsys):
        _seed_classroom_history(
            "c1", "@alice:demo", ["Quiz: conservation of momentum"]
        )
        rc = main(
            ["show", "@alice:demo", "--classroom-id", "c1"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "@alice:demo" in out
        assert "conservation" in out
        # Composed summary block should appear since there's content
        assert "Composed session-memory summary" in out

    def test_markdown_output_empty_principal(self, capsys):
        rc = main(
            ["show", "@nobody:demo", "--classroom-id", "c1"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "No prior fragments" in out
