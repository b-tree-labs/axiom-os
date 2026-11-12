# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The alert desk's data model (ADR-249).

An :class:`Alert` is what system code publishes: an event, never a message. The
desk groups alerts with one :attr:`Alert.fingerprint` into an :class:`Incident`,
and a person is shown a :class:`Rendered` incident. Everything a person reads is
written from these fields, never from raw log text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Priority = Literal["low", "normal", "high", "urgent"]
IncidentState = Literal["holding", "healing", "healed", "notified", "snoozed", "muted", "resolved"]


@dataclass(frozen=True)
class Unmet:
    """A requirement the failed work needed (ADR-185): what, why, and the fix."""

    id: str
    why: str
    fix: str = ""


@dataclass(frozen=True)
class Alert:
    """One occurrence, as system code publishes it on ``alert.<area>.<kind>``."""

    area: str  # e.g. "data", "schedule", "node"
    kind: str  # e.g. "dispatch_dead_letter", "feed_stale"
    action: str  # the work that failed, e.g. "data.refresh"
    subject: str  # what it was about, e.g. a connector or feed name
    error_class: str  # e.g. "MissingConfiguration", "RuntimeError"
    summary: str  # one plain sentence, no stack trace, no paths
    priority: Priority = "high"
    owner_agent: str = ""  # handle of the agent that owns the area, e.g. "@plinth"
    unmet: tuple[Unmet, ...] = ()
    evidence_ref: str = ""  # where the full record lives (log id, receipt id, path on the node)
    detail: dict[str, Any] = field(default_factory=dict)  # structured extras; never shown raw
    occurred_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """One cause: area, kind, action, error class and the unmet requirement ids.

        The subject is NOT part of it, so eight subjects failing for one reason
        are one incident."""
        unmet = ",".join(sorted(u.id for u in self.unmet))
        return f"{self.area}.{self.kind}|{self.action}|{self.error_class}|{unmet}"


@dataclass
class Attempt:
    """One remedy the desk ran before telling anyone (ADR-249 D3)."""

    remedy: str  # capability name
    at: datetime
    ok: bool
    outcome: str  # one plain sentence


@dataclass
class Incident:
    """One cause, with every subject it affected, and what was done about it."""

    id: str  # "inc-<12 hex>"
    fingerprint: str
    area: str
    kind: str
    action: str
    error_class: str
    priority: Priority
    owner_agent: str
    summary: str  # the first alert's summary
    unmet: tuple[Unmet, ...]
    subjects: list[str]  # ordered, unique
    first_seen: datetime
    last_seen: datetime
    occurrences: int
    state: IncidentState = "holding"
    hold_until: datetime | None = None
    attempts: list[Attempt] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    reopened: int = 0  # times it recurred after being resolved
    notified_at: datetime | None = None
    snoozed_until: datetime | None = None
    notes: list[str] = field(default_factory=list)  # "who: note" from person actions


@dataclass(frozen=True)
class Rendered:
    """What a person reads (ADR-249 D4): short, plain, with a link to the record."""

    subject: str  # email subject line, <= 90 chars, no "[HIGH]" prefix, no exception text
    body_text: str  # <= 5 sentences + the action lines
    body_html: str  # the same content as simple HTML
    incident_url: str  # the incident page
    written_by: Literal["model", "template"]


__all__ = [
    "Alert",
    "Attempt",
    "Incident",
    "IncidentState",
    "Priority",
    "Rendered",
    "Unmet",
]
