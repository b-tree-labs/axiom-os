# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-249 D2, D6 and acceptance A1, on the one path of ADR-252.

The case that prompted it: eight subjects failed for one reason within a
minute, and the owner received eight messages, each a raw exception. The
incident stage groups notifications by cause, holds before sending anything,
sends once, stays quiet on repeats, reopens on recurrence, and keeps normal
and low priority for the daily digest.
"""

from __future__ import annotations

from datetime import timedelta

from axiom.extensions.builtins.notifications.incidents.model import Attempt, Cause, Unmet
from axiom.extensions.builtins.notifications.incidents.stage import IncidentStage

UNMET = (Unmet(id="source-kind-refreshable", why="this source is pushed to, not pulled", fix="axi data source-set <name> refresh_enabled=false"),)
SUMMARY = "The scheduled refresh could not run for this source."


def _cause(subject: str, **kw) -> Cause:
    base = dict(
        area="schedule",
        kind="dispatch_dead_letter",
        action="data.refresh",
        subject=subject,
        error_class="MissingConfiguration",
        unmet=UNMET,
        evidence_ref=f"fire:{subject}",
    )
    base.update(kw)
    return Cause(**base)


def _stage(store, clock, sent, **kw) -> IncidentStage:
    return IncidentStage(store, clock=clock, deliver=lambda inc, r: sent.append((inc, r)), **kw)


def _record(stage, subject, priority="high", summary=SUMMARY, **kw):
    return stage.record(_cause(subject, **kw), summary=summary, priority=priority, actor="@plinth")


def test_eight_subjects_one_cause_is_one_message_naming_all_eight(store, clock, sent):
    stage = _stage(store, clock, sent)
    subjects = [f"source-{i}" for i in range(1, 9)]
    ids = set()
    for s in subjects:
        ids.add(_record(stage, s))
        clock.advance(seconds=5)
    assert len(ids) == 1

    stage.tick()
    assert sent == [], "nothing goes out while the hold window is open"

    clock.advance(minutes=10)
    stage.tick()
    assert len(sent) == 1
    incident, rendered = sent[0]
    assert incident.subjects == subjects
    assert incident.occurrences == 8
    assert incident.owner_agent == "@plinth"
    for s in subjects:
        assert s in rendered.body_text
    assert "Traceback" not in rendered.body_text
    assert "axi data source-set" in rendered.body_text, "the unmet fix reaches the reader"

    stored = store.get(incident.id)
    assert stored.state == "notified"
    assert stored.notified_at is not None


def test_a_repeat_while_notified_sends_nothing(store, clock, sent):
    stage = _stage(store, clock, sent)
    inc_id = _record(stage, "source-1")
    clock.advance(minutes=11)
    stage.tick()
    assert len(sent) == 1

    clock.advance(hours=1)
    assert _record(stage, "source-1") == inc_id
    assert _record(stage, "source-9") == inc_id
    clock.advance(minutes=30)
    stage.tick()
    assert len(sent) == 1
    stored = store.get(inc_id)
    assert stored.occurrences == 3
    assert stored.subjects == ["source-1", "source-9"]
    assert stored.state == "notified"


def test_a_recurrence_after_resolve_reopens_the_same_incident(store, clock, sent):
    stage = _stage(store, clock, sent)
    inc_id = _record(stage, "source-1")
    clock.advance(minutes=11)
    stage.tick()
    stage.resolve(inc_id, by="@ben.booth", note="disabled the refresh")
    assert store.get(inc_id).state == "resolved"

    clock.advance(days=1)
    assert _record(stage, "source-1") == inc_id
    reopened = store.get(inc_id)
    assert reopened.reopened == 1
    assert reopened.state == "holding"
    assert reopened.hold_until == clock() + timedelta(minutes=10)
    assert any("disabled the refresh" in n for n in reopened.notes)

    clock.advance(minutes=10)
    stage.tick()
    assert len(sent) == 2, "a reopened incident is sent once more, after one hold window"


def test_urgent_goes_after_the_one_minute_hold(store, clock, sent):
    stage = _stage(store, clock, sent)
    _record(stage, "source-1", priority="urgent")
    clock.advance(seconds=30)
    stage.tick()
    assert sent == []
    clock.advance(seconds=31)
    stage.tick()
    assert len(sent) == 1


def test_priority_only_rises(store, clock, sent):
    stage = _stage(store, clock, sent)
    inc_id = _record(stage, "source-1", priority="normal")
    _record(stage, "source-2", priority="urgent")
    _record(stage, "source-3", priority="low")
    assert store.get(inc_id).priority == "urgent"
    clock.advance(seconds=61)
    stage.tick()
    assert len(sent) == 1, "becoming urgent shortened the hold to one minute"


def test_normal_is_kept_for_the_digest_and_not_sent(store, clock, sent):
    stage = _stage(store, clock, sent)
    inc_id = _record(stage, "source-1", priority="normal")
    clock.advance(minutes=11)
    stage.tick()
    stage.tick()
    assert sent == []

    digest = stage.digest()
    assert [i.id for i in digest.open_quiet] == [inc_id]
    assert inc_id in stage.digest_text(digest)


def test_a_successful_heal_sends_nothing_and_records_healed(store, clock, sent):
    def heal(incident):
        assert incident.remedy == "data.refresh_check"
        return [Attempt(remedy=incident.remedy, at=clock(), ok=True, outcome="The environment check passed and the job ran.")]

    stage = _stage(store, clock, sent, heal=heal)
    inc_id = _record(stage, "source-1", remedy="data.refresh_check")
    clock.advance(minutes=11)
    stage.tick()
    assert sent == []
    stored = store.get(inc_id)
    assert stored.state == "healed"
    assert stored.attempts and stored.attempts[0].ok

    digest = stage.digest()
    assert [i.id for i in digest.healed] == [inc_id]


def test_a_failed_heal_still_reaches_a_person(store, clock, sent):
    def heal(incident):
        return [Attempt(remedy="data.refresh_check", at=clock(), ok=False, outcome="The check failed again.")]

    stage = _stage(store, clock, sent, heal=heal)
    _record(stage, "source-1")
    clock.advance(minutes=11)
    stage.tick()
    assert len(sent) == 1
    assert sent[0][0].attempts[0].ok is False


def test_a_restart_keeps_the_holding_state(store, clock, sent):
    first = _stage(store, clock, sent)
    inc_id = _record(first, "source-1")
    clock.advance(minutes=5)
    _record(first, "source-2")

    second = _stage(store, clock, sent)  # a new process over the same store
    assert _record(second, "source-3") == inc_id
    clock.advance(minutes=4)
    second.tick()
    assert sent == [], "the hold window survives the restart"
    clock.advance(minutes=2)
    second.tick()
    assert len(sent) == 1
    assert sent[0][0].subjects == ["source-1", "source-2", "source-3"]


def test_an_incident_quiet_for_two_days_resolves_itself(store, clock, sent):
    stage = _stage(store, clock, sent)
    inc_id = _record(stage, "source-1")
    clock.advance(minutes=11)
    stage.tick()
    clock.advance(hours=47)
    stage.tick()
    assert store.get(inc_id).state == "notified"
    clock.advance(hours=2)
    stage.tick()
    assert store.get(inc_id).state == "resolved"
    assert [i.id for i in stage.digest().resolved] == [inc_id]


def test_a_different_cause_is_a_different_incident(store, clock, sent):
    stage = _stage(store, clock, sent)
    a = _record(stage, "source-1")
    b = _record(stage, "source-1", error_class="TimeoutError", unmet=())
    assert a != b


def test_the_classification_only_tightens(store, clock, sent):
    stage = _stage(store, clock, sent)
    inc_id = stage.record(_cause("source-1"), summary=SUMMARY, classification="internal")
    stage.record(_cause("source-2"), summary=SUMMARY, classification="regulated")
    stage.record(_cause("source-3"), summary=SUMMARY, classification="public")
    assert store.delivery(inc_id)["classification"] == "regulated"


def test_the_digest_says_quiet_when_there_is_nothing(store, clock, sent):
    stage = _stage(store, clock, sent)
    text = stage.digest_text(stage.digest())
    assert "quiet" in text.lower()


def test_the_fallback_render_has_no_stack_trace_and_names_the_action(store, clock):
    from axiom.extensions.builtins.notifications.incidents.stage import _fallback_render

    stage = IncidentStage(store, clock=clock, deliver=lambda i, r: None)
    inc_id = _record(stage, "source-1", summary="Traceback (most recent call last): boom")
    r = _fallback_render(store.get(inc_id))
    assert "data.refresh" in r.subject
    assert len(r.subject) <= 90
    assert "Traceback" not in r.body_text
    assert r.written_by == "template"
