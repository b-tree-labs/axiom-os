# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""When nobody answers an update that waits for approval, ask again, louder, then tell the operator.

A site's contact gets one message, then nothing happens: they were away, the
message was filtered, or they have left. A node then sits out of date for
months, which is the failure this exists to prevent. So an unanswered approval
climbs a ladder, by days since it opened:

========  ==============  =================================================
day 0     email           the site's contacts: what changed, and the link
day 7     email           the same contacts: a reminder
day 10    chat            the site's chat channel, if it has one
day 14    dashboard       the node's heartbeat carries a flag the operator sees
day 21    email           the platform operator: contact the site directly
========  ==============  =================================================

A contact whose address bounces permanently is marked departed and not tried
again. When every contact has departed, the operator is told at once rather
than at day 21, because waiting for a ladder nobody can hear helps no one.

Each rung runs once. State is one JSON file per pending approval, so the
ladder survives restarts and can be read by a person. The clock is passed in,
so a month is tested in milliseconds.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Rung:
    day: float
    channel: str  # email | chat | dashboard
    audience: str  # contacts | operator | dashboard
    kind: str  # notice | reminder | flag | contact_the_site


DEFAULT_LADDER: tuple[Rung, ...] = (
    Rung(0, "email", "contacts", "notice"),
    Rung(7, "email", "contacts", "reminder"),
    Rung(10, "chat", "contacts", "reminder"),
    Rung(14, "dashboard", "dashboard", "flag"),
    Rung(21, "email", "operator", "contact_the_site"),
)

#: ``deliver(channel, recipient, subject, body) -> (ok, error)``. ``recipient``
#: is an address for email and the site's chat channel for chat.
Deliver = Callable[[str, str, str, str], tuple[bool, str]]

#: ``links(recipient, now) -> (approve_url, not_now_url)``: this person's own
#: signed one-time links (``update.notice``). Without it the notice carries the
#: single ``link`` it was opened with.
Links = Callable[[str, datetime], tuple[str, str]]

#: Fragments of a permanent delivery failure: the address does not exist.
_PERMANENT = ("550", "5.1.1", "5.1.10", "user unknown", "does not exist", "no such user", "recipient address rejected")


def _permanent(error: str) -> bool:
    low = (error or "").lower()
    return any(p in low for p in _PERMANENT)


class Escalation:
    """One pending approval and how far up the ladder it has gone."""

    def __init__(self, path: Path, state: dict, ladder: tuple[Rung, ...] = DEFAULT_LADDER):
        self.path = Path(path)
        self.state = state
        self.ladder = ladder

    # -- lifecycle ---------------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        site: str,
        node: str,
        version: str,
        contacts: list[str],
        operator: str,
        notes: str,
        link: str,
        chat: str | None = None,
        now: datetime | None = None,
        ladder: tuple[Rung, ...] = DEFAULT_LADDER,
    ) -> Escalation:
        state = {
            "site": site, "node": node, "version": version, "notes": notes, "link": link,
            "contacts": list(contacts), "chat": chat, "operator": operator,
            "opened_at": (now or datetime.now(UTC)).isoformat(),
            "done": [], "departed": [], "answer": None, "flag": False, "log": [],
        }
        e = cls(path, state, ladder)
        e._save()
        return e

    @classmethod
    def load(cls, path: Path, ladder: tuple[Rung, ...] = DEFAULT_LADDER) -> Escalation:
        return cls(path, json.loads(Path(path).read_text(encoding="utf-8")), ladder)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- the ladder --------------------------------------------------------------

    def _days(self, now: datetime) -> float:
        opened = datetime.fromisoformat(self.state["opened_at"])
        return (now - opened).total_seconds() / 86400.0

    def live_contacts(self) -> list[str]:
        return [c for c in self.state["contacts"] if c not in self.state["departed"]]

    def _message(self, rung: Rung, now: datetime, recipient: str = "",
                 links: Links | None = None) -> tuple[str, str]:
        s = self.state
        if rung.kind == "contact_the_site":
            return (
                f"No answer from {s['site']} about update {s['version']} after {int(self._days(now))} days",
                f"Node {s['node']} at {s['site']} has an update waiting since {s['opened_at'][:10]}. "
                f"Nobody has answered. Contacts tried: {', '.join(s['contacts']) or 'none'}; "
                f"departed: {', '.join(s['departed']) or 'none'}. Please contact the site directly.\n\n"
                f"What changed:\n{s['notes']}",
            )
        lead = "Reminder: an" if rung.kind == "reminder" else "An"
        return (
            f"{lead} update to {s['version']} is waiting for your approval ({s['node']})",
            f"{lead} update for {s['node']} is ready: version {s['version']}.\n\n"
            f"What changed:\n{s['notes']}\n\n"
            + self._link_lines(recipient, now, links) +
            "Nothing changes on your machine until someone approves. Your data keeps flowing.",
        )

    def _link_lines(self, recipient: str, now: datetime, links: Links | None) -> str:
        if links is None or not recipient:
            return f"Approve or postpone: {self.state['link']}\n\n"
        approve, not_now = links(recipient, now)
        return (f"Approve: {approve}\n"
                f"Not now: {not_now}\n"
                "These links are yours alone, work once, and expire within a day; "
                "a later reminder carries fresh ones.\n\n")

    def _log(self, now: datetime, event: str, **kw) -> dict:
        entry = {"at": now.isoformat(), "event": event, **kw}
        self.state["log"].append(entry)
        return entry

    def _run(self, i: int, rung: Rung, now: datetime, deliver: Deliver, links: Links | None = None) -> list[dict]:
        events: list[dict] = []
        subject, body = self._message(rung, now)
        if rung.audience == "dashboard":
            self.state["flag"] = True
            events.append(self._log(now, "dashboard_flag", day=rung.day))
        elif rung.audience == "operator":
            ok, err = deliver(rung.channel, self.state["operator"], subject, body)
            events.append(self._log(now, "operator_told", ok=ok, error=err))
        elif rung.channel == "chat":
            if self.state.get("chat"):
                if links is not None:
                    # A chat channel is shared: personal links never go there.
                    body = body.split("Approve:")[0] + (
                        "Each contact has their own approval link by email.\n\n"
                        "Nothing changes on your machine until someone approves. Your data keeps flowing.")
                ok, err = deliver("chat", self.state["chat"], subject, body)
                events.append(self._log(now, "chat", ok=ok, error=err))
            else:
                events.append(self._log(now, "skipped", rung="chat", reason="no chat channel for this site"))
        else:
            for contact in self.live_contacts():
                if links is not None:
                    subject, body = self._message(rung, now, contact, links)
                ok, err = deliver(rung.channel, contact, subject, body)
                events.append(self._log(now, rung.kind, to=contact, ok=ok, error=err))
                if not ok and _permanent(err):
                    self.state["departed"].append(contact)
                    events.append(self._log(now, "departed", contact=contact))
        self.state["done"].append(i)
        return events

    def step(self, now: datetime, deliver: Deliver, links: Links | None = None) -> list[dict]:
        """Run every rung that is due and not yet run. Returns what happened.

        ``links`` gives each contact their own signed one-time links; see ``update.notice``.
        """
        if self.state["answer"] is not None:
            return []
        events: list[dict] = []
        days = self._days(now)
        for i, rung in enumerate(self.ladder):
            if i in self.state["done"] or rung.day > days:
                continue
            events += self._run(i, rung, now, deliver, links)
        # Nobody left to ask: tell the operator now instead of at the last rung.
        if not self.live_contacts():
            for i, rung in enumerate(self.ladder):
                if rung.audience == "operator" and i not in self.state["done"]:
                    events += self._run(i, rung, now, deliver, links)
                    self.state["flag"] = True
        self._save()
        return events

    def answer(self, decision: str, *, by: str, now: datetime | None = None) -> None:
        """Record the answer that came back through the signed link. Stops the ladder."""
        when = now or datetime.now(UTC)
        self.state["answer"] = {"decision": decision, "by": by, "at": when.isoformat()}
        self.state["flag"] = False
        self._log(when, "answered", decision=decision, by=by)
        self._save()

    def heartbeat_fields(self, now: datetime | None = None) -> dict:
        """What the node's heartbeat reports, so the operator's dashboard sees a long wait."""
        if self.state["answer"] is not None:
            return {"update_waiting": None}
        return {
            "update_waiting": {
                "version": self.state["version"],
                "days": round(self._days(now or datetime.now(UTC)), 1),
                "flag": bool(self.state["flag"]),
                "contacts_reachable": len(self.live_contacts()),
            }
        }


def herald_deliverer(*, email_config: dict, chat_config: dict | None = None) -> Deliver:
    """A :data:`Deliver` over HERALD's email and chat channel adapters.

    Called adapter by adapter rather than through ``send``'s fallback, because
    the ladder must see a bounce: ``send`` falls back to the inbox and would
    report a departed contact as delivered.
    """
    from axiom.extensions.builtins.notifications.channels.email.channel import (
        EmailChannelAdapterProvider,
    )
    from axiom.governance import Classification

    email = EmailChannelAdapterProvider().build(email_config)
    chat = None
    if chat_config:
        from axiom.extensions.builtins.notifications.channels.slack import (
            SlackChannelAdapterProvider,
        )

        chat = SlackChannelAdapterProvider().build(chat_config)

    def deliver(channel: str, recipient: str, subject: str, body: str) -> tuple[bool, str]:
        adapter = email if channel == "email" else chat
        if adapter is None:
            return False, f"no {channel} channel configured"
        kwargs = dict(recipient=recipient, receipt_id=f"update-{subject[:24]}",
                      classification=Classification.INTERNAL, priority="normal", summary=subject)
        if channel == "email":
            kwargs["body_text"] = body
        else:
            kwargs["summary"] = f"{subject}\n{body}"
        try:
            result = adapter.deliver_sync(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a channel failure is a result, not a crash
            return False, f"{type(exc).__name__}: {exc}"
        return bool(getattr(result, "ok", False)), str(getattr(result, "error", "") or "")

    return deliver


__all__ = ["DEFAULT_LADDER", "Deliver", "Escalation", "Links", "Rung", "herald_deliverer"]
