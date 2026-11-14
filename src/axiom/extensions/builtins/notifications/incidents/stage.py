# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The incident stage (ADR-249 D2, D6; ADR-252): one incident per cause, held, then sent once.

There is one notification path. ``notifications.send(..., cause=Cause(...))``
hands a notification about a detected condition to this stage instead of
delivering it. The stage groups notifications by :attr:`Cause.fingerprint`
into one :class:`~.model.Incident`, holds it for a window so the rest of the
cause's subjects can join, asks the heal seam to try first (ADR-249 D3,
filled in a later phase), and only then delivers it, once, through the same
``send`` with the cause stripped. Repeats update the open incident and send
nothing; a recurrence after it closed reopens the same one. ``normal`` and
``low`` are never sent on their own: they wait for the daily digest.

The stage is deterministic: the clock, the heal seam and the delivery seam are
injected, and every decision reads and writes the durable store, so a new
stage over the same store (a restart, or another process) carries on where the
last one stopped.
"""

from __future__ import annotations

import html
import inspect
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from .model import Attempt, Cause, Incident, Rendered
from .store import IncidentStore

_log = logging.getLogger("axiom.notifications.incidents")

DEFAULT_HOLD = timedelta(minutes=10)
DEFAULT_URGENT_HOLD = timedelta(minutes=1)
DEFAULT_RESOLVE_AFTER = timedelta(hours=48)
DEFAULT_DIGEST_WINDOW = timedelta(hours=24)

OPEN_STATES = ("holding", "healing", "notified", "snoozed", "muted")
CLOSED_STATES = ("healed", "resolved")
_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
_SENT_NOW = ("urgent", "high")
_CLASS_RANK = {"public": 0, "internal": 1, "regulated": 2, "controlled": 3}

HealFn = Callable[[Incident], list[Attempt]]
DeliverFn = Callable[[Incident, Rendered], object]


def _no_heal(incident: Incident) -> list[Attempt]:
    """No remedies run yet (ADR-249 D3 comes later): nothing is tried, and the message says so."""
    return []


@dataclass
class Digest:
    """What the daily digest covers (ADR-249 D6)."""

    since: datetime
    until: datetime
    healed: list[Incident] = field(default_factory=list)
    resolved: list[Incident] = field(default_factory=list)
    open_quiet: list[Incident] = field(default_factory=list)
    """``normal`` / ``low`` incidents past their hold: never sent on their own."""
    open_told: list[Incident] = field(default_factory=list)
    """Incidents a person was already sent that are still open."""

    @property
    def quiet(self) -> bool:
        return not (self.healed or self.resolved or self.open_quiet or self.open_told)


class IncidentStage:
    """Groups notifications that name a cause into incidents, and decides when, if ever, one goes out."""

    def __init__(
        self,
        store: IncidentStore,
        *,
        clock: Callable[[], datetime] | None = None,
        heal: HealFn | None = None,
        deliver: DeliverFn | None = None,
        hold: timedelta = DEFAULT_HOLD,
        urgent_hold: timedelta = DEFAULT_URGENT_HOLD,
        resolve_after: timedelta = DEFAULT_RESOLVE_AFTER,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._heal = heal or _no_heal
        self._deliver = deliver or self._send
        self._hold = hold
        self._urgent_hold = urgent_hold
        self._resolve_after = resolve_after

    @property
    def store(self) -> IncidentStore:
        return self._store

    def _hold_for(self, priority: str) -> timedelta:
        return self._urgent_hold if priority == "urgent" else self._hold

    # ── intake ───────────────────────────────────────────────────────────
    def record(
        self,
        cause: Cause,
        *,
        summary: str,
        priority: str = "high",
        actor: str = "",
        recipient: str = "@operator",
        classification: str = "internal",
        intent: str = "",
    ) -> str:
        """Fold one notification about ``cause`` into its incident. Returns the
        incident id. Sends nothing."""
        now = self._clock()
        delivery = {
            "recipient": recipient,
            "classification": classification,
            "intent": intent or f"incident.{cause.area}.{cause.kind}",
        }
        existing = self._store.by_fingerprint(cause.fingerprint)
        if existing is None:
            incident = _new_incident(cause, summary, priority, actor, now, now + self._hold_for(priority))
            try:
                self._store.insert(incident, delivery=delivery)
                return incident.id
            except IntegrityError:
                # Another process opened this cause between our read and write.
                existing = self._store.by_fingerprint(cause.fingerprint)
                if existing is None:  # pragma: no cover - the row we collided with vanished
                    raise
        self._fold(existing, cause, priority, delivery, now)
        return existing.id

    def _fold(self, inc: Incident, cause: Cause, priority: str, delivery: dict, now: datetime) -> None:
        if cause.subject and cause.subject not in inc.subjects:
            inc.subjects.append(cause.subject)
        if cause.evidence_ref and cause.evidence_ref not in inc.evidence_refs:
            inc.evidence_refs.append(cause.evidence_ref)
        if cause.remedy and not inc.remedy:
            inc.remedy = cause.remedy
        inc.occurrences += 1
        inc.last_seen = now
        # Priority only rises: a cause that became urgent stays urgent.
        if _RANK.get(priority, 1) > _RANK.get(inc.priority, 1):
            inc.priority = priority  # type: ignore[assignment]
            if inc.state == "holding" and inc.hold_until is not None:
                inc.hold_until = min(inc.hold_until, now + self._hold_for(inc.priority))
        if inc.state in CLOSED_STATES:
            # Reopen the same incident, once more through one hold window.
            inc.notes.append(f"@herald: recurred after it was {inc.state}")
            inc.reopened += 1
            inc.state = "holding"
            inc.hold_until = now + self._hold_for(inc.priority)
            inc.notified_at = None
        # Classification only tightens: the held message goes out under the
        # strictest label any of its notifications carried.
        stored = self._store.delivery(inc.id)
        merged = None
        if _CLASS_RANK.get(delivery["classification"], 1) > _CLASS_RANK.get(stored.get("classification", "internal"), 1):
            merged = {**stored, "classification": delivery["classification"]}
        self._store.save(inc, delivery=merged)

    # ── the clock ────────────────────────────────────────────────────────
    def tick(self, now: datetime | None = None) -> dict[str, int]:
        """Resolve quiet incidents, then act on every hold that has run out."""
        now = now or self._clock()
        report = {"resolved": 0, "healed": 0, "notified": 0, "kept_for_digest": 0, "failed": 0}
        for inc in self._store.in_states(OPEN_STATES):
            if inc.last_seen + self._resolve_after <= now:
                inc.state = "resolved"
                inc.hold_until = None
                quiet_hours = int(self._resolve_after.total_seconds() // 3600)
                inc.notes.append(f"@herald: resolved, no occurrence in {quiet_hours} h")
                self._store.save(inc, closed_at=now)
                report["resolved"] += 1
        for inc in self._store.in_states(("holding",)):
            if inc.hold_until is None or inc.hold_until > now:
                continue
            report[self._release(inc, now)] += 1
        return report

    def _release(self, inc: Incident, now: datetime) -> str:
        try:
            attempts = list(self._heal(inc) or [])
        except Exception as exc:  # noqa: BLE001 - a broken remedy is a failed attempt
            _log.warning("heal seam raised for %s: %s", inc.id, type(exc).__name__)
            attempts = [Attempt(remedy=inc.remedy or "heal", at=now, ok=False, outcome="The remedy could not run.")]
        inc.attempts.extend(attempts)
        if any(a.ok for a in attempts):
            inc.state = "healed"
            inc.hold_until = None
            self._store.save(inc, closed_at=now)
            return "healed"
        if inc.priority not in _SENT_NOW:
            inc.hold_until = None  # held for the digest; no longer waiting on a window
            self._store.save(inc)
            return "kept_for_digest"
        try:
            self._deliver(inc, render(inc))
        except Exception as exc:  # noqa: BLE001 - stay holding; the next tick retries
            _log.warning("delivery failed for %s: %s", inc.id, type(exc).__name__)
            self._store.save(inc)
            return "failed"
        inc.state = "notified"
        inc.notified_at = now
        inc.hold_until = None
        self._store.save(inc)
        return "notified"

    def _send(self, incident: Incident, rendered: Rendered) -> object:
        return send_incident(incident, rendered, self._store.delivery(incident.id))

    # ── person actions ───────────────────────────────────────────────────
    def resolve(self, incident_id: str, by: str, note: str = "") -> Incident:
        inc = self._store.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        now = self._clock()
        inc.state = "resolved"
        inc.hold_until = None
        inc.notes.append(f"{by}: {note}" if note else f"{by}: resolved")
        self._store.save(inc, closed_at=now)
        return inc

    # ── digest ───────────────────────────────────────────────────────────
    def digest(self, *, window: timedelta = DEFAULT_DIGEST_WINDOW) -> Digest:
        now = self._clock()
        since = now - window
        d = Digest(since=since, until=now)
        for inc in self._store.closed_since(since):
            (d.healed if inc.state == "healed" else d.resolved).append(inc)
        for inc in self._store.in_states(OPEN_STATES):
            if inc.state == "holding" and inc.hold_until is None:
                d.open_quiet.append(inc)
            elif inc.state in ("notified", "snoozed", "muted"):
                d.open_told.append(inc)
        return d

    @staticmethod
    def digest_text(d: Digest) -> str:
        day = d.until.date().isoformat()
        if d.quiet:
            return f"Digest for {day}: a quiet day. No incidents were opened, fixed or left open."
        lines = [f"Digest for {day}."]

        def section(title: str, items: list[Incident]) -> None:
            if not items:
                return
            lines.append("")
            lines.append(f"{title} ({len(items)}):")
            for inc in items:
                lines.append(f"- {inc.id}: {_headline(inc)}")

        section("Fixed by agents, no action needed", d.healed)
        section("Open, low priority, held for this digest", d.open_quiet)
        section("Open, already sent to you", d.open_told)
        section("Resolved", d.resolved)
        return "\n".join(lines)


# ── construction ────────────────────────────────────────────────────────


def _new_incident(
    cause: Cause, summary: str, priority: str, actor: str, now: datetime, hold_until: datetime
) -> Incident:
    return Incident(
        id=f"inc-{uuid.uuid4().hex[:12]}",
        fingerprint=cause.fingerprint,
        area=cause.area,
        kind=cause.kind,
        action=cause.action,
        error_class=cause.error_class,
        priority=priority,  # type: ignore[arg-type]
        owner_agent=actor,
        summary=summary,
        unmet=tuple(cause.unmet),
        subjects=[cause.subject] if cause.subject else [],
        first_seen=now,
        last_seen=now,
        occurrences=1,
        remedy=cause.remedy,
        state="holding",
        hold_until=hold_until,
        evidence_refs=[cause.evidence_ref] if cause.evidence_ref else [],
    )


# ── rendering ───────────────────────────────────────────────────────────

_RAW = re.compile(r"Traceback|File \"|\bline \d+\b|(?:/[\w.-]+){2,}")


def _plain(text: str, fallback: str) -> str:
    """The first line of ``text`` when it reads as a sentence, else ``fallback``.

    A summary is meant to be one plain sentence; this is the guard for when a
    sender passed exception text instead."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    if not first or _RAW.search(first):
        return fallback
    return first


def _kind_words(inc: Incident) -> str:
    return inc.kind.replace("_", " ").replace(".", " ")


def _headline(inc: Incident) -> str:
    n = len(inc.subjects) or 1
    return f"{n} {_kind_words(inc)} failing: {inc.action}"


def _fallback_render(incident: Incident) -> Rendered:
    """The deterministic shape, used when no richer renderer is installed."""
    subject = _headline(incident)
    if len(subject) > 90:
        subject = subject[:89] + "…"
    what = _plain(incident.summary, f"The {incident.action} job failed.")
    affects = ", ".join(incident.subjects) if incident.subjects else "(no named subject)"
    if incident.attempts:
        tried = "; ".join(a.outcome for a in incident.attempts)
    else:
        tried = "Nothing was tried automatically: no remedy is declared for this kind yet."
    fixes = [u.fix or u.why for u in incident.unmet if (u.fix or u.why)]
    lines = [what, "", f"Affects: {affects}.", "", f"Tried: {tried}"]
    if fixes:
        lines += ["", "What you can do:"] + [f"- {f}" for f in fixes]
    lines += ["", f"Incident {incident.id}, seen {incident.occurrences} time(s)."]
    text = "\n".join(lines)
    body_html = "".join(
        f"<p>{html.escape(line)}</p>" if not line.startswith("- ") else f"<li>{html.escape(line[2:])}</li>"
        for line in lines
        if line
    )
    return Rendered(
        subject=subject,
        body_text=text,
        body_html=body_html,
        incident_url="",
        written_by="template",
    )


def render(incident: Incident) -> Rendered:
    """The installed renderer's output, else the deterministic fallback."""
    try:
        from .render import render_incident  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - no renderer installed: the template stands
        return _fallback_render(incident)
    try:
        return render_incident(incident)
    except Exception as exc:  # noqa: BLE001 - a renderer failure never loses the notification
        _log.warning("render_incident failed for %s: %s", incident.id, type(exc).__name__)
        return _fallback_render(incident)


# ── delivery ────────────────────────────────────────────────────────────


def _accepts(fn: Callable, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def send_incident(incident: Incident, rendered: Rendered, delivery: dict[str, Any] | None = None) -> object:
    """Deliver an incident through ``send``, with no cause, once per opening.

    ``dedup_key`` is the incident id, with the reopen count when it has
    recurred, so the send's own window cannot swallow a reopened incident."""
    from dataclasses import fields as _fields

    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        send,
    )
    from axiom.extensions.builtins.notifications.skills.send import _ctx
    from axiom.governance import Classification

    delivery = delivery or {}
    payload_kw: dict[str, object] = {"summary": rendered.subject, "body": rendered.body_text}
    send_kw: dict[str, object] = {}
    if rendered.body_html:
        if _accepts(send, "body_html"):
            send_kw["body_html"] = rendered.body_html
        elif "body_html" in {f.name for f in _fields(NotificationPayload)}:
            payload_kw["body_html"] = rendered.body_html
    dedup = incident.id if not incident.reopened else f"{incident.id}#r{incident.reopened}"
    return send(
        _ctx(),
        actor=incident.owner_agent or "@herald",
        recipient=delivery.get("recipient") or "@operator",
        payload=NotificationPayload(**payload_kw),  # type: ignore[arg-type]
        classification=Classification.from_str(delivery.get("classification") or "internal"),
        priority=Priority(incident.priority),
        intent=delivery.get("intent") or f"incident.{incident.area}.{incident.kind}",
        dedup_key=dedup,
        **send_kw,
    )


__all__ = [
    "Digest",
    "IncidentStage",
    "OPEN_STATES",
    "render",
    "send_incident",
]
