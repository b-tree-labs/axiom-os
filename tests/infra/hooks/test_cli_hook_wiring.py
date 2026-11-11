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


class TestPendingDiagnosesStayOnTheTriageSurface:
    """Pending diagnoses are pointed at on `axi triage`, and nowhere else.

    They used to print ahead of every command, `--help` included: sixteen
    stale entries, seven of them leaked test fixtures, in front of whatever
    the operator typed (#1520). `axi triage pending` lists them in full, so it
    gets no pointer either; nor do `clear` (manages them) and `heartbeat`
    (the daemon path).
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

    def test_every_other_command_runs_without_a_preamble(self, monkeypatch, capsys):
        for command in ("status", "connector", "ext", "hygiene", "context check", ""):
            out = self._emit(command, monkeypatch, capsys)
            assert "TRIAGE" not in out, f"banner printed ahead of {command!r}"

    def test_the_triage_command_points_at_them(self, monkeypatch, capsys):
        out = self._emit("triage", monkeypatch, capsys)
        assert "TRIAGE" in out and "abc123" in out and "triage pending" in out

    def test_the_verbs_that_show_or_manage_the_queue_get_no_pointer(
        self, monkeypatch, capsys
    ):
        for command in ("triage pending", "triage clear", "triage heartbeat"):
            assert "TRIAGE" not in self._emit(command, monkeypatch, capsys), command

    def test_a_command_merely_starting_with_the_letters_gets_nothing(
        self, monkeypatch, capsys
    ):
        assert "TRIAGE" not in self._emit("triager", monkeypatch, capsys)
