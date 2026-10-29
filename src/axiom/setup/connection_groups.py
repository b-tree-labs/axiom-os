# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Connections offered as a few groups rather than as fourteen prompts.

The setup wizard walked every registered connection in turn and ran each one's
interactive flow. Fourteen of them, every one optional, with no way out of the
sequence. A colleague went through it during onboarding on 2026-10-01 and said
the connections were "either too early, irrelevant or he was unsure if he
needed it", and that the process was arduous and unfriendly. He was right on
all three counts, and they are three different problems:

- **Too early.** Nothing in the list is needed to use the platform. The phase
  opened with "You can skip any for now", which reads as *you will have to do
  this eventually* rather than *none of this is required*.
- **Irrelevant.** Fourteen connections presented with equal weight, when
  somebody who came to read data from a site node needs none of them.
- **Unsure if he needed it.** Each prompt named a product and never said what
  it was for or what skipping it costs.

So: one question per category, defaulting to none, with each category saying
what it is for. Four keystrokes to get through the ones you do not want,
instead of fourteen flows. The per-connection flows are unchanged — they are
fine, there were just too many of them in a row.

This module is the part worth testing: what to offer, in what order, and what
is already done. The wizard asks the questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ConnectionGroup", "group_purpose", "plan_groups"]


#: What a category is for, in the reader's terms rather than ours. Without
#: this, each prompt named a product and left "do I need this?" unanswerable.
_PURPOSE: dict[str, str] = {
    "llm": "which model answers your questions",
    "code": "reading and writing repositories",
    "data": "a database for the platform's own tables",
    "storage": "where documents and knowledge packs live",
    "tools": "local command-line helpers, for rendering and diagrams",
}

#: Order to offer them in: most likely to matter first. A newcomer who stops
#: reading after the first line should have seen the one that mattered.
_ORDER = ["llm", "code", "data", "storage", "tools"]


def group_purpose(category: str) -> str:
    """What this category is for. Falls back to the category's own name.

    A category added later is offered with a weaker description rather than
    being dropped from the wizard — the listing is built from the registry, so
    silence here must not hide a connection.
    """
    return _PURPOSE.get(category, f"{category} connections")


@dataclass
class ConnectionGroup:
    """One category's worth of connections, split by what is left to do."""

    category: str
    purpose: str
    pending: list = field(default_factory=list)
    """Connections not yet configured — what a "yes" would walk through."""
    done: list = field(default_factory=list)
    """Already configured or already installed. Reported, never re-asked."""

    @property
    def is_empty(self) -> bool:
        """Nothing to ask about. Such a group is not offered."""
        return not self.pending


def plan_groups(connections, *, is_done) -> list[ConnectionGroup]:
    """Split ``connections`` into the groups to offer, in order.

    ``is_done(conn) -> bool`` answers whether a connection is already set up —
    injected because answering it means reading a keychain, an environment and
    a ``PATH``, none of which belongs in the shape of a list.

    A category with nothing pending is returned with its ``done`` filled in and
    ``is_empty`` true, so a caller can report "already set up" without
    offering a question nobody needs to answer.
    """
    by_category: dict[str, ConnectionGroup] = {}
    for conn in connections:
        category = getattr(conn, "category", "") or "other"
        group = by_category.setdefault(
            category, ConnectionGroup(category=category, purpose=group_purpose(category))
        )
        try:
            settled = bool(is_done(conn))
        except Exception:  # noqa: BLE001 — an unanswerable question is not "done"
            settled = False
        (group.done if settled else group.pending).append(conn)

    def rank(category: str) -> tuple[int, str]:
        return (_ORDER.index(category) if category in _ORDER else len(_ORDER), category)

    return [by_category[c] for c in sorted(by_category, key=rank)]
