# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-252: one notification path. ``send`` is the only front door.

A notification that names a cause is held as an incident and goes out later,
once, through the same ``send`` with the cause stripped. One without a cause
is delivered as written, exactly as before. The daily digest is one message,
quiet or not.
"""

from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.notifications import incidents
from axiom.extensions.builtins.notifications.channels.base import ChannelAdapterRegistry
from axiom.extensions.builtins.notifications.channels.inbox import InboxChannelAdapterProvider
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.incidents.model import Cause
from axiom.extensions.builtins.notifications.incidents.stage import IncidentStage, render, send_incident
from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.governance import Classification
from axiom.infra.skills import SkillContext, SkillRegistry

# The package re-exports a ``send`` function that shadows the module.
send_mod = importlib.import_module("axiom.extensions.builtins.notifications.send")
skills_send = importlib.import_module("axiom.extensions.builtins.notifications.skills.send")


def _cause(subject="source-1", **kw):
    base = dict(
        area="schedule",
        kind="dispatch_dead_letter",
        action="data.refresh",
        subject=subject,
        error_class="SkillFailed",
    )
    base.update(kw)
    return Cause(**base)


@pytest.fixture
def default_store(store):
    incidents.set_default_store(store)
    yield store
    incidents.set_default_store(None)


@pytest.fixture
def inbox(monkeypatch):
    """A real send context with only the inbox, made the process default so
    the stage's later delivery lands where the test can see it."""
    box = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=box))
    ctx = SendContext(registry=registry, inbox_store=box)
    monkeypatch.setattr(skills_send, "_CTX", ctx)
    monkeypatch.setattr(
        "axiom.extensions.builtins.notifications.preferences.resolve_role", lambda r: r
    )
    # Role routing is not under test here (test_an_alert_no_person_can_read_is_undelivered
    # covers an unheld role): the recipient is taken as a reachable address.
    monkeypatch.setattr(
        "axiom.extensions.builtins.notifications.preferences.unheld_role", lambda r: None
    )
    return ctx, box


@pytest.fixture
def sends(monkeypatch):
    """Every call that reaches send(), replaced by a recorder."""
    captured: list[dict] = []

    class _Receipt:
        id = "rcpt-test"
        outcome = "succeeded"

    def fake(ctx, **kw):
        captured.append(kw)
        return _Receipt()

    monkeypatch.setattr(send_mod, "send", fake)
    return captured


def _send(ctx, cause=None, summary="The scheduled refresh failed.", priority=Priority.HIGH):
    return send(
        ctx,
        actor="@plinth",
        recipient="@operator",
        payload=NotificationPayload(summary=summary),
        classification=Classification.INTERNAL,
        priority=priority,
        intent="data.dispatch.dead_letter",
        cause=cause,
    )


def test_with_a_cause_send_holds_and_names_the_incident(default_store, inbox):
    ctx, box = inbox
    receipts = [_send(ctx, _cause(s)) for s in ("source-a", "source-b", "source-c")]
    assert {r.outcome for r in receipts} == {"held"}
    ids = {r.incident_id for r in receipts}
    assert len(ids) == 1
    (inc_id,) = ids
    assert receipts[0].id == f"held-{inc_id}"
    assert box.all() == [], "nothing is delivered while the incident is held"
    assert default_store.get(inc_id).subjects == ["source-a", "source-b", "source-c"]


def test_without_a_cause_send_is_unchanged(default_store, inbox):
    ctx, box = inbox
    receipt = _send(ctx)
    assert receipt.outcome == "succeeded"
    assert receipt.incident_id is None
    assert len(box.all()) == 1
    assert default_store.in_states(("holding",)) == []


def test_the_held_incident_goes_out_once_through_send_with_no_cause(default_store, inbox):
    ctx, box = inbox
    for s in ("source-a", "source-b", "source-c"):
        _send(ctx, _cause(s))
    stage = IncidentStage(default_store, clock=lambda: datetime.now(UTC) + timedelta(minutes=11))
    report = stage.tick()
    assert report["notified"] == 1
    rows = box.all()
    assert len(rows) == 1, "one message for the cause, and it was not held a second time"
    assert "data.refresh" in rows[0].summary
    assert stage.tick()["notified"] == 0


def test_a_store_that_is_down_falls_back_to_one_direct_send_per_cause(inbox):
    ctx, box = inbox

    class _Broken:
        def by_fingerprint(self, fp):
            raise RuntimeError("database unavailable")

    incidents.set_default_store(_Broken())  # type: ignore[arg-type]
    try:
        first = _send(ctx, _cause("source-a"))
        second = _send(ctx, _cause("source-b"))
    finally:
        incidents.set_default_store(None)
    assert first.outcome == "succeeded"
    assert second.id == first.id, "keyed on the fingerprint: the second is the first send's receipt"
    assert len(box.all()) == 1, "one message per cause"


def test_the_stage_delivery_is_keyed_on_the_incident(store, clock, sends):
    stage = IncidentStage(store, clock=clock)  # default deliver goes through send
    inc_id = stage.record(_cause(), summary="The refresh failed.", priority="high", actor="@plinth",
                          recipient="@owner", classification="regulated", intent="data.dispatch.dead_letter")
    clock.advance(minutes=11)
    stage.tick()
    assert len(sends) == 1
    kw = sends[0]
    assert kw["dedup_key"] == inc_id
    assert kw["actor"] == "@plinth"
    assert kw["recipient"] == "@owner", "delivered to whom the sender named"
    assert kw["classification"] is Classification.REGULATED, "under the label it was sent with"
    assert kw["intent"] == "data.dispatch.dead_letter"
    assert kw["priority"].value == "high"
    assert "cause" not in kw, "the stage's delivery never names a cause again"
    assert "source-1" in kw["payload"].body


def test_a_reopened_incident_is_not_swallowed_by_send_dedup(store, clock, sends):
    stage = IncidentStage(store, clock=clock)
    inc_id = stage.record(_cause(), summary="x")
    clock.advance(minutes=11)
    stage.tick()
    stage.resolve(inc_id, by="@ben.booth")
    stage.record(_cause(), summary="x")
    clock.advance(minutes=11)
    stage.tick()
    assert [s["dedup_key"] for s in sends] == [inc_id, f"{inc_id}#r1"]


def test_send_incident_passes_html_only_where_send_takes_it(store, clock, sends):
    stage = IncidentStage(store, clock=clock, deliver=lambda i, r: None)
    inc = store.get(stage.record(_cause(), summary="x"))
    send_incident(inc, render(inc), store.delivery(inc.id))
    # The recorder takes **kw, so its signature names no body_html: the stage
    # must not invent a keyword the real send does not declare.
    assert "body_html" not in sends[0]


def _skill_ctx(tmp_path):
    from axiom.extensions.builtins.notifications import skills

    reg = SkillRegistry()
    skills.bind(reg)
    return reg, SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


def test_the_tick_skill_releases_held_incidents(default_store, sends, tmp_path):
    stage = IncidentStage(default_store, clock=lambda: datetime.now(UTC) - timedelta(minutes=30))
    stage.record(_cause(), summary="x")
    reg, ctx = _skill_ctx(tmp_path)
    result = reg.invoke("notifications.incident_tick", {}, ctx)
    assert result.ok, result.errors
    assert result.value["notified"] == 1
    assert len(sends) == 1


def test_the_digest_skill_sends_one_message_even_when_quiet(default_store, sends, tmp_path):
    reg, ctx = _skill_ctx(tmp_path)
    result = reg.invoke("notifications.digest", {}, ctx)
    assert result.ok, result.errors
    assert result.value["quiet"] is True
    assert len(sends) == 1
    assert "quiet" in sends[0]["payload"].summary
    assert sends[0]["dedup_key"].startswith("notifications.digest:")


def test_the_digest_lists_incidents_held_for_it(default_store, sends, tmp_path):
    stage = IncidentStage(default_store, clock=lambda: datetime.now(UTC) - timedelta(minutes=30))
    inc_id = stage.record(_cause(), summary="x", priority="normal")
    stage.tick(datetime.now(UTC))
    assert sends == []
    reg, ctx = _skill_ctx(tmp_path)
    result = reg.invoke("notifications.digest", {}, ctx)
    assert result.value["open_quiet"] == 1
    assert inc_id in sends[0]["payload"].body
