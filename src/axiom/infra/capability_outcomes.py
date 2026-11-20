# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The last capability call each caller made in this process, and how it ended (LOOP-B2, B3).

:func:`~axiom.infra.skill_dispatch.invoke_capability` notes every call it finishes here:
who called, which capability, on which surface, and whether it succeeded, failed or was
refused. Nothing here reports anything. A feedback report reads it so the reporter types
no context: a person's report names the capability they last used, and an agent that
flags a failed call gets the call's outcome and first error attached (``feedback.report``).
Only calls the agent flags become reports.

Bounded and in memory: at most :data:`LIMIT` callers, one entry each. A long-lived
server (MCP, the web node) keeps it across calls; a one-shot command line starts empty,
and a report from there simply carries no capability.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: The most callers remembered; the oldest is dropped first.
LIMIT = 512
#: The most capabilities remembered per caller.
PER_CALLER = 16

OK = "ok"
FAILED = "failed"
REFUSED = "refused"


@dataclass(frozen=True)
class Outcome:
    capability: str
    surface: str
    outcome: str
    error: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "surface": self.surface, "outcome": self.outcome,
                "error": self.error, "at": self.at.isoformat()}


_lock = threading.Lock()
_calls: OrderedDict[str, OrderedDict[str, Outcome]] = OrderedDict()


def _classify(result: Any) -> tuple[str, str]:
    if getattr(result, "ok", True):
        return OK, ""
    errors = [str(e) for e in (getattr(result, "errors", None) or [])]
    first = errors[0].splitlines()[0][:300] if errors else ""
    refused = first.startswith(("denied by hook", "approval required")) or "refus" in first.lower()
    return (REFUSED if refused else FAILED), first


def note(caller: str, capability: str, result: Any, *, surface: str) -> None:
    """Remember how ``caller``'s call to ``capability`` ended. Never raises."""
    if not caller or not capability:
        return
    try:
        outcome, error = _classify(result)
        with _lock:
            mine = _calls.pop(caller, None) or OrderedDict()
            mine.pop(capability, None)
            mine[capability] = Outcome(capability, surface, outcome, error)
            while len(mine) > PER_CALLER:
                mine.popitem(last=False)
            _calls[caller] = mine
            while len(_calls) > LIMIT:
                _calls.popitem(last=False)
    except Exception:  # noqa: BLE001 - remembering an outcome never fails the call
        return


def last(caller: str, capability: str | None = None, *, skip_prefix: str = "") -> Outcome | None:
    """The caller's last call (to ``capability`` when named), skipping capabilities under ``skip_prefix``."""
    with _lock:
        mine = _calls.get(caller)
        if not mine:
            return None
        if capability:
            return mine.get(capability)
        for name in reversed(mine):
            if skip_prefix and name.startswith(skip_prefix):
                continue
            return mine[name]
    return None


def reset() -> None:
    with _lock:
        _calls.clear()


__all__ = ["FAILED", "LIMIT", "OK", "Outcome", "REFUSED", "last", "note", "reset"]
