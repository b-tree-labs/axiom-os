# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S4 Sessions and obligations (#1818): numbered sessions from the site's seed, one open at a
time, and a check cadence that warns, misses, is met, and tells each person once."""

from __future__ import annotations

from datetime import timedelta

import pytest

from .conftest import ANN, BO, LOG, SEED, SITE, check, open_session, sign_cli


@pytest.mark.acceptance("LB-E2E-A8")
def test_lb_e2e_a8_sessions_number_from_the_seed_one_open_at_a_time(world):
    from axiom.extensions.builtins.attest import service

    with pytest.raises(service.AttestRefused):
        check(world)  # no session open
    opened = open_session(world)
    assert opened["interval"] == {"kind": "session", "number": SEED, "event": "opened"}
    with pytest.raises(service.AttestRefused):
        open_session(world)  # one at a time
    assert check(world)["interval"]["number"] == SEED
    closed = sign_cli(ANN, "CLOSE", {"text": "done"})
    assert closed["interval"]["number"] == SEED
    assert open_session(world)["interval"]["number"] == SEED + 1


@pytest.mark.acceptance("LB-E2E-A8")
def test_lb_e2e_a8_the_number_to_come_is_readable_before_the_opener(world):
    # #1808: the number the next opener will get, readable before it is signed; none while open.
    from axiom.extensions.builtins.attest import intervals

    assert intervals.next_number(SITE, LOG, "session") == SEED
    assert intervals.listing(SITE, LOG)["next_numbers"] == {"session": SEED}
    open_session(world)
    assert intervals.next_number(SITE, LOG, "session") is None
    assert intervals.listing(SITE, LOG)["next_numbers"] == {"session": None}
    sign_cli(ANN, "CLOSE", {"text": "done"})
    assert intervals.next_number(SITE, LOG, "session") == SEED + 1


@pytest.mark.acceptance("LB-E2E-A9")
def test_lb_e2e_a9_one_warning_one_miss_each_told_once_then_met(world):
    from axiom.extensions.builtins.attest import obligations
    from axiom.infra.bus import get_default_eventbus

    t0 = world.clock["t"]
    open_session(world)
    published: list[str] = []
    sub = get_default_eventbus().subscribe(
        f"attest.{LOG}.*", lambda subj, p: published.append(subj)
    )
    try:
        for minutes in (26, 27, 31, 32, 40):
            obligations.tick(
                SITE, LOG, now=t0 + timedelta(minutes=minutes), state_dir=world.state_dir
            )
        world.clock["t"] = t0 + timedelta(minutes=41)
        check(world)
        obligations.tick(SITE, LOG, now=t0 + timedelta(minutes=42), state_dir=world.state_dir)
    finally:
        get_default_eventbus().unsubscribe(sub)
    assert [e["state"] for e in obligations.events(SITE, LOG)] == ["warn", "missed", "met"]
    assert published.count(f"attest.{LOG}.obligation_missed") == 1
    missed_to = sorted(r for r, st in world.sent if st == "missed")
    assert missed_to == sorted([ANN.handle, BO.handle])
    (now_state,) = obligations.evaluate(SITE, LOG, now=t0 + timedelta(minutes=42))
    assert now_state["state"] == "ok"


@pytest.mark.acceptance("LB-E2E-A9")
def test_lb_e2e_a9_an_explanation_marks_the_miss_explained(world):
    # #1810: an ``obligation_ref`` field names the miss (a system fact); the signed entry marks
    # it explained, and only a miss at this site and log can be named.
    from axiom.extensions.builtins.attest import obligations, service

    t0 = world.clock["t"]
    open_session(world)
    obligations.tick(SITE, LOG, now=t0 + timedelta(minutes=31), state_dir=world.state_dir)
    (miss,) = [e for e in obligations.events(SITE, LOG) if e["state"] == "missed"]
    assert miss["explained_by"] is None
    with pytest.raises(service.AttestRefused, match="miss"):
        sign_cli(ANN, "EXPLAIN", {"text": "no such miss", "miss": "not-an-event"})
    ex = sign_cli(ANN, "EXPLAIN", {"text": "the desk was unattended", "miss": miss["event_id"]})
    (after,) = [e for e in obligations.events(SITE, LOG) if e["state"] == "missed"]
    assert after["explained_by"] == ex["attestation_id"]
    assert [(m["event_id"], m["explained_by"]) for m in obligations.misses(SITE, LOG)] == [
        (miss["event_id"], ex["attestation_id"])
    ]
    items = service.seal_preview(SITE, LOG, boundary="session")["open_items"]
    (item,) = [i for i in items if i["kind"] == "missed_obligation"]
    assert item["explained_by"] == ex["attestation_id"] and "explained" in item["summary"]
