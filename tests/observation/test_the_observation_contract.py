# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265: everything the system learns is an observation.

One contract for three families (a reading, a statement, an event), with the
fields every surface needs: what it is about, when it was true and when it was
learned, who or what produced it and what kind of evidence it is, how sure we
are, what it was derived from, and who may see it. These tests hold the rules
the ADR decided, so a producer cannot mint an observation the surfaces would
have to second-guess.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from axiom.observation import (
    EVENT_PHASES,
    QUALITIES,
    SOURCE_CLASSES,
    WIRE_SCHEMA,
    Confidence,
    Derivation,
    Event,
    Observation,
    ObservationError,
    Reading,
    Source,
    Statement,
    SubjectRef,
    from_wire,
    to_wire,
)

T0 = datetime(2026, 11, 22, 14, 0, tzinfo=UTC)


def _reading(**over) -> Observation:
    base = dict(
        subjects=(SubjectRef("channel", "loop.TC1"),),
        observed_at=T0,
        recorded_at=T0 + timedelta(seconds=2),
        source=Source(producer="instrument:daq.loop", principal="node:edge-1"),
        source_class="measured",
        content=Reading(value=312.4, unit="K", quality="good", uncertainty=0.5),
    )
    base.update(over)
    return Observation(**base)


def _statement(**over) -> Observation:
    base = dict(
        subjects=(SubjectRef("run", "412"),),
        observed_at=T0,
        recorded_at=T0,
        source=Source(producer="logbook:ops", principal="person:ro-1"),
        source_class="stated",
        content=Statement(kind="note", text="Pool level checked at the console."),
    )
    base.update(over)
    return Observation(**base)


# -- the shape -------------------------------------------------------------------------------


def test_the_family_is_named_by_the_content():
    assert _reading().family == "reading"
    assert _statement().family == "statement"
    event = _statement(
        source_class="derived",
        content=Event(kind="threshold.crossed", phase="occurred"),
        derivation=Derivation(inputs=("obs-1",), method="rule:pool-limit", version="1"),
    )
    assert event.family == "event"


def test_an_observation_is_about_something():
    with pytest.raises(ObservationError, match="about"):
        _reading(subjects=())


def test_a_subject_reference_is_typed_and_round_trips():
    ref = SubjectRef.parse("channel:loop.TC1")
    assert (ref.kind, ref.id) == ("channel", "loop.TC1")
    assert str(ref) == "channel:loop.TC1"
    with pytest.raises(ObservationError):
        SubjectRef.parse("no-kind-here")


def test_times_carry_a_zone():
    with pytest.raises(ObservationError, match="time zone"):
        _reading(observed_at=datetime(2026, 11, 22, 14, 0))


# -- the kind of evidence ---------------------------------------------------------------------


def test_the_source_class_vocabulary_is_closed():
    assert set(SOURCE_CLASSES) == {
        "measured", "estimated", "predicted", "simulated", "derived", "stated", "interpreted",
    }
    with pytest.raises(ObservationError, match="source_class"):
        _reading(source_class="guessed")


def test_a_reading_is_never_stated_or_interpreted():
    with pytest.raises(ObservationError, match="reading"):
        _reading(source_class="stated")


def test_a_statement_is_stated_or_interpreted():
    with pytest.raises(ObservationError, match="statement"):
        _statement(source_class="measured")


def test_a_modelled_value_names_its_model():
    with pytest.raises(ObservationError, match="derivation"):
        _reading(source_class="predicted")
    ok = _reading(source_class="predicted",
                  derivation=Derivation(inputs=(), method="model:pool-thermal", version="3"))
    assert ok.derivation.method == "model:pool-thermal"


def test_a_derived_observation_names_what_it_came_from():
    with pytest.raises(ObservationError, match="came from"):
        _statement(
            source_class="derived",
            content=Event(kind="threshold.crossed", phase="occurred"),
            derivation=Derivation(inputs=(), method="rule:pool-limit", version="1"),
        )


def test_a_stated_observation_is_attributed_to_a_principal():
    with pytest.raises(ObservationError, match="attributed"):
        _statement(source=Source(producer="logbook:ops", principal=""))


def test_an_agents_claim_is_interpreted_and_says_who_is_accountable():
    claim = _statement(
        source=Source(producer="agent:scan", principal="person:ben", agent="scan"),
        source_class="interpreted",
        content=Statement(kind="blocker", text="The flow loop waits on a pump part."),
        confidence=Confidence.not_yet_known(),
        derivation=Derivation(inputs=("obs-7",), method="extractor:meeting-notes", version="2"),
    )
    assert claim.source.agent == "scan" and claim.source.principal == "person:ben"
    with pytest.raises(ObservationError, match="accountable"):
        replace(claim, source=Source(producer="agent:scan", principal="", agent="scan"))


# -- how sure: D10 ----------------------------------------------------------------------------


def test_an_interpreted_statement_carries_a_confidence_even_when_unknown():
    with pytest.raises(ObservationError, match="confidence"):
        _statement(
            source_class="interpreted",
            derivation=Derivation(inputs=("obs-7",), method="extractor:x", version="1"),
        )


def test_an_uncalibrated_confidence_is_not_yet_known_never_a_raw_score():
    unknown = Confidence.not_yet_known()
    assert unknown.value is None and unknown.calibrated is False
    assert unknown.words() == "confidence not yet known"
    with pytest.raises(ObservationError, match="calibrat"):
        Confidence(value=0.92, calibrated=False)


def test_a_calibrated_confidence_says_what_it_was_measured_against():
    c = Confidence(value=0.81, calibrated=True, basis="412 corrected extractions, extractor v2")
    assert c.words() == "confidence 81%"
    with pytest.raises(ObservationError, match="basis"):
        Confidence(value=0.81, calibrated=True)
    with pytest.raises(ObservationError, match="between 0 and 1"):
        Confidence(value=1.3, calibrated=True, basis="x")


def test_a_stated_observation_has_no_confidence_it_is_attributed_instead():
    with pytest.raises(ObservationError, match="attributed"):
        _statement(confidence=Confidence.not_yet_known())


def test_an_unreported_uncertainty_is_none_never_zero():
    silent = _reading(content=Reading(value=312.4, unit="K", quality="good", uncertainty=None))
    assert silent.content.uncertainty is None
    assert silent.content.uncertainty_words() == "not reported"
    assert _reading().content.uncertainty_words() == "± 0.5 K"
    with pytest.raises(ObservationError, match="negative"):
        Reading(value=1.0, unit="K", quality="good", uncertainty=-0.1)


def test_reading_quality_is_the_closed_consumer_vocabulary():
    assert QUALITIES == ("good", "suspect", "bad", "saturated", "stale")
    with pytest.raises(ObservationError, match="quality"):
        Reading(value=1.0, unit="K", quality="ok")


def test_an_event_phase_is_started_ended_or_occurred():
    assert EVENT_PHASES == ("started", "ended", "occurred")
    with pytest.raises(ObservationError, match="phase"):
        Event(kind="x", phase="happening")


# -- the wire: one envelope, every key present ------------------------------------------------


def test_the_wire_form_round_trips_every_family():
    for obs in (
        _reading(),
        _statement(),
        _statement(
            source_class="derived",
            content=Event(kind="feed.stale", phase="started", severity="warning"),
            derivation=Derivation(inputs=("obs-1", "obs-2"), method="rule:stale", version="1"),
            labels=("internal",),
        ),
    ):
        wire = to_wire(obs)
        assert wire["schema"] == WIRE_SCHEMA
        assert from_wire(wire) == obs


def test_the_wire_form_states_absence_instead_of_dropping_keys():
    wire = to_wire(_reading(content=Reading(value=1.0, unit="K", quality="good")))
    for key in ("uncertainty", "quality_reason"):
        assert key in wire["content"] and wire["content"][key] is None
    for key in ("confidence", "derivation", "labels", "id"):
        assert key in wire


def test_the_wire_form_is_validated_on_the_way_in():
    wire = to_wire(_reading())
    wire["source_class"] = "stated"
    with pytest.raises(ObservationError):
        from_wire(wire)
    wire = to_wire(_reading())
    wire["schema"] = "observation/99"
    with pytest.raises(ObservationError, match="schema"):
        from_wire(wire)
