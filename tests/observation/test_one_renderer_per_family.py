# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265 D7: one human renderer per family, used by every CLI that prints an observation.

A reading says its value, unit and how sure it is (and its quality when it is not good);
a statement says what was claimed and by whom, and how sure an agent is when an agent
interpreted it; an event says what happened, its phase and severity. Each family has one
line shape, so the same observation reads the same wherever it is printed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.observation import (
    Confidence,
    Derivation,
    Event,
    Observation,
    Reading,
    Source,
    Statement,
    SubjectRef,
)
from axiom.observation.render import reading_text, render_text

T0 = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)


def _obs(content, source_class, **over):
    base = dict(
        subjects=(SubjectRef("channel", "loop.TC1"),),
        observed_at=T0,
        recorded_at=T0,
        source=Source(producer="instrument:daq.loop", principal="node:edge-1"),
        source_class=source_class,
        content=content,
    )
    base.update(over)
    return Observation(**base)


def test_a_reading_says_its_value_unit_and_how_sure():
    assert reading_text(312.4, "K", 0.5) == "312.4 K ± 0.5 K"
    assert reading_text(312.4, "K", None) == "312.4 K (uncertainty not reported)"
    assert reading_text(312.4, "K", {"low": 0.2, "high": 0.4}) == "312.4 K ± 0.2 to 0.4 K"


def test_a_reading_names_its_quality_only_when_it_is_not_good():
    assert reading_text(312.4, "K", 0.5, quality="good") == "312.4 K ± 0.5 K"
    assert reading_text(312.4, "K", 0.5, quality="suspect") == "312.4 K ± 0.5 K, suspect"
    assert reading_text(None, "K", None) == "no value"


def test_the_reading_family_line():
    line = render_text(_obs(Reading(value=312.4, unit="K", uncertainty=0.5), "measured"))
    assert line == "channel:loop.TC1  312.4 K ± 0.5 K  at 2026-11-24 14:00Z"


def test_a_modelled_reading_says_it_is_not_a_measurement():
    obs = _obs(Reading(value=310.0, unit="K"), "predicted",
               derivation=Derivation(inputs=(), method="model:pool-thermal", version="3"))
    assert render_text(obs).endswith("(predicted by model:pool-thermal v3)")


def test_the_statement_family_line_is_attributed():
    obs = _obs(Statement(kind="note", text="Pool level checked."), "stated",
               source=Source(producer="logbook:ops", principal="person:ro-1"))
    assert render_text(obs) == "note: Pool level checked.  (person:ro-1, 2026-11-24 14:00Z)"


def test_an_interpreted_statement_names_the_agent_and_how_sure_it_is():
    obs = _obs(Statement(kind="blocker", text="The loop waits on a pump part."), "interpreted",
               source=Source(producer="agent:scan", principal="person:ben", agent="scan"),
               confidence=Confidence.not_yet_known(),
               derivation=Derivation(inputs=("obs-7",), method="extractor:notes", version="2"))
    assert render_text(obs) == (
        "blocker: The loop waits on a pump part.  "
        "(scan for person:ben, confidence not yet known, 2026-11-24 14:00Z)"
    )


def test_the_event_family_line():
    obs = _obs(Event(kind="feed.stale", phase="started", severity="warning"), "derived",
               derivation=Derivation(inputs=("obs-1",), method="rule:stale", version="1"))
    assert render_text(obs) == "feed.stale started (warning)  channel:loop.TC1  at 2026-11-24 14:00Z"
