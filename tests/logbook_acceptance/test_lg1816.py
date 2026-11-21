# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for #1816: the post-sign cross-check hook and logbook predicates.

After a statement is signed, each declared ``[[crosscheck]]`` field is compared with an
independent source at the record's time, and the comparison is stored as a linked system fact,
never written into the record. A ``[[predicate]]`` (for example "an open interval is expected
now") is evaluated on the tick, and a source that says an interval should be open while none is
raises a discrepancy fact. A source reads gold through the gold door only. Rows LG-1816-A1 to A9
are Axiom's; A10 is the web half, in axiom-appkit. No domain words: a "check log" with a
quantity and readings, a "run" interval and a source named ``feed``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402
    crosscheck,
    field_sources,
    predicates,
    registry,
    roles,
    service,
    site_settings,
)
from axiom.extensions.builtins.attest.field_sources import Instrument  # noqa: E402
from axiom.extensions.builtins.attest.gold_reader import (  # noqa: E402
    GOLD_VERBS,
    GoldReader,
    GoldReadRefused,
)
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.infra.bus import get_default_eventbus  # noqa: E402

SITE = "site-a"
LOG = "check_log"
ME = "@op1:site-a"
T0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
GAUGES = [Instrument(id="flow", label="Flow", unit="L/min",
                     uncertainty={"kind": "magnitude_only", "u": "0.5"}),
          Instrument(id="temp", label="Temperature", unit="degC")]  # fmt: skip

DECL = {
    "logbook": {"id": LOG, "version": "1", "display": "Check Log", "classification": "internal"},
    "interval": {"run": {"opens": ["START"], "closes": ["STOP"]}},
    "type": [
        {"id": "START", "meanings": ["authored"], "roles": ["operator"],
         "requires_interval": "none_open", "confirm": {"modalities_any": ["screen", "cli"]}},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"],
         "confirm": {"modalities_any": ["screen", "cli"]},
         "fields": [
             {"id": "power", "type": "quantity", "unit": "kW", "observe": True, "label": "Power",
              "convert": [{"from": "W", "to": "kW", "factor": "0.001"}]},
             {"id": "gauges", "type": "readings", "observe": True, "from_site": "gauges",
              "allow_not_taken": True},
             {"id": "note", "type": "text"},
         ]},
        {"id": "STOP", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "run",
         "confirm": {"modalities_any": ["screen", "cli"]}},
    ],
    "crosscheck": [
        {"field": "power", "source": "feed_power", "tolerance_from_site": "check.power_tolerance",
         "stale_after": "5m"},
        {"field": "gauges", "source": "feed_gauges", "tolerance_from_site": "check.gauge_tolerance",
         "tolerance": "1"},
    ],
    "predicate": [
        {"id": "active", "label": "Active", "source": "feed_active", "expects": "interval.run.open",
         "stale_after": "5m", "notify": ["operator"]},
    ],
}  # fmt: skip


class Feed:
    """The test's independent source: what it says, and every request it was asked."""

    def __init__(self):
        self.power = crosscheck.SourceValue(value="950", unit="kW", as_of=None)
        self.gauges = {"flow": crosscheck.SourceValue(value="12.0", unit="L/min", as_of=None),
                       "temp": crosscheck.SourceValue(value="31", unit="degC", as_of=None)}  # fmt: skip
        self.active: predicates.PredicateReading = predicates.PredicateReading(value=False, as_of=None)
        self.asked: list[crosscheck.SourceRequest] = []
        self.readers: list[GoldReader] = []

    def _at(self, v, at):
        return v if v.as_of is not None else replace(v, as_of=at)

    def power_source(self, req, gold):
        self.asked.append(req)
        self.readers.append(gold)
        return self._at(self.power, req.at)

    def gauge_source(self, req, gold):
        self.asked.append(req)
        return self._at(self.gauges[req.instrument], req.at)

    def active_source(self, site_id, at, gold):
        r = self.active
        return r if r.as_of is not None else replace(r, as_of=at)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


@pytest.fixture
def check_log(attest_db, clock, tmp_path):
    feed = Feed()
    field_sources.register("gauges", lambda site: GAUGES if site == SITE else [])
    crosscheck.register_source("feed_power", feed.power_source)
    crosscheck.register_source("feed_gauges", feed.gauge_source)
    predicates.register_source("feed_active", feed.active_source)
    site_settings.register(lambda site: {"check": {"power_tolerance": "10",
                                                   "gauge_tolerance": {"flow": "0.5"}}})  # fmt: skip
    registry.register(parse_logbook(DECL, source="test:check_log"))
    state_dir = tmp_path / "ticks"  # beside the HTTP client's own state dir, never in it
    state_dir.mkdir()
    roles.grant(ME, SITE, "operator", by="@admin:site-a", state_dir=state_dir)
    sent: list[tuple[str, dict]] = []
    predicates.set_sender(lambda recipient, fact: sent.append((recipient, fact)))
    yield {"feed": feed, "state_dir": state_dir, "sent": sent, "clock": clock}
    predicates.reset_sender()
    crosscheck.unregister_source("feed_power")
    crosscheck.unregister_source("feed_gauges")
    predicates.unregister_source("feed_active")
    field_sources.unregister("gauges")
    site_settings.unregister()


def draft(fields=None, entry_type="CHECK"):
    meaning = "authored" if entry_type in ("START", "STOP") else "performed"
    return service.create_draft(
        site_id=SITE, logbook=LOG, entry_type=entry_type, meaning=meaning,
        content={"title": entry_type.lower(), "fields": fields or {}}, origin="human",
        for_principal=ME,
    )  # fmt: skip


def sign(draft_id, signer):
    who = person()
    p = service.present(draft_id)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def check(signer, power=("950", "kW"), gauges=None):
    gauges = gauges if gauges is not None else {"flow": "12.2", "temp": "31"}
    return sign(draft({"power": {"value": power[0], "unit": power[1]}, "gauges": gauges}), signer)


def fact_for(rec, field):
    (f,) = [f for f in crosscheck.facts(SITE, LOG) if f["attestation_id"] == rec["attestation_id"]
            and f["field"] == field]  # fmt: skip
    return f


def captured(pattern):
    got: list[tuple[str, dict]] = []
    bus = get_default_eventbus()
    prefix = pattern.rstrip("*")

    def keep(subject, payload):
        if subject.startswith(prefix):
            got.append((subject, payload))

    sub = bus.subscribe(f"attest.{LOG}.*", keep, source="test")
    return got, lambda: bus.unsubscribe(sub)


# -- A1: the declarations are enforced now ------------------------------------------------------


@pytest.mark.acceptance("LG-1816-A1")
def test_lg1816_a1_crosschecks_and_predicates_load_enforced():
    book = parse_logbook(DECL)
    power, gauges = book.crosschecks
    assert (power.field, power.source, power.tolerance_key, power.stale_after_seconds) == (
        "power", "feed_power", "check.power_tolerance", 300)
    assert (gauges.tolerance_default, gauges.types) == ("1", ("CHECK",))
    (active,) = book.predicates
    assert (active.id, active.label, active.source, active.expects_interval, active.notify) == (
        "active", "Active", "feed_active", "run", ("operator",))
    assert not any(k.startswith(("crosscheck", "predicate")) for k in book.not_yet_enforced)


@pytest.mark.acceptance("LG-1816-A1")
@pytest.mark.parametrize(("table", "change", "words"), [
    ("crosscheck", {"field": "nope"}, "no entry type declares a field 'nope'"),
    ("crosscheck", {"field": "note"}, "quantity, number or readings"),
    ("crosscheck", {"source": ""}, "source names"),
    ("crosscheck", {"tolerance": "-1"}, "tolerance"),
    ("crosscheck", {"stale_after": "soon"}, "stale_after"),
    ("crosscheck", {"tolernce": "1"}, "unknown key"),
    ("predicate", {"expects": "interval.shift.open"}, "interval.<kind>.open"),
    ("predicate", {"id": "Bad Id"}, "id"),
    ("predicate", {"source": None}, "source names"),
    ("predicate", {"label": ""}, "label"),
    ("predicate", {"notfy": ["x"]}, "unknown key"),
])  # fmt: skip
def test_lg1816_a1_a_bad_declaration_is_refused_with_the_fix(table, change, words):
    bad = {**DECL, table: [{**DECL[table][0], **change}]}
    with pytest.raises(LogbookError, match=words.replace("(", r"\(").replace(".", r"\.")
                       .replace("<", ".").replace(">", ".")):  # fmt: skip
        parse_logbook(bad)


@pytest.mark.acceptance("LG-1816-A1")
def test_lg1816_a1_a_field_or_predicate_declared_twice_is_refused():
    with pytest.raises(LogbookError, match="once"):
        parse_logbook({**DECL, "crosscheck": [DECL["crosscheck"][0], DECL["crosscheck"][0]]})
    with pytest.raises(LogbookError, match="once"):
        parse_logbook({**DECL, "predicate": [DECL["predicate"][0], DECL["predicate"][0]]})


# -- A2: validate names each binding -------------------------------------------------------------

#: The check log's bindings as a file, as `axi log validate` reads one.
VALIDATE_TOML = """
[logbook]
id = "check_log"
version = "1"

[interval.run]
opens = ["START"]
closes = ["STOP"]

[[type]]
id = "START"
meanings = ["authored"]
roles = ["operator"]

[[type]]
id = "CHECK"
meanings = ["performed"]
roles = ["operator"]
fields = [
  { id = "power", type = "quantity", unit = "kW", observe = true },
  { id = "gauges", type = "readings", observe = true, from_site = "gauges" },
]

[[type]]
id = "STOP"
meanings = ["authored"]
roles = ["operator"]

[[crosscheck]]
field = "power"
source = "feed_power"
tolerance_from_site = "check.power_tolerance"

[[crosscheck]]
field = "gauges"
source = "feed_gauges"
tolerance = "1"

[[predicate]]
id = "active"
label = "Active"
source = "feed_active"
expects = "interval.run.open"
"""


@pytest.mark.acceptance("LG-1816-A2")
def test_lg1816_a2_validate_lists_each_binding_and_fails_on_one_nothing_binds(check_log, tmp_path):
    from axiom.extensions.builtins.attest.skills import logbook as logbook_skill

    path = tmp_path / "check_log.toml"
    path.write_text(VALIDATE_TOML, encoding="utf-8")
    ok = logbook_skill.validate({"path": str(path)})
    assert ok.ok, ok.errors
    assert ok.value["bindings"] == [
        {"kind": "crosscheck", "name": "power", "source": "feed_power", "bound": True},
        {"kind": "crosscheck", "name": "gauges", "source": "feed_gauges", "bound": True},
        {"kind": "predicate", "name": "active", "source": "feed_active", "bound": True},
    ]
    predicates.unregister_source("feed_active")
    bad = logbook_skill.validate({"path": str(path)})
    assert not bad.ok
    assert "feed_active" in bad.errors[0] and "predicate_source" in bad.errors[0]


# -- A3: the fact, linked, beside an untouched record ---------------------------------------------


@pytest.mark.acceptance("LG-1816-A3")
def test_lg1816_a3_one_linked_fact_per_field_and_the_record_is_untouched(check_log, node_signer):
    rec = check(node_signer, power=("955", "kW"), gauges={"flow": "12.2", "temp": "31"})
    power = fact_for(rec, "power")
    assert {k: power[k] for k in ("attestation_id", "field", "result", "reason", "tolerance")} == {
        "attestation_id": rec["attestation_id"], "field": "power", "result": "agrees",
        "reason": "", "tolerance": "10"}  # fmt: skip
    assert power["source_value"]["value"] == "950" and power["source_value"]["unit"] == "kW"
    assert power["labels"] == {"classification": "internal", "export_controlled": False}
    gauges = fact_for(rec, "gauges")
    by = {d["instrument"]: d for d in gauges["detail"]}
    assert (by["flow"]["result"], by["flow"]["tolerance"]) == ("agrees", "0.5"), "per-instrument site value"
    assert (by["temp"]["result"], by["temp"]["tolerance"]) == ("agrees", "1"), "the declared default"
    assert gauges["result"] == "agrees"
    again = service.record(rec["attestation_id"])
    assert again["digest"] == rec["digest"] and again["content"] == rec["content"]
    assert "crosscheck" not in str(again["content"])
    assert service.verify_logbook(SITE, LOG, {node_signer.key_id: node_signer.public_bytes}).ok


@pytest.mark.acceptance("LG-1816-A3")
def test_lg1816_a3_outside_tolerance_disagrees_in_exact_decimals(check_log, node_signer):
    feed = check_log["feed"]
    assert fact_for(check(node_signer, power=("960", "kW")), "power")["result"] == "agrees", "|10| <= 10"
    assert fact_for(check(node_signer, power=("960.01", "kW")), "power")["result"] == "disagrees"
    # A declared conversion applies: 955000 W is 955 kW.
    assert fact_for(check(node_signer, power=("955000", "W")), "power")["result"] == "agrees"
    feed.gauges["flow"] = crosscheck.SourceValue(value="11.6", unit="L/min", as_of=None)
    g_fact = fact_for(check(node_signer), "gauges")
    assert g_fact["result"] == "disagrees"
    assert [d["result"] for d in g_fact["detail"]] == ["disagrees", "agrees"]


# -- A4: honest not_checked ----------------------------------------------------------------------


@pytest.mark.acceptance("LG-1816-A4")
@pytest.mark.parametrize(("source", "reason"), [
    (lambda at: crosscheck.SourceValue("950", "kW", as_of=at - timedelta(minutes=6)), "stale"),
    (lambda at: crosscheck.SourceValue("950", "kW", as_of=None, state="stale"), "stale"),
    (lambda at: crosscheck.SourceValue("950", "kW", as_of=at, state="faulted"), "faulted"),
    (lambda at: crosscheck.SourceValue("950", "", as_of=at), "unitless"),
    (lambda at: crosscheck.SourceValue(None, "kW", as_of=at), "no_value"),
    (lambda at: crosscheck.SourceValue("950", "MW", as_of=at), "unit"),
    (lambda at: crosscheck.SourceValue("lots", "kW", as_of=at), "not_a_number"),
])  # fmt: skip
def test_lg1816_a4_a_source_that_cannot_answer_is_not_checked(check_log, node_signer, source, reason):
    crosscheck.register_source("feed_power", lambda req, gold: source(req.at))
    f = fact_for(check(node_signer), "power")
    assert (f["result"], f["reason"]) == ("not_checked", reason)


@pytest.mark.acceptance("LG-1816-A4")
def test_lg1816_a4_unbound_raising_untolerated_and_not_taken_are_not_checked(check_log, node_signer):
    crosscheck.unregister_source("feed_power")
    assert fact_for(check(node_signer), "power")["reason"] == "unbound"

    def boom(req, gold):
        raise RuntimeError("the feed is down")

    crosscheck.register_source("feed_power", boom)
    assert fact_for(check(node_signer), "power")["reason"] == "source_failed"
    site_settings.register(lambda site: {})
    crosscheck.register_source("feed_power", check_log["feed"].power_source)
    f = fact_for(check(node_signer), "power")
    assert (f["result"], f["reason"]) == ("not_checked", "no_tolerance")
    g_fact = fact_for(check(node_signer, gauges={"flow": {"not_taken": True, "reason": "blocked"},
                                                  "temp": "31"}), "gauges")  # fmt: skip
    assert [(d["instrument"], d["result"], d["reason"]) for d in g_fact["detail"]] == [
        ("flow", "not_checked", "not_taken"), ("temp", "agrees", "")]
    assert g_fact["result"] == "not_checked", "a field with any instrument unchecked is never agrees"


@pytest.mark.acceptance("LG-1816-A4")
def test_lg1816_a4_uncertainty_is_carried_and_never_combined(check_log, node_signer):
    check_log["feed"].gauges["flow"] = crosscheck.SourceValue(
        value="12.0", unit="L/min", as_of=None, uncertainty={"kind": "magnitude_only", "u": "0.2"})
    flow = next(d for d in fact_for(check(node_signer), "gauges")["detail"] if d["instrument"] == "flow")
    assert flow["entered"]["uncertainty"] == {"kind": "magnitude_only", "u": "0.5"}
    assert flow["source_value"]["uncertainty"] == {"kind": "magnitude_only", "u": "0.2"}
    assert "combined" not in flow and "u_combined" not in str(flow)


# -- A5: never pre-fills ---------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1816-A5")
def test_lg1816_a5_no_source_is_asked_before_signing_and_nothing_is_prefilled(check_log, node_signer):
    feed = check_log["feed"]
    # A value no id, digest or time can contain (a dot never appears in one of those).
    feed.power = crosscheck.SourceValue(value="947.25", unit="kW", as_of=None)
    d = draft({"power": {"value": "955", "unit": "kW"}, "gauges": {"flow": "12.2", "temp": "31"}})
    p = service.present(d)
    assert feed.asked == [], "drafting and presenting never reach a source"
    assert "947.25" not in p.rendered and "947.25" not in str(p.statement)
    assert "947.25" not in str(service.draft(d))
    service.respond(p.presentation_id, principal=person().principal, answer="sign", via="cli")
    rec = service.sign(p.presentation_id, signatory=person(), signer=node_signer).record
    assert feed.asked, "asked after signing"
    assert rec["content"]["fields"]["power"]["value"] == "955", "the person's value stays"


# -- A6: published, streamed, best effort, caught up --------------------------------------------


@pytest.mark.acceptance("LG-1816-A6")
def test_lg1816_a6_a_disagreement_is_published_with_the_logs_labels(check_log, node_signer):
    got, done = captured(f"attest.{LOG}.crosscheck_*")
    try:
        rec = check(node_signer, power=("990", "kW"))
    finally:
        done()
    subjects = [s for s, _ in got]
    assert subjects == [f"attest.{LOG}.crosscheck_disagrees"]
    payload = got[0][1]
    assert payload["attestation_id"] == rec["attestation_id"] and payload["field"] == "power"
    assert payload["labels"] == {"classification": "internal", "export_controlled": False}
    assert payload["site_id"] == SITE


@pytest.mark.acceptance("LG-1816-A6")
def test_lg1816_a6_the_stream_forwards_facts_without_an_id():
    from axiom.extensions.builtins.attest import api

    assert api.stream_event("crosscheck_disagrees") == "crosscheck"
    assert api.stream_event("predicate_discrepancy") == "predicate"
    assert api.stream_event("obligation_missed") == "obligation"
    assert api.stream_event("signed") is None, "a record keeps its own frame and id"


@pytest.mark.acceptance("LG-1816-A6")
def test_lg1816_a6_a_failing_hook_never_fails_the_signature_and_the_tick_catches_up(
    check_log, node_signer
):
    def broken(record):
        raise RuntimeError("the fact store is unreachable")

    real, crosscheck.check_record = crosscheck.check_record, broken
    try:
        rec = check(node_signer)
    finally:
        crosscheck.check_record = real
    assert service.record(rec["attestation_id"])["seq"] == rec["seq"], "signed all the same"
    assert crosscheck.facts(SITE, LOG) == []
    done = crosscheck.catch_up(SITE, LOG)
    assert done == 2, "power and gauges, once"
    assert crosscheck.catch_up(SITE, LOG) == 0
    assert {f["field"] for f in crosscheck.facts(SITE, LOG)} == {"power", "gauges"}


# -- A7: gold, through the gold door ----------------------------------------------------------------


def _registry_with(*names):
    from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

    calls: list[tuple[str, dict]] = []
    reg = SkillRegistry()
    for name in names:
        def fn(params, ctx=None, _n=name):
            calls.append((_n, dict(params)))
            if params.get("table") == "log_records":
                return SkillResult(ok=False, errors=["no such gold object: log_records"])
            return SkillResult(ok=True, value={"data": {"points": [["t", "950"]]}})

        reg.register_skill(SkillSpec(name=name, fn=fn, description=name, inputs={},
                                     surfaces=("cli",), side_effects=False, idempotent=True,
                                     risk="read"))  # fmt: skip
    return reg, calls


@pytest.mark.acceptance("LG-1816-A7")
def test_lg1816_a7_a_source_reads_gold_only_through_the_gold_verbs(tmp_path):
    from axiom.infra.skills import SkillContext

    reg, calls = _registry_with(*GOLD_VERBS, "data.sample", "data.conform_try")
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("lg1816"))
    gold = GoldReader(ctx)
    assert set(GOLD_VERBS) == {"data.series", "data.aggregate", "data.latest", "data.show", "data.table"}
    assert gold.read("data.series", table="signals", column="value")["data"]["points"]
    assert calls == [("data.series", {"table": "signals", "column": "value"})]
    for verb in ("data.sample", "data.conform_try", "attest.show"):
        with pytest.raises(GoldReadRefused, match="gold verb"):
            gold.read(verb, table="signals")
    with pytest.raises(GoldReadRefused, match="gold"):
        gold.read("data.series", table="signals", tier="silver")
    with pytest.raises(GoldReadRefused, match="log_records"):
        gold.read("data.table", table="log_records")
    assert [c[0] for c in calls] == ["data.series", "data.table"], "refused before any other verb ran"
    import inspect

    from axiom.extensions.builtins.attest import gold_reader

    assert "invoke_capability" in inspect.getsource(gold_reader.GoldReader.read)


@pytest.mark.acceptance("LG-1816-A7")
def test_lg1816_a7_each_source_is_handed_a_gold_reader(check_log, node_signer):
    check(node_signer)
    assert check_log["feed"].readers and all(isinstance(r, GoldReader) for r in check_log["feed"].readers)


# -- A8: predicates on the tick -------------------------------------------------------------------


@pytest.mark.acceptance("LG-1816-A8")
def test_lg1816_a8_expected_open_with_none_open_is_one_discrepancy(check_log, node_signer):
    feed, state_dir = check_log["feed"], check_log["state_dir"]
    feed.active = predicates.PredicateReading(value=True, as_of=None, reason="output above idle")
    got, done = captured(f"attest.{LOG}.predicate_*")
    try:
        first = predicates.tick(SITE, LOG, now=T0, state_dir=state_dir)
        again = predicates.tick(SITE, LOG, now=T0 + timedelta(minutes=1), state_dir=state_dir)
    finally:
        done()
    assert [c["state"] for c in first] == ["discrepancy"] and again == [], "on change only"
    (fact,) = first
    assert (fact["predicate"], fact["expects"]["kind"]) == ("active", "run")
    assert fact["labels"] == {"classification": "internal", "export_controlled": False}
    assert [s for s, _ in got] == [f"attest.{LOG}.predicate_discrepancy"]
    assert got[0][1]["labels"]["classification"] == "internal"
    assert [r for r, _ in check_log["sent"]] == [ME], "the notify roles are told"


@pytest.mark.acceptance("LG-1816-A8")
def test_lg1816_a8_an_open_interval_or_a_no_is_consistent_once(check_log, node_signer):
    feed, state_dir = check_log["feed"], check_log["state_dir"]
    feed.active = predicates.PredicateReading(value=True, as_of=None)
    predicates.tick(SITE, LOG, now=T0, state_dir=state_dir)
    sign(draft(entry_type="START"), node_signer)
    cleared = predicates.tick(SITE, LOG, now=T0 + timedelta(minutes=1), state_dir=state_dir)
    assert [c["state"] for c in cleared] == ["consistent"]
    feed.active = predicates.PredicateReading(value=False, as_of=None)
    assert predicates.tick(SITE, LOG, now=T0 + timedelta(minutes=2), state_dir=state_dir) == [], (
        "an open interval the source does not expect is consistent: it may be open while idle")
    states = [e["state"] for e in predicates.events(SITE, LOG)]
    assert states == ["discrepancy", "consistent"]


@pytest.mark.acceptance("LG-1816-A8")
@pytest.mark.parametrize(("reading", "reason"), [
    (lambda at: predicates.PredicateReading(value=True, as_of=at - timedelta(minutes=6)), "stale"),
    (lambda at: predicates.PredicateReading(value=None, as_of=at, reason="faulted"), "faulted"),
    (lambda at: predicates.PredicateReading(value=False, as_of=None), "stale"),
])  # fmt: skip
def test_lg1816_a8_a_source_that_cannot_say_is_unknown_never_consistent(check_log, reading, reason):
    predicates.register_source("feed_active", lambda site, at, gold: reading(at))
    (fact,) = predicates.tick(SITE, LOG, now=T0, state_dir=check_log["state_dir"])
    assert (fact["state"], fact["reason"]) == ("unknown", reason)
    predicates.unregister_source("feed_active")
    (unbound,) = predicates.tick(SITE, LOG, now=T0 + timedelta(minutes=1), state_dir=check_log["state_dir"])
    assert (unbound["state"], unbound["reason"]) == ("unknown", "unbound")


# -- A9: the door ---------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1816-A9")
def test_lg1816_a9_the_crosschecks_doors_are_registered_rows():
    http = g.get("attest.http.crosschecks")
    assert http.route == ("GET", "/api/v1/attest/{logbook}/crosschecks")
    assert (http.shape, http.surface, http.serves) == ("target", "api", "crosschecks")
    assert set(http.params) >= {"result", "attestation_id"}
    skill = g.get("attest.crosschecks")
    assert skill.skill == "attest.crosschecks" and skill.serves == "crosschecks"
    from axiom.extensions.builtins.attest.skills import DOOR_SKILLS

    assert "attest.crosschecks" in DOOR_SKILLS


@pytest.mark.acceptance("LG-1816-A9")
def test_lg1816_a9_served_with_labels_facts_and_worded_predicate_state(check_log, node_signer, client):
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_

    agrees = check(node_signer)
    off = check(node_signer, power=("990", "kW"))
    check_log["feed"].active = predicates.PredicateReading(value=True, as_of=None, reason="output above idle")
    predicates.tick(SITE, LOG, now=T0, state_dir=check_log["state_dir"])
    c = as_(client, OP1)
    got = c.get(f"/api/v1/attest/{LOG}/crosschecks?site={SITE}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert (body["site_id"], body["logbook"]) == (SITE, LOG)
    assert body["labels"] == {"classification": "internal", "export_controlled": False}
    assert {(f["attestation_id"], f["field"]) for f in body["crosschecks"]} == {
        (r["attestation_id"], fld) for r in (agrees, off) for fld in ("power", "gauges")}
    (state,) = body["predicates"]
    assert (state["predicate"], state["state"], state["honest_state"]) == ("active", "discrepancy", "unlogged")
    assert state["words"] == "Active with no open run", state
    only = c.get(f"/api/v1/attest/{LOG}/crosschecks?site={SITE}&result=disagrees").json()
    assert [(f["attestation_id"], f["field"]) for f in only["crosschecks"]] == [(off["attestation_id"], "power")]
    one = c.get(f"/api/v1/attest/{LOG}/crosschecks?site={SITE}&attestation_id={agrees['attestation_id']}")
    assert {f["attestation_id"] for f in one.json()["crosschecks"]} == {agrees["attestation_id"]}
    bad = c.get(f"/api/v1/attest/{LOG}/crosschecks?site={SITE}&result=fine")
    assert bad.status_code == 400 and bad.json()["code"] == "filter"


@pytest.mark.acceptance("LG-1816-A9")
def test_lg1816_a9_a_predicate_that_cannot_run_is_feed_stale_and_none_is_nothing(check_log):
    assert predicates.current(SITE, LOG) == [], "no fact: the page claims nothing"
    check_log["feed"].active = predicates.PredicateReading(value=None, as_of=T0, reason="faulted")
    predicates.tick(SITE, LOG, now=T0, state_dir=check_log["state_dir"])
    (state,) = predicates.current(SITE, LOG)
    assert (state["state"], state["honest_state"]) == ("unknown", "feed_stale")
    assert state["words"] == "Active check cannot run: the source is faulted"


@pytest.mark.acceptance("LG-1816-A9")
def test_lg1816_a9_a_refused_caller_learns_nothing_of_the_facts(check_log, node_signer, client):
    from axiom.extensions.builtins.attest.tests.test_api import OP2, as_

    check(node_signer, power=("990", "kW"))
    registry.register(replace(registry.get(LOG), readers=("auditor",)))
    got = as_(client, OP2).get(f"/api/v1/attest/{LOG}/crosschecks?site={SITE}")
    assert got.status_code == 404 and set(got.json()) == {"detail", "code"}
    assert "990" not in got.text and "disagrees" not in got.text
