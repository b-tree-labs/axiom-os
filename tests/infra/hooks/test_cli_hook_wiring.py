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
