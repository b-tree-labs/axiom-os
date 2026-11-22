# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Arrival: what reaches somebody who is not looking at this.

The journey map scored arrival 9 and found nothing in it — every other
stage was gated on a stage that did not exist. These tests are about the
three rules that keep the fix from becoming the problem it was built
against.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.fleet import store as fleet_store
from axiom.extensions.builtins.fleet.db_models import Base as FleetBase
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.receipts.brief import compose_brief, fleet_source
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase
from axiom.extensions.builtins.receipts.digest import ALL_CLEAR, compose_digest

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CADENCES = {"heartbeat": 900, "service_health": 900}


@contextlib.contextmanager
def _fleet(*nodes_ago):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    FleetBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    fleet_store.set_provider(provider)
    with fleet_store.session_scope() as s:
        for node, ago in nodes_ago:
            ingest_reports(
                s,
                site="local",
                node_id=node,
                reporter_principal=f"@{node}:local",
                now=NOW - timedelta(seconds=ago),
                cadences=CADENCES,
                reports=[
                    {"kind": "heartbeat", "payload": {}},
                    {
                        "kind": "service_health",
                        "payload": {
                            "services": [{"name": "api", "status": "healthy", "latency_ms": 9}]
                        },
                    },
                ],
            )
        s.commit()
    try:
        yield
    finally:
        fleet_store.reset_provider()
        engine.dispose()


@pytest.fixture()
def receipts():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    RcptBase.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    with contextlib.closing(s):
        yield s
    engine.dispose()


def _digest(receipts, *nodes_ago, where=""):
    with _fleet(*nodes_ago):
        with fleet_store.session_scope() as fs:
            items = fleet_source(fs, now=NOW)
            # this_node mirrors the skill, which passes the node it runs
            # on; without it no handling is computed and the digest has
            # no "why" to carry.
            brief = compose_brief(
                receipts,
                items,
                site="local",
                snapshot=False,
                now=NOW,
                this_node="the-console",
                fleet_session=fs,
            )
    # now=NOW, not the wall clock. Without it the ages below are measured
    # against today's real date, which happens to pass on the day the
    # fixture was written and silently drifts every day after.
    return compose_digest(brief, where=where, now=NOW)


def test_one_message_for_the_day_not_one_per_case(receipts):
    """The construct is built against alert fatigue. One interruption
    describing three cases is the point; three interruptions is the
    problem it was supposed to solve."""
    d = _digest(receipts, ("node-a", 9000), ("node-b", 9000), ("node-c", 9000))
    assert d.waiting == 3
    assert d.subject == "3 cases waiting on you · local"
    assert d.body.count("stopped reporting") == 3


def test_a_quiet_day_still_arrives(receipts):
    """A digest that only arrives when something is wrong makes 'nothing
    is wrong' indistinguishable from 'the digest is broken'."""
    d = _digest(receipts, ("node-a", 0))
    assert d.waiting == 0
    assert d.needs_anyone is False
    assert ALL_CLEAR in d.body
    # ...and it still says what passed, so the all-clear has evidence.
    assert "check" in d.body


def test_it_says_WHY_a_person_is_needed_not_the_whole_case(receipts):
    """Evidence, reach and derivations live on the surface. Carrying them
    here makes a message nobody reads and a second place to drift."""
    d = _digest(receipts, ("node-a", 9000))
    assert "node-a stopped reporting" in d.body
    assert "Only node-a can put this right from its own side" in d.body
    # NOT the evidence, the reach, or the arithmetic.
    assert "seconds" not in d.body
    assert "affected" not in d.body


def test_the_cap_is_on_what_is_shown_never_on_what_is_counted(receipts):
    d = _digest(receipts, *[(f"node-{n}", 9000) for n in "abcde"])
    assert d.waiting == 5, "the count is the true count"
    assert d.body.count("stopped reporting") == 3, "the list is capped"
    assert "2 more not shown" in d.body


def test_a_link_is_omitted_rather_than_faked(receipts):
    """A digest pointing somewhere wrong is worse than one pointing
    nowhere."""
    without = _digest(receipts, ("node-a", 9000))
    assert "http" not in without.body
    with_link = _digest(receipts, ("node-a", 9000), where="https://node.example/receipts/")
    assert "https://node.example/receipts/" in with_link.body


def test_composing_the_arrival_message_does_not_consume_deltas(receipts):
    """Reading is not deciding. A digest that advanced the trust-delta
    baseline would make the next one silently different for having been
    sent."""
    from axiom.extensions.builtins.receipts.db_models import BriefSnapshot

    _digest(receipts, ("node-a", 9000))
    assert receipts.query(BriefSnapshot).count() == 0


def test_a_send_with_nobody_to_reach_is_refused(monkeypatch, tmp_path):
    """And the refusal names the file to write. "recipient is required"
    told somebody they were stuck; this tells them how to stop being.

    AXI_STATE_DIR is redirected because the skill now consults the DECLARED
    audience, and without the override this test reads the developer's real
    ~/.axi — so it would start passing or failing based on whether they
    happen to have declared one.
    """
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    from axiom.extensions.builtins.receipts.skills.digest import run

    result = run({"site": "local"})
    assert not result.ok
    assert "no audience declared" in result.errors[0]
    assert "digest_audience" in result.errors[0]


def test_a_declared_audience_is_who_a_scheduled_run_reaches(monkeypatch, tmp_path, receipts):
    """The whole point: a manifest-declared schedule carries no params, so
    without this a scheduled digest refuses on every single tick."""
    from axiom.extensions.builtins.receipts.audience import Audience, save_audience

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(
        Audience(recipients=("@robin", "@sam"), where="https://node.example/receipts/"),
        state_dir=tmp_path,
    )
    calls = _captured_send(monkeypatch)
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200), recipient="")
    assert {c["recipient"] for c in calls} == {"@robin", "@sam"}
    assert result.value["sent"] is True
    assert set(result.value["deliveries"]) == {"@robin", "@sam"}
    # ...and the declared link is used, since a scheduled run passes none.
    assert "https://node.example/receipts/" in result.value["digest"]["body"]


def test_one_bad_address_does_not_silence_everybody_else(monkeypatch, tmp_path, receipts):
    from axiom.extensions.builtins.receipts.audience import Audience, save_audience
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin", "@nowhere")), state_dir=tmp_path)

    class _Ok:
        outcome, channel, durable, error = "succeeded", "teams", True, None

    class _Bad:
        outcome, channel, durable, error = "denied", None, True, "unknown_recipient"

    monkeypatch.setattr(
        digest_skill,
        "_send",
        lambda d, *, recipient, actor, dedup_key: _Bad() if recipient == "@nowhere" else _Ok(),
    )
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200), recipient="")
    assert result.value["sent"] is True, "one bad address reported a total failure"
    assert result.value["deliveries"]["@robin"]["sent"] is True
    assert result.value["deliveries"]["@nowhere"]["sent"] is False


def test_a_disabled_audience_sends_nothing_and_says_so(monkeypatch, tmp_path, receipts):
    from axiom.extensions.builtins.receipts.audience import Audience, save_audience

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",), enabled=False), state_dir=tmp_path)
    calls = _captured_send(monkeypatch)
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200), recipient="")
    assert result.ok is True
    assert result.value["sent"] is False
    assert calls == []
    assert "disabled" in " ".join(result.actions_taken)


def test_a_malformed_declaration_is_not_read_as_nobody_declared(monkeypatch, tmp_path, receipts):
    """ "Nobody declared an audience" and "the declaration is wrong" want
    different sentences, and the second should list every problem at once."""
    from axiom.extensions.builtins.receipts.audience import audience_path

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    path = audience_path(state_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[digest_audience]\nrecipients = ["robin"]\nschedule = "every tuesday"\n',
        encoding="utf-8",
    )
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200), recipient="")
    assert not result.ok
    assert len(result.errors) == 2, result.errors
    assert not any("no audience declared" in e for e in result.errors)


def test_dry_run_shows_what_would_arrive_without_arriving(receipts):
    """How you look at the message before arming a schedule with it."""
    from axiom.extensions.builtins.receipts import store as rcpt_store
    from axiom.extensions.builtins.receipts.skills.digest import run

    @contextlib.contextmanager
    def provider():
        yield receipts

    rcpt_store.set_provider(provider)
    try:
        with _fleet(("node-a", 9000)):
            result = run({"site": "local", "dry_run": True})
    finally:
        rcpt_store.reset_provider()
    assert result.ok
    assert result.value["sent"] is False
    assert result.value["digest"]["waiting"] == 1


def test_a_case_waiting_on_a_click_does_not_read_like_one_waiting_on_judgement(receipts):
    """Found by looking at the real digest. A case nothing is stopping —
    a declared fix that applies here — carries no "why", because nobody's
    judgement is needed. Left bare it reads identically to a case that
    genuinely needs somebody, which is the distinction the whole handling
    model exists to draw."""
    from dataclasses import replace

    from axiom.extensions.builtins.receipts.brief import Brief
    from axiom.extensions.builtins.receipts.cases import Case
    from axiom.extensions.builtins.receipts.digest import compose_digest

    ready = Case(
        case_id="c-1",
        site="local",
        entity_kind="node",
        entity_id="this-node",
        title="this-node stopped reporting",
        severity="stale",
        handling={
            "can_run": True,
            "needs_person": False,
            "because": "",
            "fix_summary": "Make this node report now.",
            "runs": "fleet.report",
            "reason": "",
        },
    )
    brief = Brief(
        generated_at=NOW.isoformat(),
        site="local",
        focus=None,
        needs_you=[],
        trust_deltas=[],
        quiet_line="1 check passed quietly.",
        counts={"cases": 1, "green": 1},
        cases=[ready],
    )
    body = compose_digest(brief).body
    assert "A fix is ready: make this node report now." in body

    # And a case that DOES need judgement still says so, differently.
    needs = replace(
        ready,
        handling={
            **ready.handling,
            "can_run": False,
            "needs_person": True,
            "because": "The fix already ran and this is still happening.",
        },
    )
    other = compose_digest(replace(brief, cases=[needs])).body
    assert "The fix already ran" in other
    assert "A fix is ready" not in other


# --- Arrival must not report an arrival it did not achieve (2026-09-25) ---
#
# Every test above this line exercises COMPOSITION or dry_run. Not one
# exercised delivery, which is exactly how the following survived shipping:
#
#   >>> send(SendContext(), ..., classification=INTERNAL, ...)
#   outcome='denied'  error='no_channel_at_or_below_classification'
#
# `SendContext()` is the bare constructor. Only `SendContext.default()`
# registers the inbox adapter, so the digest's send was DENIED — nothing
# reached anybody — and `_deliver` returned True because `send` had not
# raised. The verb reported `sent: true` on every run.
#
# This is the `fleet.report`-returns-ok-on-an-unenrolled-node class, in the
# one path whose whole job is to reach a person. The notifications fabric
# was built to prevent it: a receipt carries `outcome` AND `durable`, the
# latter documented as "did this survive the process", precisely because
# "the default in-memory inbox accepts every alert and keeps none, so a
# monitor run from a laptop reports a delivery that never reached anyone."
# The digest threw both away.


def _run_digest(monkeypatch, receipts, *nodes_ago, recipient="@ben", **params):
    """Invoke the verb the way the scheduler would, against a real send."""
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    with _fleet(*nodes_ago):
        monkeypatch.setattr(digest_skill, "_this_node", lambda: "the-console")
        from axiom.extensions.builtins.receipts import store as rcpt_store

        @contextlib.contextmanager
        def provider():
            yield receipts

        rcpt_store.set_provider(provider)
        try:
            return digest_skill.run({"site": "local", "recipient": recipient, **params})
        finally:
            rcpt_store.reset_provider()


def test_a_denied_send_is_never_reported_as_sent(monkeypatch, receipts):
    """The receipt says denied. The result must say the same thing, and say
    why — "could not send" without a reason is the next hour of somebody's
    life."""
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    class _Receipt:
        outcome = "denied"
        channel = None
        durable = True  # the fabric leaves this True on the deny path
        error = "no_channel_at_or_below_classification"

    monkeypatch.setattr(digest_skill, "_send", lambda *a, **k: _Receipt())
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200))
    assert result.value["sent"] is False
    assert result.value["durable"] is False, (
        "a denied send delivered nothing; durable must not ride the fabric's "
        "default through to the caller"
    )
    said = " ".join(result.actions_taken) + " ".join(result.errors or [])
    assert "no_channel_at_or_below_classification" in said


def _pin_inbox_store(monkeypatch, store):
    """Pin the process-wide inbox store for one test.

    THE DEFECT THIS EXISTS TO FIX: this pair used to be one test that ran
    against whatever store the machine happened to resolve. `_resolve_inbox_store`
    returns Postgres when it is reachable and in-memory when it is not, so the
    assertion was about the environment rather than about the code — and when
    #1021 pointed the WRITER at the same resolver as the reader, the test
    turned red on every machine with a database and stayed green on every
    machine without one. Neither result told anybody anything about the digest.

    Pinning the store makes each direction a property of the argument.
    """
    # importlib, not `from ... import send`: the package exports a FUNCTION
    # named `send` that shadows the submodule of the same name, so the plain
    # import hands back a callable with no attribute to patch.
    import importlib

    send_mod = importlib.import_module("axiom.extensions.builtins.notifications.send")
    monkeypatch.setattr(send_mod, "_resolve_inbox_store", lambda: store)


def test_the_real_unconfigured_path_on_a_volatile_store_reports_accepted_but_lost(
    monkeypatch, receipts
):
    """No stub: this is what a machine with no channel and no database gets.
    The inbox baseline accepts it, and an in-memory store in a scheduled run —
    a fresh process every time — loses every digest. That is reported rather
    than rounded up to success."""
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore

    _pin_inbox_store(monkeypatch, InMemoryInboxStore())
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200), period="volatile-store-case")
    assert result.value["sent"] is False
    assert result.value["durable"] is False
    assert "survive" in " ".join(result.actions_taken).lower()


def test_the_real_unconfigured_path_on_a_durable_store_is_delivered(monkeypatch, receipts):
    """The other half, which #1021 made reachable: when the store the WRITER
    resolves is durable, the digest genuinely survives the process and saying
    so is correct. Asserting only the volatile case would let a regression that
    reported every durable send as lost pass unnoticed."""
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore

    class _DurableStore(InMemoryInboxStore):
        # A store declares its own durability; the digest reads it off the
        # receipt rather than guessing from the store's type.
        durable = True

    _pin_inbox_store(monkeypatch, _DurableStore())
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200), period="durable-store-case")
    assert result.value["durable"] is True
    assert result.value["sent"] is True


def test_delivery_that_will_not_survive_the_process_is_not_called_delivered(monkeypatch, receipts):
    """An in-memory inbox accepts and discards. A scheduled run is a fresh
    process every time, so `accepted` there means `lost` — and reporting it
    as sent is how a site believes it is being told things it is not."""
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    class _Receipt:
        outcome = "succeeded"
        channel = "inbox"
        durable = False
        error = None

    monkeypatch.setattr(digest_skill, "_send", lambda *a, **k: _Receipt())
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200))
    assert result.value["sent"] is False
    assert result.value["durable"] is False
    said = " ".join(result.actions_taken).lower()
    assert "survive" in said or "not durable" in said or "will not outlive" in said


def test_a_real_durable_delivery_does_report_sent(monkeypatch, receipts):
    """The guard must be capable of passing, or it is just a red light."""
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    class _Receipt:
        outcome = "succeeded"
        channel = "teams"
        durable = True
        error = None

    monkeypatch.setattr(digest_skill, "_send", lambda *a, **k: _Receipt())
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200))
    assert result.value["sent"] is True
    assert result.value["durable"] is True


# --- How long has this been true? (finding 3 of the stage-0 walk) ---


def test_the_digest_says_how_long_the_condition_has_held(receipts):
    """A case three minutes old and one sitting eleven hours read the same,
    which is the one fact that tells an operator whether they are late."""
    d = _digest(receipts, ("node-a", 7200))
    assert "2 hours" in d.body, d.body


def test_two_cases_of_different_ages_do_not_read_the_same(receipts):
    # 3000s, not 1800s: staleness fires at a multiple of the cadence, so a
    # node 30 minutes into a 900s cadence is still green and composes no
    # case at all. The age only has something to describe once the
    # evaluator has called it stale.
    young = _digest(receipts, ("node-a", 3000))
    old = _digest(receipts, ("node-b", 86400))
    assert young.body != old.body
    assert "50 minutes" in young.body, young.body
    assert "1 day" in old.body, old.body


def test_an_unrecorded_age_is_stated_not_blank(receipts):
    """Absence is written. A case whose claims carry no timestamp must say
    so rather than silently reading as fresh."""
    from axiom.extensions.builtins.receipts.brief import Brief, OversightItem
    from axiom.extensions.builtins.receipts.cases import compose_cases
    from axiom.extensions.builtins.receipts.digest import compose_digest

    item = OversightItem(
        entity_kind="node",
        entity_id="node-z",
        claim_kind="heartbeat",
        status="stale",
        evidence="nothing arrived",
        site="local",
        observed_at="",
    )
    cases = compose_cases([item])
    brief = Brief(
        generated_at=NOW.isoformat(),
        site="local",
        focus=None,
        needs_you=[item],
        trust_deltas=[],
        quiet_line="",
        counts={"cases": len(cases)},
        cases=cases,
    )
    body = compose_digest(brief).body
    assert "not recorded" in body, body


# --- The dedup key defeated the cadence (found while fixing the above) ---
#
# `dedup_key=f"receipts.digest:{digest.subject}"`, and the fabric keys
# suppression on `(actor, dedup_key)` where actor is `@receipts:local` for
# every digest. Two consequences, both silent:
#
#   1. The subject is stable whenever the case COUNT is stable, and
#      SendContext.default() uses a durable FileDedupLog. So the second
#      digest saying "2 cases waiting on you" is suppressed — for ever.
#      A site whose situation does not change gets exactly one message,
#      which is the precise failure mode "it sends on the cadence even
#      when quiet" exists to prevent.
#   2. The recipient is not in the key, so a digest to a second person is
#      suppressed as a duplicate of the first person's.


def _captured_send(monkeypatch):
    """Stand in for the fabric and record what it was asked to do."""
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    calls = []

    class _Receipt:
        outcome = "succeeded"
        channel = "teams"
        durable = True
        error = None

    def fake(digest, *, recipient, actor, dedup_key):
        calls.append({"recipient": recipient, "dedup_key": dedup_key})
        return _Receipt()

    monkeypatch.setattr(digest_skill, "_send", fake)
    return calls


def test_the_same_digest_in_a_later_period_is_not_a_duplicate(monkeypatch, receipts):
    calls = _captured_send(monkeypatch)
    _run_digest(monkeypatch, receipts, ("node-a", 7200), period="2026-09-25T07:00")
    _run_digest(monkeypatch, receipts, ("node-a", 7200), period="2026-09-25T08:00")
    assert calls[0]["dedup_key"] != calls[1]["dedup_key"], (
        "both cadence periods produced the same dedup key, so the second "
        "digest would be suppressed and the cadence silently stops"
    )


def test_two_recipients_are_not_each_others_duplicates(monkeypatch, receipts):
    calls = _captured_send(monkeypatch)
    _run_digest(monkeypatch, receipts, ("node-a", 7200), recipient="@ben", period="p")
    _run_digest(monkeypatch, receipts, ("node-a", 7200), recipient="@robin", period="p")
    assert calls[0]["dedup_key"] != calls[1]["dedup_key"], (
        "one person's digest suppresses another person's"
    )


def test_rerunning_the_verb_in_one_period_is_still_one_interruption(monkeypatch, receipts):
    """The guard must not throw away what dedup is FOR."""
    calls = _captured_send(monkeypatch)
    _run_digest(monkeypatch, receipts, ("node-a", 7200), period="p")
    _run_digest(monkeypatch, receipts, ("node-a", 7200), period="p")
    assert calls[0]["dedup_key"] == calls[1]["dedup_key"]


def test_a_suppressed_duplicate_does_not_read_as_a_failure(monkeypatch, receipts):
    """Suppression is the fabric working, not the digest breaking. It must
    be distinguishable from `denied`, or a scheduled run that correctly
    declines to re-interrupt somebody looks like an outage."""
    from axiom.extensions.builtins.receipts.skills import digest as digest_skill

    class _Receipt:
        outcome = "suppressed_duplicate"
        channel = None
        durable = False
        error = None

    monkeypatch.setattr(digest_skill, "_send", lambda *a, **k: _Receipt())
    result = _run_digest(monkeypatch, receipts, ("node-a", 7200))
    assert result.ok is True
    assert result.value["sent"] is False
    said = " ".join(result.actions_taken).lower()
    assert "already" in said, said
    assert "not delivered" not in said, said


# --- Founder review of the stage-0 artifact (2026-09-25) ---


def test_the_subject_names_the_site_when_every_case_shares_one(receipts):
    """A run that merely happens to find one site's cases is the same
    message to the reader as one scoped to it."""
    d = _digest(receipts, ("node-a", 9000))
    assert d.subject.endswith(" · local"), d.subject


def test_the_subject_stays_silent_when_the_cases_span_sites(receipts):
    """Naming one site would be naming the wrong one."""
    from axiom.extensions.builtins.receipts.brief import Brief, OversightItem
    from axiom.extensions.builtins.receipts.cases import compose_cases
    from axiom.extensions.builtins.receipts.digest import compose_digest

    items = [
        OversightItem(
            entity_kind="node",
            entity_id=entity,
            claim_kind="heartbeat",
            status="stale",
            evidence="e",
            site=site,
            observed_at=(NOW - timedelta(hours=2)).isoformat(),
        )
        for entity, site in (("node-a", "local"), ("node-b", "remote"))
    ]
    cases = compose_cases(items)
    brief = Brief(
        generated_at=NOW.isoformat(),
        site=None,
        focus=None,
        needs_you=items,
        trust_deltas=[],
        quiet_line="",
        counts={"cases": len(cases)},
        cases=cases,
    )
    assert " · " not in compose_digest(brief, now=NOW).subject


def test_a_case_nothing_here_can_act_on_says_what_that_means_for_the_reader(receipts):
    """A reason without its consequence is half a sentence. "Only node-a can
    put it right from its own side" leaves a person asking what to DO."""
    d = _digest(receipts, ("node-a", 9000))
    assert "it needs somebody with access to that machine" in d.body, d.body
