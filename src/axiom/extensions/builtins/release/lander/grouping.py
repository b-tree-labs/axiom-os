# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Group open pull requests.

Signals, strongest first in the explanation: an explicit ``batch:<id>``
label (ignores the window), an agent session, a ticket id, a branch
prefix, and an identical file set. The window (default 6h) limits every
signal except the explicit label. ``batch-exempt`` opts a pull request
out of automatic signals. Exact file equality is the file signal;
a proper subset is reported by the caller, not unioned here.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta

from axiom.extensions.builtins.release.lander.model import OpenPull

_BATCH = re.compile(r"(?i)^batch:(.+)$")
_SESSION_LABEL = re.compile(r"(?i)^(?:agent-)?session:(.+)$")
_SESSION_BODY = re.compile(r"(?im)^Agent-Session:\s*(\S+)\s*$")
_DEPENDENCY = re.compile(r"^(dependabot/[^/]+/.+)-(\d+(?:\.\d+)*)$")


def batch_labels(pull: OpenPull) -> tuple[str, ...]:
    found = []
    for label in pull.labels:
        match = _BATCH.match(label.strip())
        if match:
            found.append(match.group(1))
    return tuple(dict.fromkeys(found))


def is_batch_exempt(pull: OpenPull) -> bool:
    return any(label.strip().lower() == "batch-exempt" for label in pull.labels)


def is_dependabot(pull: OpenPull) -> bool:
    """True when Dependabot opened the branch.

    A grouped update is already one pull request. Automatic batching
    and file-overlap would treat every ``dependabot/`` branch as the
    same family, because they share that prefix and often the same
    manifest. An explicit ``batch:`` label still applies.
    """
    return pull.head_ref.startswith("dependabot/")


def session_id(pull: OpenPull) -> str | None:
    for label in pull.labels:
        match = _SESSION_LABEL.match(label.strip())
        if match:
            return match.group(1)
    body = _SESSION_BODY.search(pull.body or "")
    if body:
        return body.group(1)
    return None


def branch_prefix(ref: str) -> str:
    """First path segment, or the token before the first hyphen."""
    if "/" in ref:
        return ref.split("/", 1)[0]
    if "-" in ref:
        return ref.split("-", 1)[0]
    return ref


def dependency_key(ref: str) -> str | None:
    """``dependabot/<eco>/<name>`` with the trailing version removed."""
    match = _DEPENDENCY.match(ref)
    if not match:
        return None
    return match.group(1)


def dependency_version(ref: str) -> tuple[int, ...]:
    match = _DEPENDENCY.match(ref)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(2).split("."))


class _Union:
    def __init__(self, numbers: list[int]) -> None:
        self.parent = {number: number for number in numbers}

    def find(self, number: int) -> int:
        while self.parent[number] != number:
            self.parent[number] = self.parent[self.parent[number]]
            number = self.parent[number]
        return number

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left


def _link(pulls: list[OpenPull], union: _Union, *, ignore_window: bool, window: timedelta) -> None:
    ordered = sorted(pulls, key=lambda pull: pull.updated_at)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            gap = right.updated_at - left.updated_at
            if ignore_window or gap <= window:
                union.union(left.number, right.number)
            else:
                break


DEFAULT_SIGNALS = frozenset(
    {"batch_label", "session", "ticket", "branch_prefix", "exact_files", "affected_scope"}
)


def group_pulls(
    pulls: list[OpenPull],
    *,
    window: timedelta,
    ignore_window: bool = False,
    signals: frozenset[str] | set[str] | None = None,
) -> list[list[OpenPull]]:
    """Connected components. Every pull is in exactly one component."""
    if not pulls:
        return []
    union = _Union([pull.number for pull in pulls])
    by_label: dict[str, list[OpenPull]] = defaultdict(list)
    by_session: dict[str, list[OpenPull]] = defaultdict(list)
    by_ticket: dict[str, list[OpenPull]] = defaultdict(list)
    by_prefix: dict[str, list[OpenPull]] = defaultdict(list)
    by_files: dict[tuple[str, ...], list[OpenPull]] = defaultdict(list)
    by_scope: dict[str, list[OpenPull]] = defaultdict(list)
    enabled = set(DEFAULT_SIGNALS if signals is None else signals)
    for pull in pulls:
        if "batch_label" in enabled:
            for label in batch_labels(pull):
                by_label[label].append(pull)
        if is_batch_exempt(pull) or is_dependabot(pull):
            continue
        if "session" in enabled:
            session = session_id(pull)
            if session:
                by_session[session].append(pull)
        if "ticket" in enabled:
            for ticket in pull.ticket_ids:
                by_ticket[ticket].append(pull)
        if "branch_prefix" in enabled:
            by_prefix[branch_prefix(pull.head_ref)].append(pull)
        if "exact_files" in enabled:
            files = pull.sorted_files()
            if files:
                by_files[files].append(pull)
        if "affected_scope" in enabled and pull.scope and pull.scope != "all":
            by_scope[pull.scope].append(pull)
    for group in by_label.values():
        _link(group, union, ignore_window=True, window=window)
    for group in by_session.values():
        _link(group, union, ignore_window=ignore_window, window=window)
    for group in by_ticket.values():
        _link(group, union, ignore_window=ignore_window, window=window)
    for group in by_prefix.values():
        _link(group, union, ignore_window=ignore_window, window=window)
    for group in by_files.values():
        _link(group, union, ignore_window=ignore_window, window=window)
    for group in by_scope.values():
        _link(group, union, ignore_window=ignore_window, window=window)
    buckets: dict[int, list[OpenPull]] = defaultdict(list)
    for pull in pulls:
        buckets[union.find(pull.number)].append(pull)
    components = [sorted(group, key=lambda pull: pull.number) for group in buckets.values()]
    components.sort(key=lambda group: group[0].number)
    return components


def shared_reasons(members: list[OpenPull]) -> tuple[str, ...]:
    """Signals that at least two members share."""
    reasons: list[str] = []

    def shared(values: list[tuple[str, ...]]) -> list[str]:
        counts: dict[str, int] = {}
        for group in values:
            for value in dict.fromkeys(group):
                counts[value] = counts.get(value, 0) + 1
        return sorted(key for key, count in counts.items() if count >= 2)

    for label in shared([batch_labels(pull) for pull in members]):
        reasons.append(f"batch:{label}")
    sessions = shared([(session_id(pull) or "",) for pull in members])
    for session in sessions:
        if session:
            reasons.append(f"session:{session}")
    tickets = shared([pull.ticket_ids for pull in members])
    for ticket in tickets:
        reasons.append(f"ticket:{ticket}")
    prefixes = shared([(branch_prefix(pull.head_ref),) for pull in members])
    for prefix in prefixes:
        reasons.append(f"prefix:{prefix}")
    file_keys = shared(
        [(" ".join(pull.sorted_files()),) if pull.sorted_files() else () for pull in members]
    )
    if any(key for key in file_keys):
        reasons.append("exact-files")
    scopes = shared([(pull.scope,) if pull.scope and pull.scope != "all" else () for pull in members])
    for scope in scopes:
        reasons.append(f"affected-scope:{scope}")
    return tuple(reasons)
