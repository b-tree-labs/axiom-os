# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who signs an archival act (ADR-290 D8).

Sealing, archiving and tombstoning a period are acts on a log. They are signed by the accountable
human where one can be traced to the actor, and by the acting identity where none can:

- a human acting signs for themselves;
- an agent is signed for by the human :func:`axiom.agents.activation.accountable_for` names for it
  (the site's configuration, the activation record, the agent's declaration, the site default);
- a machine (the scheduler, a node) is signed for by the human the site names for it (the site
  default, ``*``);
- with no human traced, the agent or machine signs as itself.

Declared accountability is the authority: systems rarely declare authority apart from it, so a human
name tied to the actor is what is asked for and is enough. More of the chain (who the human answers
to) is recorded when known and never required. The record says who signed, who performed the act and
where the trace came from, so an audit can tell a person's own act from one made for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

#: The actor kinds a caller names: it knows whether a person, an agent or a machine is acting.
KINDS = ("human", "agent", "machine")


@dataclass(frozen=True)
class AccountableSigner:
    signer: str
    performed_by: str
    #: ``self`` (a human, for themselves), ``accountable`` (an agent or machine, traced to a human),
    #: ``actor`` (no human traced: the agent or machine itself).
    kind: str
    #: Where the trace came from: ``self``, ``site``, ``record``, ``declaration``, ``site-default``,
    #: or empty when there is none.
    source: str
    #: Who the accountable human answers to, when known; never required.
    answers_to: str = ""

    @property
    def traced(self) -> bool:
        return self.kind in ("self", "accountable")

    def as_record(self) -> dict[str, Any]:
        """The ``signer`` block an archival act's record carries."""
        out: dict[str, Any] = {"principal": self.signer, "kind": self.kind}
        if self.kind != "self":
            out["performed_by"] = self.performed_by
        if self.kind == "accountable":
            out["accountable_source"] = self.source
            if self.answers_to:
                out["answers_to"] = self.answers_to
        return out


def _name(handle: str) -> str:
    """``@keel:site-a`` as the agent maps spell it: ``keel``."""
    return handle.lstrip("@").partition(":")[0]


def resolve(
    actor: str,
    kind: str,
    *,
    record: dict | None = None,
    named: dict[str, str] | None = None,
    declared: dict[str, str] | None = None,
    answers_to: str = "",
) -> AccountableSigner:
    """The signer of an act ``actor`` (of ``kind``: human, agent or machine) performs.

    ``record`` is the site's activation record, ``named`` the site's configured map and ``declared``
    what an agent's own declaration names; each is read from the platform when left out."""
    if kind not in KINDS:
        raise ValueError(f"an actor is one of {KINDS}, not {kind!r}")
    if kind == "human":
        return AccountableSigner(actor, actor, "self", "self")

    from axiom.agents import activation

    if record is None:
        try:
            record = activation.load()
        except Exception:  # noqa: BLE001 - no record is a site that has none yet
            record = None
    name = _name(actor)
    who = SimpleNamespace(handle=name, accountable=(declared or {}).get(name, ""))
    human, source, _entry = activation.accountable_for(who, record, named)
    if human:
        return AccountableSigner(human, actor, "accountable", source, answers_to)
    return AccountableSigner(actor, actor, "actor", "")
