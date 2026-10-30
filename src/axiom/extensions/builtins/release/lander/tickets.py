# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ticket identifiers on a pull request.

``TicketIndex`` is the adapter. GitHub Issues and Linear ship first.
Another tracker implements ``ids_for`` and is passed to ``TicketRouter``.
"""

from __future__ import annotations

import re
from typing import Protocol

from axiom.extensions.builtins.release.lander.model import OpenPull

_GH_CLOSE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)\b"
)
_ISSUE_LABEL = re.compile(r"(?i)^issue:(\d+)$")
_LINEAR_URL = re.compile(r"linear\.app/\S*?/issue/([A-Z][A-Z0-9]+-\d+)", re.IGNORECASE)
_LINEAR_WORD = re.compile(r"(?i)\blinear[:\s]+([A-Z][A-Z0-9]+-\d+)\b")
_LINEAR_LABEL = re.compile(r"(?i)^(?:linear|ticket):([A-Za-z][A-Za-z0-9]+-\d+)$")


class TicketIndex(Protocol):
    """One tracker. Returns stable ids, or nothing."""

    name: str

    def ids_for(self, pull: OpenPull) -> tuple[str, ...]:
        """Identifiers this pull names. Empty when it names none."""


class GitHubIssueTickets:
    """Closing keywords, ``owner/name#n``, and ``issue:n`` labels."""

    name = "github_issues"

    def ids_for(self, pull: OpenPull) -> tuple[str, ...]:
        text = f"{pull.title}\n{pull.body}"
        found: list[str] = []
        for match in _GH_CLOSE.finditer(text):
            repo = match.group("repo") or ""
            number = match.group("num")
            found.append(f"{repo}#{number}" if repo else f"#{number}")
        for label in pull.labels:
            labeled = _ISSUE_LABEL.match(label)
            if labeled:
                found.append(f"#{labeled.group(1)}")
        return tuple(dict.fromkeys(found))


class LinearTickets:
    """Linear issue keys from a URL, a ``Linear:`` line, or a label."""

    name = "linear"

    def ids_for(self, pull: OpenPull) -> tuple[str, ...]:
        text = f"{pull.title}\n{pull.body}"
        found: list[str] = []
        for match in _LINEAR_URL.finditer(text):
            found.append(match.group(1).upper())
        for match in _LINEAR_WORD.finditer(text):
            found.append(match.group(1).upper())
        for label in pull.labels:
            labeled = _LINEAR_LABEL.match(label)
            if labeled:
                found.append(labeled.group(1).upper())
        return tuple(dict.fromkeys(found))


class TicketRouter:
    """Run every configured index and keep the first occurrence of each id."""

    def __init__(self, indexes: tuple[TicketIndex, ...] | None = None) -> None:
        self.indexes: tuple[TicketIndex, ...] = indexes or (
            GitHubIssueTickets(),
            LinearTickets(),
        )

    def ids_for(self, pull: OpenPull) -> tuple[str, ...]:
        found: list[str] = list(pull.ticket_ids)
        for index in self.indexes:
            found.extend(index.ids_for(pull))
        return tuple(dict.fromkeys(found))
