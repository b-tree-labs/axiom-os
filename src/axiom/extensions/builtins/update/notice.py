# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The update notice a site's contacts receive, with their own approval links (ADR-179 §4, §6).

When a node reports a release waiting for approval, the platform operator
opens a notice. Each time the ladder (``update.escalation``) contacts someone,
this:

1. makes sure a signed ``apply_update`` request for that version is waiting
   at the site's relay. A relay request is valid for at most a day and the
   ladder runs for weeks, so a rung after the last request expired signs a
   fresh one; the node drops the old one when it expires;
2. signs that person's own **Approve** and **Not now** links, bound to that
   request, that person, and an expiry no later than the request's;
3. lets HERALD render them into the message.

Pressing a link files it at the edge; the node verifies it and approves or
declines through the same path as its command line, then reports the result.
:meth:`UpdateNotice.observe` reads those results and stops the ladder.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.infra import maintenance as m
from axiom.vega.identity.keypair import Keypair

from .escalation import Deliver, Escalation

#: How long each signed request (and so each round of links) stays valid.
REQUEST_TTL = timedelta(hours=23)

#: ``post(envelope)``: hand a signed request to the site's relay.
Post = Callable[[dict[str, Any]], Any]


class UpdateNotice:
    def __init__(
        self,
        escalation: Escalation,
        *,
        keypair: Keypair,
        key_id: str,
        operator_principal: str,
        edge_url: str,
        post: Post,
        ttl: timedelta = REQUEST_TTL,
    ) -> None:
        self.escalation = escalation
        self.keypair = keypair
        self.key_id = key_id
        self.operator = operator_principal
        self.edge_url = edge_url
        self.post = post
        self.ttl = ttl

    @property
    def _state(self) -> dict[str, Any]:
        return self.escalation.state.setdefault("notice", {"requests": []})

    def _current_request(self, now: datetime) -> dict[str, Any]:
        """The signed request this round of links is bound to; a fresh one once the last expires."""
        reqs = self._state["requests"]
        if reqs:
            env = reqs[-1]
            expires = datetime.fromisoformat(env["request"]["expires_at"].replace("Z", "+00:00"))
            if now < expires - timedelta(minutes=10):
                return env
        s = self.escalation.state
        env = m.sign_request(self.keypair, key_id=self.key_id, operator_principal=self.operator, site=s["site"],
                             action="apply_update", params={"version": s["version"]},
                             issued_at=now, expires_at=now + self.ttl)
        self.post(env)
        reqs.append(env)
        return env

    def links(self, recipient: str, now: datetime) -> tuple[str, str]:
        env = self._current_request(now)
        approve = m.sign_approval(self.keypair, key_id=self.key_id, operator_principal=self.operator,
                                  request_envelope=env, recipient=recipient, decision="approve", issued_at=now)
        deny = m.sign_approval(self.keypair, key_id=self.key_id, operator_principal=self.operator,
                               request_envelope=env, recipient=recipient, decision="deny", issued_at=now)
        return m.approval_link(self.edge_url, approve), m.approval_link(self.edge_url, deny)

    def step(self, now: datetime, deliver: Deliver) -> list[dict]:
        events = self.escalation.step(now, deliver, links=self.links)
        self.escalation._save()  # the requests it signed are part of the notice's state
        return events

    def observe(self, results: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any] | None:
        """Stop the ladder once the node reports a decision on one of this notice's requests."""
        ours = {env["request"]["id"] for env in self._state["requests"]}
        for r in results:
            if r.get("id") not in ours:
                continue
            status = r.get("status")
            if status in ("done", "failed", "denied"):
                decision = "deny" if status == "denied" else "approve"
                self.escalation.answer(decision, by=str(r.get("by") or "an approval link"),
                                       now=now or datetime.now(UTC))
                return {"decision": decision, "result": r}
        return None


def relay_poster(relay_url: str, token: str) -> Post:
    """A :data:`Post` to the relay's operator endpoint."""
    import json
    import urllib.request

    def post(envelope: dict[str, Any]) -> Any:
        req = urllib.request.Request(f"{relay_url.rstrip('/')}/maintenance/requests", method="POST",
                                     data=json.dumps(envelope).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - configured relay
            return json.loads(r.read() or b"{}")

    return post


__all__ = ["REQUEST_TTL", "UpdateNotice", "relay_poster"]
