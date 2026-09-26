# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""There was no freshness view at all, and a stopped stream looks healthy.

`silence()` and CreditedGuard watch a LIVE producer's stream and are good at
it — but they run inside the producer. They cannot say a site's silver data
stopped advancing four months ago, because in that case the producer is not
running to notice. ACU: 17.4M rows, last one 2026-05-20, nothing said so.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform import skills as data_skills
from axiom.extensions.builtins.data_platform.skills import ingest_freshness as mod
from axiom.infra.skills import SkillContext

NOW = "2026-09-19T00:00:00+00:00"


@pytest.fixture
def ctx(tmp_path):
    return SkillContext(
        registry=data_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )


def _rows(monkeypatch, rows):
    class _Conn:
        def execute(self, sql, params=None):
            return SimpleNamespace(fetchall=lambda: rows)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "axiom.infra.db.engine_for",
        lambda ext: SimpleNamespace(connect=lambda: _Conn()),
    )


def _row(site, stream, last, n=100, cls="measured"):
    return SimpleNamespace(
        site=site, stream=stream, source_class=cls, n=n,
        last_ts=datetime.fromisoformat(last),
    )


# --- an expectation is required to grade ------------------------------------


def test_without_a_declared_cadence_a_stream_is_ungraded_not_fine(ctx, monkeypatch):
    """A threshold invented here would be a number nobody agreed to, and STALE
    carries more authority than a guess deserves."""
    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    r = ctx.registry.invoke("data.ingest_freshness", {"now": NOW}, ctx)
    s = r.value["streams"][0]
    assert s["grade"] == "ungraded"
    assert s["age_hours"] > 2800, "the age is still reported — only the grade is withheld"
    assert r.ok, "ungraded is not a failure"


def test_ungraded_streams_are_named_because_nothing_will_alert_on_them(ctx, monkeypatch):
    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    r = ctx.registry.invoke("data.ingest_freshness", {"now": NOW}, ctx)
    assert any("UNGRADED" in a and "Nothing will alert" in a for a in r.actions_taken)


def test_with_an_expectation_a_stopped_stream_is_stale(ctx, monkeypatch):
    """The ACU case, graded."""
    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    r = ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24}, ctx
    )
    assert not r.ok
    assert r.value["stale"] == 1
    joined = " ".join(r.errors)
    assert "2026-05-20" in joined
    assert "expected within 24" in joined


def test_a_current_stream_is_fresh(ctx, monkeypatch):
    _rows(monkeypatch, [_row("ut-triga", "console", "2026-09-18T23:00:00+00:00")])
    r = ctx.registry.invoke("data.ingest_freshness", {"now": NOW, "expect_hours": 24}, ctx)
    assert r.ok
    assert r.value["streams"][0]["grade"] == "fresh"


def test_per_stream_expectations_are_honoured(ctx, monkeypatch):
    """A 1 Hz instrument and a nightly batch are not stale at the same age."""
    _rows(monkeypatch, [
        _row("s", "fast", "2026-09-18T23:00:00+00:00"),
        _row("s", "nightly", "2026-09-18T02:00:00+00:00"),
    ])
    r = ctx.registry.invoke(
        "data.ingest_freshness",
        {"now": NOW, "expect_hours": {"fast": 2, "nightly": 48}},
        ctx,
    )
    grades = {s["stream"]: s["grade"] for s in r.value["streams"]}
    assert grades == {"fast": "fresh", "nightly": "fresh"}


def test_a_stream_past_its_own_expectation_is_stale_even_when_others_are_fine(ctx, monkeypatch):
    _rows(monkeypatch, [
        _row("s", "fast", "2026-09-18T12:00:00+00:00"),
        _row("s", "nightly", "2026-09-18T23:00:00+00:00"),
    ])
    r = ctx.registry.invoke(
        "data.ingest_freshness",
        {"now": NOW, "expect_hours": {"fast": 2, "nightly": 48}},
        ctx,
    )
    assert not r.ok
    assert [s["stream"] for s in r.value["streams"] if s["grade"] == "stale"] == ["fast"]


# --- clocks ------------------------------------------------------------------


def test_a_naive_timestamp_is_unknown_age_not_an_invented_offset():
    """An hour of invented offset is an hour of invented freshness."""
    assert mod._age_hours("2026-05-20T00:00:00", datetime.now(UTC)) is None


def test_an_unparseable_timestamp_is_unknown_not_zero():
    assert mod._age_hours("20/05/2026", datetime.now(UTC)) is None


def test_no_last_ts_is_unknown():
    assert mod._age_hours(None, datetime.now(UTC)) is None


def test_an_unreadable_store_is_reported_not_raised(ctx, monkeypatch):
    def boom(ext):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("axiom.infra.db.engine_for", boom)
    r = ctx.registry.invoke("data.ingest_freshness", {}, ctx)
    assert not r.ok
    assert "could not read silver.signals" in " ".join(r.errors)


# --- telling someone --------------------------------------------------------
#
# The ROM bridge wrote `silent: True` and 2,130 reader errors to health.json
# and nothing read it for four days. Detection without a consumer is the same
# as no detection, and this is the consumer.


def _published(monkeypatch):
    calls = []

    def fake(intent, summary, **kw):
        calls.append({"intent": intent, "summary": summary, **kw})
        return "receipt-1"

    monkeypatch.setattr(
        "axiom.extensions.builtins.data_platform._herald.publish_event", fake
    )
    return calls


def test_inspecting_freshness_does_not_page_anyone(ctx, monkeypatch):
    """Read-only by default. An operator looking at freshness should not
    generate an alert by looking."""
    calls = _published(monkeypatch)
    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    r = ctx.registry.invoke("data.ingest_freshness", {"now": NOW, "expect_hours": 24}, ctx)
    assert not r.ok, "the stream is stale"
    assert calls == [], "looking at freshness raised an alert"


def test_the_scheduled_run_raises_herald(ctx, monkeypatch):
    calls = _published(monkeypatch)
    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    r = ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
    )
    assert len(calls) == 1
    assert calls[0]["intent"] == "data.ingest.stale"
    assert "acu-flowloop/loop.instrument" in calls[0]["body"]
    assert r.value["alert_receipt"] == "receipt-1"


def test_nothing_stale_raises_nothing(ctx, monkeypatch):
    calls = _published(monkeypatch)
    _rows(monkeypatch, [_row("ut-triga", "console", "2026-09-18T23:00:00+00:00")])
    ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
    )
    assert calls == []


def test_the_same_stale_set_reuses_one_dedup_key(ctx, monkeypatch):
    """An alert that repeats every run for four months is one people learn to
    close without reading."""
    calls = _published(monkeypatch)
    rows = [
        _row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00"),
        _row("vcu-flowloop", "vcu.msetf", "2026-07-24T00:00:00+00:00"),
    ]
    for _ in range(3):
        _rows(monkeypatch, rows)
        ctx.registry.invoke(
            "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
        )
    keys = {c["dedup_key"] for c in calls}
    assert len(keys) == 1, "the same stale set produced more than one dedup key"


def test_a_newly_stale_stream_changes_the_key_and_gets_through(ctx, monkeypatch):
    """The case that must never be suppressed: something NEW broke."""
    calls = _published(monkeypatch)

    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
    )
    _rows(monkeypatch, [
        _row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00"),
        _row("ut-triga", "rom.flux", "2026-09-15T23:04:00+00:00"),
    ])
    ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
    )

    assert len({c["dedup_key"] for c in calls}) == 2, (
        "a newly stale stream reused the previous dedup key and would have been "
        "suppressed — that is the alert that must always get through"
    )


def test_the_key_does_not_depend_on_stream_order(ctx, monkeypatch):
    """Two rows in a different order are the same outage."""
    calls = _published(monkeypatch)
    a = _row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")
    b = _row("vcu-flowloop", "vcu.msetf", "2026-07-24T00:00:00+00:00")
    for rows in ([a, b], [b, a]):
        _rows(monkeypatch, rows)
        ctx.registry.invoke(
            "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
        )
    assert len({c["dedup_key"] for c in calls}) == 1


def test_the_summary_names_the_worst_stream(ctx, monkeypatch):
    """A count alone sends someone to look; a name sends them to the right place."""
    calls = _published(monkeypatch)
    _rows(monkeypatch, [
        _row("ut-triga", "rom.flux", "2026-09-15T23:04:00+00:00"),
        _row("tamu-bubbleloop", "tamu.loop", "2025-07-06T15:28:00+00:00"),
    ])
    ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
    )
    assert "tamu-bubbleloop/tamu.loop" in calls[0]["summary"], calls[0]["summary"]


def test_a_herald_failure_does_not_fail_the_check(ctx, monkeypatch):
    """publish_event never raises and returns None when it could not send.
    A broken notifier must not make freshness itself unobservable."""
    monkeypatch.setattr(
        "axiom.extensions.builtins.data_platform._herald.publish_event",
        lambda *a, **k: None,
    )
    _rows(monkeypatch, [_row("acu-flowloop", "loop.instrument", "2026-05-20T00:00:00+00:00")])
    r = ctx.registry.invoke(
        "data.ingest_freshness", {"now": NOW, "expect_hours": 24, "alert": True}, ctx
    )
    assert r.value["alert_receipt"] is None
    assert any("did not complete" in a for a in r.actions_taken)
    assert r.value["stale"] == 1, "the finding survives a failed send"
