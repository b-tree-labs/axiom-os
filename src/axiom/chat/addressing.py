# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""@agent mention parsing and resolution.

Mentions are tokens starting with '@', not preceded by a word character
(so 'email@example.com' is not a mention). A mention may be qualified
with a context using a colon separator (Matrix-style):
'@name:context-id'. A single leading '@' is mandatory.

Resolution maps a list of mention strings to concrete MentionTarget
records via an AddressBook. Wildcards expand against the current period
roster so @all-curios is scoped to the meeting in session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_\-\.]+(?::[A-Za-z0-9_\-\.]+)?)")


def parse_mentions(text: str) -> list[str]:
    return ["@" + m for m in _MENTION_RE.findall(text)]


@dataclass
class MentionTarget:
    handle: str
    agent: str
    context: str | None = None


class AddressBook:
    """Map mention handles to (agent, context) targets."""

    def __init__(self) -> None:
        self._entries: dict[str, MentionTarget] = {}

    def register(self, handle: str, *, agent: str, context: str | None = None) -> None:
        self._entries[handle] = MentionTarget(handle=handle, agent=agent, context=context)

    def lookup(self, handle: str) -> MentionTarget | None:
        return self._entries.get(handle)


_WILDCARDS = {"@all-curios"}


def resolve(
    mentions: list[str],
    *,
    book: AddressBook,
    period_roster: list[str],
) -> list[MentionTarget]:
    """Resolve a list of mention handles to targets. Unknown handles are dropped."""
    out: list[MentionTarget] = []
    seen: set[str] = set()

    for handle in mentions:
        if handle in _WILDCARDS:
            # Expand wildcard to every resolvable roster member.
            for roster_handle in period_roster:
                target = book.lookup(roster_handle)
                if target is not None and target.agent not in seen:
                    out.append(target)
                    seen.add(target.agent)
            continue

        target = book.lookup(handle)
        if target is not None and target.agent not in seen:
            out.append(target)
            seen.add(target.agent)

    return out
