# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TRIAGE's CLI failure listener.

The listener subscribes to `cli.arg_error` events on the bus, runs the
event through `cli_diagnoses.match_failure`, and on hit appends a
record to `~/.axi/agents/triage/pending-diagnoses.jsonl` so the
pre-command hook can surface it on next CLI invocation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.diagnostics import cli_listener
from axiom.infra.bus import EventBus


def _bonsai_event_payload() -> dict:
    return {
        "command": "chat",
        "argv": ["axi", "chat"],
        "error_type": "OSError",
        "error_message": "[Errno 22] Invalid argument: bonsai-1.7b.gguf",
        "traceback": "OSError: [Errno 22] bonsai-1.7b.gguf\n",
        "fingerprint": "chat:OSError:bonsai",
        "recovered": False,
        "environment": {"neut_version": "0.13.0"},
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.axi → tmp/.axi so listener's writes stay isolated.

    Returns the resolved state dir (tmp/.axi) so callers using `read_pending`
    or `pending_path` with an explicit `state_dir` argument hit the same
    path the listener wrote to.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Disable the LLM fallback in unit tests — listener tests target the
    # catalog-match path; LLM fallback has its own coverage in
    # test_cli_diagnoses_llm.py.
    monkeypatch.setenv("AXI_DIAGNOSES_NO_LLM", "1")
    return tmp_path / ".axi"


@pytest.fixture
def bus(tmp_path: Path) -> EventBus:
    return EventBus(log_path=tmp_path / "events.jsonl")


class TestListenerWritesPendingDiagnosis:
    def test_bonsai_event_writes_pending_diagnosis(
        self, bus: EventBus, state_dir: Path
    ) -> None:
        cli_listener.register(bus)

        bus.publish("cli.arg_error", _bonsai_event_payload(), source="test")

        pending = cli_listener.read_pending(state_dir)
        assert len(pending) == 1
        assert pending[0]["pattern_id"] == "bonsai-deprecated"
        assert "qwen" in pending[0]["remedy"].lower()

    def test_unmatched_event_writes_nothing(
        self, bus: EventBus, state_dir: Path
    ) -> None:
        cli_listener.register(bus)

        bus.publish(
            "cli.arg_error",
            {
                "command": "ext",
                "error_type": "FileNotFoundError",
                "error_message": "manifest.toml missing",
                "traceback": "",
                "fingerprint": "ext:FileNotFoundError",
                "recovered": False,
                "environment": {},
                "timestamp": datetime.now(UTC).isoformat(),
            },
            source="test",
        )

        assert cli_listener.read_pending(state_dir) == []

    def test_duplicate_events_dedupe_by_fingerprint(
        self, bus: EventBus, state_dir: Path
    ) -> None:
        """Two identical bonsai failures shouldn't pile up two diagnoses."""
        cli_listener.register(bus)

        bus.publish("cli.arg_error", _bonsai_event_payload(), source="test")
        bus.publish("cli.arg_error", _bonsai_event_payload(), source="test")

        pending = cli_listener.read_pending(state_dir)
        assert len(pending) == 1


class TestPendingLifecycle:
    def test_clear_by_fingerprint_removes_one_entry(self, state_dir: Path) -> None:
        cli_listener.append_diagnosis(
            state_dir,
            {"pattern_id": "bonsai-deprecated", "fingerprint": "abc123",
             "summary": "x", "remedy": "y", "confidence": 0.9,
             "matched_at": "2026-05-03T20:00:00Z"},
        )
        cli_listener.append_diagnosis(
            state_dir,
            {"pattern_id": "other", "fingerprint": "xyz789",
             "summary": "x", "remedy": "y", "confidence": 0.9,
             "matched_at": "2026-05-03T20:00:00Z"},
        )

        cli_listener.clear_pending(state_dir, fingerprint="abc123")

        remaining = cli_listener.read_pending(state_dir)
        assert len(remaining) == 1
        assert remaining[0]["fingerprint"] == "xyz789"

    def test_clear_all_empties_log(self, state_dir: Path) -> None:
        cli_listener.append_diagnosis(
            state_dir,
            {"pattern_id": "bonsai-deprecated", "fingerprint": "abc123",
             "summary": "x", "remedy": "y", "confidence": 0.9,
             "matched_at": "2026-05-03T20:00:00Z"},
        )

        cli_listener.clear_pending(state_dir, fingerprint=None)

        assert cli_listener.read_pending(state_dir) == []

    def test_read_pending_handles_missing_file(self, state_dir: Path) -> None:
        # Fresh ~/.axi → no log yet → empty list, not error.
        assert cli_listener.read_pending(state_dir) == []

    def test_read_pending_skips_corrupt_lines(self, state_dir: Path) -> None:
        path = cli_listener.pending_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A real diagnosis followed by a corrupt line and another real one.
        path.write_text(
            json.dumps({"pattern_id": "bonsai-deprecated", "fingerprint": "a",
                        "summary": "s", "remedy": "r", "confidence": 0.9,
                        "matched_at": "2026-05-03T20:00:00Z"}) + "\n"
            "{not json\n" +
            json.dumps({"pattern_id": "other", "fingerprint": "b",
                        "summary": "s", "remedy": "r", "confidence": 0.9,
                        "matched_at": "2026-05-03T20:00:00Z"}) + "\n"
        )

        pending = cli_listener.read_pending(state_dir)
        # Both valid entries returned; corrupt line silently skipped.
        assert {p["fingerprint"] for p in pending} == {"a", "b"}
