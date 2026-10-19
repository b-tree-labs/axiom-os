# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration: cli.command_started / cli.command_ended fire at the dispatch."""

from __future__ import annotations

from axiom.infra.bus import EventBus
from axiom.infra.cli_hooks import publish_command_ended, publish_command_started


class TestCliHookFiring:
    def test_command_started_event_shape(self):
        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(
            "cli.command_started",
            lambda subject, payload: captured.append((subject, dict(payload))),
        )

        publish_command_started(
            command_path="ext fix",
            args=["--dry-run"],
            principal="@me:axiom",
            eventbus=bus,
        )
        assert captured
        subject, payload = captured[0]
        assert subject == "cli.command_started"
        assert payload["command_path"] == "ext fix"
        assert payload["args"] == ["--dry-run"]
        assert payload["principal"] == "@me:axiom"
        assert "started_at" in payload

    def test_command_ended_event_shape(self):
        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(
            "cli.command_ended",
            lambda subject, payload: captured.append((subject, dict(payload))),
        )

        publish_command_ended(
            command_path="ext fix",
            exit_code=0,
            duration_ms=42,
            eventbus=bus,
        )
        assert captured
        subject, payload = captured[0]
        assert subject == "cli.command_ended"
        assert payload["exit_code"] == 0
        assert payload["duration_ms"] == 42
        assert "ended_at" in payload

    def test_no_throw_when_eventbus_none(self):
        # Soft semantics: passing None for the bus must never raise. Used
        # by axiom_cli.main as the early-startup default before the bus
        # is wired.
        publish_command_started(
            command_path="ext",
            args=[],
            principal="@p:c",
            eventbus=None,
        )
        publish_command_ended(
            command_path="ext",
            exit_code=0,
            duration_ms=1,
            eventbus=None,
        )


class TestTheBannerStaysOffItsOwnSurface:
    """`axi triage pending` used to print the banner above its own output.

    The banner ends with "Read the remedy: axi triage pending", so on that
    command it instructed the reader to run the command they were already
    running — and then the command printed the same diagnoses underneath it.
    """

    @staticmethod
    def _emit(command, monkeypatch, capsys):
        from axiom.extensions.builtins.diagnostics import cli_listener
        from axiom.infra import cli_hooks

        monkeypatch.delenv("AXI_DIAGNOSES_QUIET", raising=False)
        monkeypatch.setattr(
            cli_listener,
            "read_pending",
            lambda *a, **k: [
                {"fingerprint": "abc123", "summary": "a thing broke", "confidence": 0.6}
            ],
        )
        cli_hooks.surface_pending_diagnoses(command=command)
        return capsys.readouterr().err

    def test_it_is_silent_on_the_triage_command(self, monkeypatch, capsys):
        assert "TRIAGE" not in self._emit("triage", monkeypatch, capsys)

    def test_it_is_silent_on_a_triage_subcommand(self, monkeypatch, capsys):
        assert "TRIAGE" not in self._emit("triage pending", monkeypatch, capsys)

    def test_it_still_fires_on_everything_else(self, monkeypatch, capsys):
        """Suppressing it everywhere would be worse than the bug it fixes."""
        for command in ("status", "connector", "ext", ""):
            out = self._emit(command, monkeypatch, capsys)
            assert "TRIAGE" in out, f"banner lost on {command!r}"

    def test_a_command_merely_starting_with_the_letters_is_not_suppressed(
        self, monkeypatch, capsys
    ):
        assert "TRIAGE" in self._emit("triager", monkeypatch, capsys)
