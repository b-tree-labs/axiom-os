# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The scheduler's dead-letter hook names its cause (ADR-249, ADR-252).

It used to send once per dead-lettered fire with the exception as the subject
line, so N connectors failing for one reason were N messages. Now each fire
sends one notification with a :class:`Cause`, and the incident stage folds
them into one incident carrying the failed result's structured ``unmet``.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.notifications import incidents
from axiom.extensions.builtins.notifications.channels.base import ChannelAdapterRegistry
from axiom.extensions.builtins.notifications.channels.inbox import InboxChannelAdapterProvider
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.incidents.stage import OPEN_STATES
from axiom.extensions.builtins.notifications.send import SendContext

T = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
UNMET = [{"id": "source-kind-refreshable", "why": "this source is pushed to, not pulled", "fix": "set refresh_enabled=false on it"}]


def _payload(connector: str) -> dict:
    return {
        "schedule_id": f"sched-{connector}",
        "action": "data.refresh",
        "intended_fire_at": T,
        "envelope": {"action": "data.refresh", "params": {"connector": connector}},
        "error": "SkillDispatchError('data.refresh failed: MissingConfiguration: ...')",
        "error_class": "MissingConfiguration",
        "unmet": UNMET,
    }


@pytest.fixture
def box(monkeypatch):
    inbox = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=inbox))
    skills_send = importlib.import_module("axiom.extensions.builtins.notifications.skills.send")
    monkeypatch.setattr(skills_send, "_CTX", SendContext(registry=registry, inbox_store=inbox))
    monkeypatch.setattr("axiom.extensions.builtins.notifications.preferences.resolve_role", lambda r: r)
    return inbox


@pytest.fixture
def svc(store, tmp_path, box):
    from axiom.extensions.builtins.data_platform.services.orchestrator import OrchestratorService
    from axiom.infra.skills import SkillRegistry

    incidents.set_default_store(store)
    yield OrchestratorService(registry=SkillRegistry(), state_dir=tmp_path, clock=lambda: T)
    incidents.set_default_store(None)


def test_three_connectors_failing_for_one_reason_are_one_incident(svc, store, box):
    for c in ("source-a", "source-b", "source-c"):
        svc._on_dead_letter(_payload(c))
    found = store.in_states(OPEN_STATES)
    assert len(found) == 1
    inc = found[0]
    assert inc.subjects == ["source-a", "source-b", "source-c"]
    assert inc.occurrences == 3
    assert inc.owner_agent == "@plinth"
    assert inc.priority == "high"
    assert [u.id for u in inc.unmet] == ["source-kind-refreshable"]
    assert inc.unmet[0].fix == "set refresh_enabled=false on it"
    assert "SkillDispatchError" not in inc.summary, "the exception string stays in the logs"
    assert box.all() == [], "nothing is delivered until the stage releases the incident"


def test_the_bus_event_the_hook_always_published_is_kept(svc, monkeypatch):
    published: list = []

    class _Bus:
        def publish(self, subject, payload, **kw):
            published.append(subject)

    monkeypatch.setattr("axiom.infra.bus.get_default_eventbus", lambda: _Bus())
    svc._on_dead_letter(_payload("source-a"))
    assert "data.dispatch.dead_letter" in published
    assert "incident.schedule.dispatch_dead_letter" in published


def test_a_different_error_is_a_different_incident(svc, store):
    svc._on_dead_letter(_payload("source-a"))
    other = _payload("source-b")
    other["error_class"] = "TimeoutError"
    other["unmet"] = []
    svc._on_dead_letter(other)
    assert len(store.in_states(OPEN_STATES)) == 2
