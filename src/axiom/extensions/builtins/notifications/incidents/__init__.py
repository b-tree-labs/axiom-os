# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Incidents: what a notification that names a cause becomes (ADR-249, ADR-252).

There is one notification path, ``notifications.send``. A sender that detected
a condition passes ``cause=Cause(...)``; ``send`` hands the notification to the
:class:`IncidentStage` here, which records it in the durable store (senders run
in other processes than the stage's tick), groups it with every other
notification about the same cause, and delivers it later through ``send``
with the cause stripped. A notification without a cause never comes here.
"""

from __future__ import annotations

from .model import Attempt, Cause, Incident, Rendered, Unmet
from .stage import IncidentStage
from .store import IncidentStore

_STORE: IncidentStore | None = None


def default_store() -> IncidentStore:
    """The process's incident store (``session_for("notifications")``)."""
    global _STORE
    if _STORE is None:
        _STORE = IncidentStore()
    return _STORE


def set_default_store(store: IncidentStore | None) -> None:
    """Point the process at another store (tests), or back to the default."""
    global _STORE
    _STORE = store


def default_stage() -> IncidentStage:
    return IncidentStage(default_store())


__all__ = [
    "Attempt",
    "Cause",
    "Incident",
    "IncidentStage",
    "IncidentStore",
    "Rendered",
    "Unmet",
    "default_stage",
    "default_store",
    "set_default_store",
]
