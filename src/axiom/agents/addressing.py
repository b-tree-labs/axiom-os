# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`@axi do X` — address this platform from inside any harness.

A person in Claude Code, Codex or OpenCode should be able to address Axiom the
way they address a colleague, without learning a tool name. The harness reads
one instruction at connect — if you see a leading `@handle`, call
`agents.address` with the whole message — and everything after the handle is
the request.

`agent_router.parse_addressee` already split a leading name off a message, but
it expected a chat adapter to have stripped the `@` upstream, and it knew
nothing about the brand handle. A consumer distribution ships its own CLI
name, and addressing that name reaches the same orchestrator `@axi` does —
nobody should have to know which name their install was built under.

What this deliberately does NOT do is answer unaddressed text. A router that
treated every message as possibly-for-it would hijack the conversation it was
embedded in, which is the one failure that would get the whole integration
switched off.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

#: A handle only counts at the START of the message, and the `@` must begin a
#: token — `ben@axi.example` is an email, not an address. Trailing punctuation
#: (`@axi:` / `@axi,`) belongs to the sentence rather than to the name.
#: `@name` or `@name:context` — the principal form this platform already uses
#: everywhere else (`@ben.booth:axiom`, `@senna:ut-austin`). The context is
#: captured rather than ignored: without this group, `@axi:andretti do X`
#: parsed as handle `axi` with request `"andretti do X"`, so the word naming
#: WHICH NODE was asked got swallowed into the question and the local agent
#: answered as though it had been addressed directly.
# The separator keeps `:` so a trailing colon stays punctuation: `@axi: do X`
# is someone addressing axi, not naming a context called "". The context group
# requires a letter after the colon, so the two cannot be confused.
_HANDLE_RE = re.compile(r"^@([A-Za-z][\w.-]*)(?::([A-Za-z][\w.-]*))?[:,\s]+(.*)$", re.S)
_BARE_HANDLE_RE = re.compile(r"^@([A-Za-z][\w.-]*)(?::([A-Za-z][\w.-]*))?[:,]?\s*$")


class NotAddressed(Exception):
    """This message is not addressed to an Axiom agent, or not to a known one."""

    def __init__(self, message: str, did_you_mean: list[str] | None = None):
        super().__init__(message)
        self.did_you_mean = did_you_mean or []


@dataclass(frozen=True)
class Addressed:
    """A message that named an agent, and what it asked for."""

    handle: str
    request: str
    #: The `:context` suffix, e.g. `andretti` in `@axi:andretti`. Empty means
    #: the local node. A context this node cannot route to is refused rather
    #: than answered locally — see `split_handle`.
    context: str = ""
    #: The handle as typed, before brand aliasing — so a caller can echo back
    #: what the person actually wrote rather than the name it resolved to.
    typed: str = ""
    did_you_mean: list[str] = field(default_factory=list)


def split_handle(
    text: str,
    *,
    known: set[str],
    brand_handle: str = "",
    default: str = "axi",
    local_contexts: set[str] | None = None,
) -> Addressed:
    """Split `@handle request` into its parts, or refuse.

    ``brand_handle`` is this install's own CLI name, whatever a distribution
    was built under. Addressing it reaches the orchestrator, because the brand
    IS the orchestrator from outside: someone typing the product's name is
    asking the platform, not a persona.
    """
    local_contexts = {c.lower() for c in (local_contexts or set())} | {"local"}
    stripped = (text or "").strip()
    if not stripped.startswith("@"):
        raise NotAddressed("not addressed to an agent")

    if _BARE_HANDLE_RE.match(stripped):
        raise NotAddressed("addressed an agent but asked it nothing")

    m = _HANDLE_RE.match(stripped)
    if not m:
        raise NotAddressed("not addressed to an agent")

    typed = m.group(1)
    context = (m.group(2) or "").strip()
    request = m.group(3).strip()
    if not request:
        raise NotAddressed("addressed an agent but asked it nothing")

    handle = typed.lower()
    if brand_handle and handle == brand_handle.lower():
        handle = default

    if context and context.lower() not in local_contexts:
        # Refusing beats answering. A remote context names another node, and
        # there is no agent-invocation transport across federation yet:
        # federation projects signed DATA, it does not carry a turn. Answering
        # locally would return this node's view under a name that asked for
        # another node's, which is the one answer that cannot be checked by
        # reading it.
        raise NotAddressed(
            f"'@{typed}:{context}' names another node; this node can only "
            f"answer for itself. Remote agent addressing is not wired yet."
        )

    if handle not in known:
        raise NotAddressed(
            f"unknown agent '@{typed}'",
            did_you_mean=difflib.get_close_matches(handle, sorted(known), n=3, cutoff=0.6),
        )
    return Addressed(handle=handle, request=request, typed=typed, context=context)


__all__ = ["Addressed", "NotAddressed", "split_handle"]
