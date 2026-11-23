# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Obligations: a check that must be signed every N minutes while a run is
open, and the alarm when it is not (U6b, spec Obligations).

All on an injected clock. A miss is a recorded fact, raised once, sent to the
people who hold the logbook's notify roles, and cleared by signing the check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.attest import obligations, registry, roles, service, site_settings
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook
from axiom.infra.bus import get_default_eventbus

from .test_signing import SITE, person

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

LOGBOOK = {
    "logbook": {"id": "watch_log", "version": "1"},
    "interval": {"run": {"opens": ["START"], "closes": ["STOP"]}},
    "type": [
        {"id": "START", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "none_open"},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "STOP", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "run"},
    ],
    "obligation": [
        {
            "id": "check_cadence",
            "type": "CHECK",
            "every": {"site_key": "watch.checks.interval_minutes", "default": 30},
            "warn_before": {"site_key": "watch.checks.warn_lead_minutes", "default": 5},
            "while": "interval.run.open",
            "notify": ["operator", "supervisor"],
            "required": True,
        }
    ],
}  # fmt: skip


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


@pytest.fixture
def watch(attest_db, clock, tmp_path):
    registry.register(parse_logbook(LOGBOOK, source="test:watch"))
    roles.grant("@op1:site-a", SITE, "operator", by="@admin:site-a", state_dir=tmp_path)
    roles.grant("@sup:site-a", SITE, "supervisor", by="@admin:site-a", state_dir=tmp_path)
    sent: list[tuple[str, str]] = []
    obligations.set_sender(lambda recipient, state: sent.append((recipient, state["state"])))
    yield {"state_dir": tmp_path, "sent": sent, "clock": clock}
    obligations.reset_sender()
    site_settings.unregister()


def sign(entry_type, signer):
    m = {"START": "authored", "STOP": "authored"}.get(entry_type, "performed")
    d = service.create_draft(
        site_id=SITE, logbook="watch_log", entry_type=entry_type, meaning=m,
        content={"title": entry_type.lower(), "fields": {}}, origin="human", for_principal="@op1:site-a",
    )  # fmt: skip
    p = service.present(d)
    service.respond(p.presentation_id, principal=person().principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=person(), signer=signer).record


def state(now, w):
    (s,) = obligations.evaluate(SITE, "watch_log", now=now)
    return s


# -- declaration -------------------------------------------------------------------------


def test_an_obligation_is_enforced_now_not_deferred():
    b = parse_logbook(LOGBOOK)
    ob = b.obligations[0]
    assert (ob.id, ob.type, ob.every_default, ob.warn_default, ob.while_interval) == (
        "check_cadence", "CHECK", 30, 5, "run",
    )  # fmt: skip
    assert ob.required and ob.notify == ("operator", "supervisor")
    assert "obligation" not in b.not_yet_enforced


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"type": "NOPE"}, "NOPE"),
        ({"while": "interval.shift.open"}, "shift"),
        ({"every": {"default": 0}}, "every"),
    ],
)
def test_a_bad_obligation_is_refused(change, message):
    bad = {**LOGBOOK, "obligation": [{**LOGBOOK["obligation"][0], **change}]}
    with pytest.raises(LogbookError, match=message):
        parse_logbook(bad)


# -- evaluation ----------------------------------------------------------------------------


def test_nothing_is_owed_while_no_run_is_open(watch):
    assert obligations.evaluate(SITE, "watch_log", now=T0) == []


def test_the_first_check_is_due_one_interval_after_the_run_opens(watch, node_signer):
    sign("START", node_signer)
    s = state(T0 + timedelta(minutes=10), watch)
    assert s["state"] == "ok" and s["due_at"] == T0 + timedelta(minutes=30)
    assert state(T0 + timedelta(minutes=26), watch)["state"] == "warn"
    m = state(T0 + timedelta(minutes=31), watch)
    assert m["state"] == "missed" and m["severity"] == "alarm" and m["overdue_seconds"] == 60


def test_signing_the_check_moves_the_next_due_time(watch, node_signer):
    sign("START", node_signer)
    watch["clock"]["t"] = T0 + timedelta(minutes=20)
    sign("CHECK", node_signer)
    s = state(T0 + timedelta(minutes=31), watch)
    assert s["state"] == "ok" and s["due_at"] == T0 + timedelta(minutes=50)


def test_the_site_sets_the_interval(watch, node_signer):
    site_settings.register(lambda site: {"watch": {"checks": {"interval_minutes": 15}}})
    sign("START", node_signer)
    assert state(T0, watch)["due_at"] == T0 + timedelta(minutes=15)


def test_closing_the_run_ends_the_obligation(watch, node_signer):
    sign("START", node_signer)
    sign("STOP", node_signer)
    assert obligations.evaluate(SITE, "watch_log", now=T0 + timedelta(hours=2)) == []


# -- the alarm ----------------------------------------------------------------------------------


def test_a_miss_is_raised_once_to_everyone_holding_a_notify_role(watch, node_signer):
    sign("START", node_signer)
    got: list[str] = []
    sub = get_default_eventbus().subscribe("attest.watch_log.*", lambda subj, p: got.append(subj))
    try:
        for minutes in (31, 32, 40):
            obligations.tick(
                SITE, "watch_log", now=T0 + timedelta(minutes=minutes), state_dir=watch["state_dir"]
            )
    finally:
        get_default_eventbus().unsubscribe(sub)
    assert got.count("attest.watch_log.obligation_missed") == 1
    assert sorted(watch["sent"]) == [("@op1:site-a", "missed"), ("@sup:site-a", "missed")]
    events = obligations.events(SITE, "watch_log")
    assert [e["state"] for e in events] == ["missed"]


def test_a_warning_is_raised_before_the_miss(watch, node_signer):
    sign("START", node_signer)
    obligations.tick(
        SITE, "watch_log", now=T0 + timedelta(minutes=26), state_dir=watch["state_dir"]
    )
    obligations.tick(
        SITE, "watch_log", now=T0 + timedelta(minutes=31), state_dir=watch["state_dir"]
    )
    assert [e["state"] for e in obligations.events(SITE, "watch_log")] == ["warn", "missed"]


def test_signing_the_late_check_records_it_met_and_clears_the_alarm(watch, node_signer):
    sign("START", node_signer)
    obligations.tick(
        SITE, "watch_log", now=T0 + timedelta(minutes=35), state_dir=watch["state_dir"]
    )
    watch["clock"]["t"] = T0 + timedelta(minutes=36)
    sign("CHECK", node_signer)
    obligations.tick(
        SITE, "watch_log", now=T0 + timedelta(minutes=37), state_dir=watch["state_dir"]
    )
    assert [e["state"] for e in obligations.events(SITE, "watch_log")] == ["missed", "met"]
    assert state(T0 + timedelta(minutes=37), watch)["state"] == "ok"


def test_obligation_events_are_append_only(watch, node_signer):
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import store

    sign("START", node_signer)
    obligations.tick(
        SITE, "watch_log", now=T0 + timedelta(minutes=31), state_dir=watch["state_dir"]
    )
    with pytest.raises(Exception, match="append-only"):
        with store.session_scope() as s:
            s.execute(text("DELETE FROM attest_obligation_events"))
            s.commit()


def test_a_notice_obligation_is_not_an_alarm(watch, node_signer):
    registry.register(
        parse_logbook(
            {**LOGBOOK, "logbook": {"id": "watch_log", "version": "2"},
             "obligation": [{**LOGBOOK["obligation"][0], "required": False}]},
            source="test:watch",
        )
    )  # fmt: skip
    sign("START", node_signer)
    assert state(T0 + timedelta(minutes=31), watch)["severity"] == "notice"


def test_the_stream_carries_the_alarm(watch, node_signer):
    """The bus event a tick publishes is what the browser's stream forwards."""
    from axiom.extensions.builtins.attest import api

    sign("START", node_signer)
    got: list[tuple[str, dict]] = []
    sub = get_default_eventbus().subscribe("attest.watch_log.*", lambda s, p: got.append((s, p)))
    try:
        obligations.tick(
            SITE, "watch_log", now=T0 + timedelta(minutes=31), state_dir=watch["state_dir"]
        )
    finally:
        get_default_eventbus().unsubscribe(sub)
    subject, payload = got[-1]
    assert subject == "attest.watch_log.obligation_missed"
    assert payload["site_id"] == SITE and payload["severity"] == "alarm"
    assert payload["due_at"] == (T0 + timedelta(minutes=30)).isoformat()
    assert "obligation" in api._sse(None, "obligation", payload)
