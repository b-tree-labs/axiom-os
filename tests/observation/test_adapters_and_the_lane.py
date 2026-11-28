# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265 step 2: the records that exist today present themselves as observations.

A silver row is a reading, a video trigger's event and an incident's cause are
events, and the journal has a lane for observations. The vocabularies the data
platform already publishes must not drift from the contract's, so a guard here
fails the moment they do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from axiom.observation import QUALITIES, SOURCE_CLASSES, ObservationError
from axiom.observation.adapters import from_cause, from_silver_row, from_trigger_event


def test_a_silver_row_is_a_reading():
    row = {
        "site": "ut", "feed": "loop.readings", "channel": "TC1",
        "ts": datetime(2026, 11, 22, 14, 0, tzinfo=UTC), "value": 312.4, "unit": "K",
        "quality": "good", "source_class": "measured", "schema_ref": "loop/csv-v1",
        "row_hash": "sha256:abc", "model_ref": None, "uncertainty": 0.5,
        "quality_reason": None, "derivation": "raw",
    }
    obs = from_silver_row(row)
    assert obs.family == "reading"
    assert [str(s) for s in obs.subjects] == ["channel:ut/loop.readings/TC1"]
    assert obs.content.value == 312.4 and obs.content.uncertainty == 0.5
    assert obs.source.producer == "feed:ut/loop.readings"
    assert obs.id == "reading:sha256:abc/TC1"


def test_a_silver_row_without_an_uncertainty_says_not_reported():
    row = {"site": "ut", "feed": "f", "channel": "c", "ts": datetime(2026, 1, 1, tzinfo=UTC),
           "value": 1.0, "unit": None, "quality": "good", "source_class": "measured",
           "schema_ref": "s", "row_hash": "h", "uncertainty": None}
    assert from_silver_row(row).content.uncertainty_words() == "not reported"


def test_a_modelled_silver_row_carries_its_model():
    row = {"site": "ut", "feed": "f", "channel": "c", "ts": datetime(2026, 1, 1, tzinfo=UTC),
           "value": 1.0, "unit": "K", "quality": "good", "source_class": "predicted",
           "schema_ref": "s", "row_hash": "h", "model_ref": "pool-thermal@3", "uncertainty": 0.2}
    obs = from_silver_row(row)
    assert obs.source_class == "predicted" and obs.derivation.method == "pool-thermal@3"


def test_a_legacy_ok_quality_reads_as_good():
    # silver's column default was once 'ok', outside ADR-132's vocabulary.
    row = {"site": "ut", "feed": "f", "channel": "c", "ts": datetime(2026, 1, 1, tzinfo=UTC),
           "value": 1.0, "quality": "ok", "source_class": "measured", "schema_ref": "s",
           "row_hash": "h"}
    assert from_silver_row(row).content.quality == "good"


@dataclass(frozen=True)
class _Trigger:
    stream: str = "cam-1"
    trigger: str = "motion@2"
    ts: str = "2026-11-22T14:00:05+00:00"
    kind: str = "started"
    incident_key: str = "cam-1:motion@2:ep7"
    severity: str = "warning"
    score: float | None = 0.83
    detail: dict = field(default_factory=dict)
    sensitivity: str = "internal"


def test_a_video_trigger_event_is_a_derived_event():
    obs = from_trigger_event(_Trigger())
    assert obs.family == "event" and obs.source_class == "derived"
    assert obs.content.phase == "started" and obs.content.severity == "warning"
    assert obs.derivation.method == "trigger:motion" and obs.derivation.version == "2"
    assert [str(s) for s in obs.subjects] == ["stream:cam-1", "episode:cam-1:motion@2:ep7"]
    assert obs.labels == ("internal",)


@dataclass(frozen=True)
class _Cause:
    area: str = "data"
    kind: str = "feed_stale"
    action: str = "data.refresh"
    subject: str = "loop.readings"
    error_class: str = "Stale"
    unmet: tuple = ()
    remedy: str = ""
    evidence_ref: str = "log:991"
    detail: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = datetime(2026, 11, 22, 14, 1, tzinfo=UTC)


def test_an_incident_cause_is_a_derived_event_that_points_at_its_evidence():
    obs = from_cause(_Cause(), agent="plinth", principal="person:ben")
    assert obs.family == "event" and obs.content.kind == "data.feed_stale"
    assert obs.derivation.inputs == ("log:991",)
    assert obs.source.agent == "plinth"
    with pytest.raises(ObservationError, match="time"):
        from_cause(_Cause(occurred_at=None), agent="plinth", principal="person:ben")


def test_the_journal_has_an_observations_lane():
    from axiom.extensions.builtins.journal import lanes

    assert "observations" in lanes.BUILTIN_LANES
    assert lanes.match("observation.statement")[0] == "observations"


def test_the_data_platforms_vocabularies_match_the_contract():
    """Until step 5 moves the data platform onto the contract's constants, they must agree."""
    from axiom.extensions.builtins.data_platform.company import QUALITY
    from axiom.extensions.builtins.data_platform.daq.envelope import SOURCE_CLASSES as EMITTED

    assert tuple(QUALITY) == QUALITIES
    assert set(EMITTED) <= set(SOURCE_CLASSES)
