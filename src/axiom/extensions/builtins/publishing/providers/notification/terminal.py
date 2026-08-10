# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0


# DEPRECATED 2026-06-01 (v0.30) per ADR-059 + ADR-060: publishing emits
# events on the EventBus; HERALD's agent_bridge routes them through the
# connector adapters. This module survives only for backwards-compat
# during the v0.30 transition and is REMOVED IN v0.31.
"""TerminalNotificationProvider — stdout + macOS notifications.

Prints notifications to stdout. On macOS, tries to use pync for
native notification center integration, falls back to print.
"""

from __future__ import annotations

from typing import Any

from ...factory import PublisherFactory
from ..base import NotificationProvider


class TerminalNotificationProvider(NotificationProvider):
    """Terminal/stdout notification provider with optional macOS notifications."""

    def __init__(self, config: dict[str, Any] | None = None):
        self._pync_available = False
        try:
            import pync  # type: ignore  # noqa: F401
            self._pync_available = True
        except ImportError:
            pass

    def send(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        urgency: str = "normal",
    ) -> bool:
        """Print notification to stdout and optionally trigger macOS notification."""
        # Urgency indicator
        indicators = {"low": ".", "normal": "-", "high": "!"}
        indicator = indicators.get(urgency, "-")

        print(f"[{indicator}] {subject}")
        if body:
            for line in body.strip().splitlines():
                print(f"    {line}")
        if recipients:
            print(f"    To: {', '.join(recipients)}")

        # macOS notification
        if self._pync_available and urgency in ("normal", "high"):
            try:
                import pync  # type: ignore
                pync.notify(body[:200], title=subject)
            except Exception:
                pass  # Non-fatal

        return True


# Self-register with factory
PublisherFactory.register("notification", "terminal", TerminalNotificationProvider)
