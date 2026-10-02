# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A consumer can feed this surface its own population.

`brief.py` promised a `sources` parameter that did not exist, so every
caller read the fleet directly and a consumer with instrument readings,
irrigation zones or robots had nowhere to put them. These tests are the
promise, kept.

Three real consumers share one call: a facility registering instrument
readings, an agronomy deployment registering field equipment, and the
fleet evaluator registering nodes. That they are the same act is what
"about supervision rather than about machines" has to mean in code.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.sources import (
    register_source,
    registry,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_registry()
    yield
    reset_registry()


def _reading(entity: str, status: str = "unproven") -> OversightItem:
    return OversightItem(
        entity_kind="instrument",
        entity_id=entity,
        claim_kind="reading_validity",
        status=status,
        evidence="marked valid with no unit declared",
        site="site-a",
    )


def test_a_consumer_registers_its_population_and_its_claims_arrive():
    register_source("instruments", "instrument readings", lambda **_: [_reading("room-b2")])
    got = registry().collect()
    assert [i.entity_id for i in got.items] == ["room-b2"]
    assert got.unreadable == ()


def test_two_unlike_consumers_coexist():
    """The seam's actual job. A facility's instruments and a farm's
    equipment are one brief, and neither needs to know about the other."""
    register_source("instruments", "instrument readings", lambda **_: [_reading("room-b2")])
    register_source(
        "field-equipment",
        "field equipment",
        lambda **_: [
            OversightItem(
                entity_kind="irrigation",
                entity_id="zone-11",
                claim_kind="valve_state",
                status="failed",
                evidence="open past its window",
                site="site-a",
            )
        ],
    )
    got = registry().collect()
    assert {i.entity_kind for i in got.items} == {"instrument", "irrigation"}


def test_a_source_that_fails_is_named_not_swallowed():
    """A population nobody could read is not a population with nothing
    wrong — it is one you can no longer tell about. Swallowing it makes a
    partial brief indistinguishable from a quiet one, which is the
    liveness problem this surface exists to refuse."""

    def _broken(**_):
        raise ConnectionError("the facility historian is unreachable")

    register_source("instruments", "instrument readings", _broken)
    register_source("field-equipment", "field equipment", lambda **_: [_reading("zone-1")])

    got = registry().collect()
    assert [i.entity_id for i in got.items] == ["zone-1"], "one bad source blinded the rest"
    assert got.unreadable == (("instruments", "instrument readings", "ConnectionError"),)
    assert got.blind_spots() == [
        "could not read instrument readings (instruments): ConnectionError"
    ]


def test_registering_one_population_twice_is_refused():
    """Two readers of one population double-count every claim it makes."""
    register_source("instruments", "instrument readings", lambda **_: [])
    with pytest.raises(ValueError, match="already registered"):
        register_source("instruments", "instrument readings again", lambda **_: [])


def test_binding_the_default_registers_the_fleet():
    from axiom.extensions.builtins.receipts.sources import bind_default

    assert bind_default().names() == ["fleet"]
    assert bind_default().names() == ["fleet"], "binding twice double-registered"
