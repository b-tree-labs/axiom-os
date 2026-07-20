# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PolicyEngine — broadcast, retrieve, revoke, expire scoped directives.

NL interpretation is injectable: tests pass a fake; production wires AXI.
The engine owns scope, membership, revocation; it does not own parsing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from axiom.chat import AddressBook
from axiom.policy.directive import Directive

NLInterpreter = Callable[..., dict[str, Any] | None]


def expand_targets(
    *,
    raw_mentions: list[str],
    book: AddressBook,
    period_roster: list[str],
) -> list[str]:
    """Resolve mentions (including @all-curios wildcard) to agent ids."""
    from axiom.chat import resolve

    resolved = resolve(raw_mentions, book=book, period_roster=period_roster)
    return [t.agent for t in resolved]


class PolicyEngine:
    """In-memory store + lifecycle for Directive records."""

    def __init__(self, *, nl_interpreter: NLInterpreter | None = None) -> None:
        self._directives: dict[str, Directive] = {}
        self._interpret = nl_interpreter

    # --- direct broadcast -------------------------------------------------

    def broadcast(
        self,
        *,
        issuer: str,
        targets: list[str],
        body: str,
        scope_kind: str,
        scope_id: str,
        now: float,
    ) -> str:
        d_id = uuid.uuid4().hex[:12]
        self._directives[d_id] = Directive(
            id=d_id,
            issuer=issuer,
            targets=tuple(targets),
            body=body,
            scope_kind=scope_kind,
            scope_id=scope_id,
            issued_at=now,
        )
        return d_id

    # --- NL broadcast (injectable interpreter) ----------------------------

    def broadcast_from_text(
        self,
        text: str,
        *,
        issuer: str,
        context: dict[str, Any],
        now: float,
    ) -> str | None:
        if self._interpret is None:
            raise RuntimeError(
                "no NL interpreter configured; pass nl_interpreter to PolicyEngine "
                "or call broadcast() directly"
            )
        parsed = self._interpret(text, issuer=issuer, context=context)
        if parsed is None:
            return None

        # Expand wildcard targets against the period roster.
        resolver = context.get("address_book_resolver")
        period_roster = context.get("period_roster", [])
        targets: list[str] = []
        for mention in parsed.get("targets", []):
            if mention == "@all-curios":
                for handle in period_roster:
                    agent = resolver(handle) if resolver else None
                    if agent and agent not in targets:
                        targets.append(agent)
            else:
                agent = resolver(mention) if resolver else mention
                if agent and agent not in targets:
                    targets.append(agent)

        scope_kind = parsed.get("scope_kind", "period")
        scope_id = (
            context.get(f"current_{scope_kind}_id")
            or parsed.get("scope_id")
            or "ambient"
        )

        return self.broadcast(
            issuer=issuer,
            targets=targets,
            body=parsed.get("body", text),
            scope_kind=scope_kind,
            scope_id=scope_id,
            now=now,
        )

    # --- retrieval --------------------------------------------------------

    def active_for(self, target: str, *, now: float) -> list[Directive]:
        return [
            d for d in self._directives.values()
            if d.active and target in d.targets
        ]

    # --- lifecycle --------------------------------------------------------

    def revoke(
        self,
        directive_id: str,
        *,
        now: float,
        reason: str | None = None,
        actor: str | None = None,
    ) -> None:
        d = self._directives[directive_id]
        if actor is not None and actor != d.issuer:
            raise PermissionError(
                f"only issuer may revoke; directive issued by {d.issuer}, actor is {actor}"
            )
        self._directives[directive_id] = d.revoke(now=now, reason=reason)

    def expire_scope(self, *, scope_kind: str, scope_id: str, now: float) -> int:
        n = 0
        for d_id, d in list(self._directives.items()):
            if d.active and d.scope_kind == scope_kind and d.scope_id == scope_id:
                self._directives[d_id] = d.expire(now=now)
                n += 1
        return n
