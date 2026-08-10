# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pre-command pending-diagnosis surface.

Closes the loop the user described 2026-05-03: TRIAGE writes pending
diagnoses on cli.arg_error; the next CLI invocation reads them and
prompts the user before dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.diagnostics import cli_listener
from axiom.infra import cli_hooks


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".axi"


def _seed_diagnosis(state_dir: Path, *, fingerprint: str = "abc123") -> None:
    cli_listener.append_diagnosis(
        state_dir,
        {
            "pattern_id": "bonsai-deprecated",
            "fingerprint": fingerprint,
            "summary": "Bonsai gguf missing",
            "remedy": "Edit llm-providers.toml: replace bonsai-local with qwen-local.",
            "confidence": 0.95,
            "matched_at": "2026-05-03T20:00:00Z",
        },
    )


class TestSurfacePendingDiagnoses:
    def test_no_pending_writes_nothing(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        cli_hooks.surface_pending_diagnoses()

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_pending_diagnosis_writes_to_stderr(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _seed_diagnosis(state_dir)

        cli_hooks.surface_pending_diagnoses()

        captured = capsys.readouterr()
        # stderr-only — never stdout (machine-readable command output
        # must remain clean for piping).
        assert captured.out == ""
        assert "TRIAGE" in captured.err
        assert "Bonsai" in captured.err
        assert "qwen-local" in captured.err

    def test_includes_clear_hint(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """User must learn how to dismiss the diagnosis once acted on."""
        _seed_diagnosis(state_dir, fingerprint="abc123")

        cli_hooks.surface_pending_diagnoses()

        captured = capsys.readouterr()
        # The clear command should appear so the user knows how to acknowledge.
        assert "axi triage clear" in captured.err
        # The fingerprint should be quotable for clear-by-id.
        assert "abc123" in captured.err

    def test_multiple_diagnoses_each_listed(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _seed_diagnosis(state_dir, fingerprint="aaa")
        cli_listener.append_diagnosis(
            state_dir,
            {
                "pattern_id": "other-pattern",
                "fingerprint": "bbb",
                "summary": "Other thing",
                "remedy": "Do other.",
                "confidence": 0.7,
                "matched_at": "2026-05-03T20:01:00Z",
            },
        )

        cli_hooks.surface_pending_diagnoses()

        captured = capsys.readouterr()
        assert "Bonsai" in captured.err
        assert "Other thing" in captured.err

    def test_corrupt_log_does_not_break_dispatch(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A corrupt pending-diagnoses file must not break the next command."""
        path = cli_listener.pending_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage not json\n")

        # Should not raise.
        cli_hooks.surface_pending_diagnoses()

    def test_silent_when_axi_diagnoses_quiet_set(
        self, state_dir: Path, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Opt-out flag suppresses surfacing without clearing the log,
        so users can dismiss noise without losing the audit trail."""
        _seed_diagnosis(state_dir)
        monkeypatch.setenv("AXI_DIAGNOSES_QUIET", "1")

        cli_hooks.surface_pending_diagnoses()

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_pending_diagnoses_persist_after_surface(
        self, state_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Surfacing does NOT auto-clear — only explicit `clear` does.
        Otherwise a single CLI run would lose the diagnosis the user
        hasn't yet acted on."""
        _seed_diagnosis(state_dir, fingerprint="abc123")

        cli_hooks.surface_pending_diagnoses()

        # Still there for the next invocation if they didn't fix it yet.
        remaining = cli_listener.read_pending(state_dir)
        assert len(remaining) == 1
        assert remaining[0]["fingerprint"] == "abc123"
