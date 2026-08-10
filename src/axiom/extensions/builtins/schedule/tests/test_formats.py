# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cadence-format codec: read/write cron + ISO-8601, auto-detect, round-trip."""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.schedule import formats
from axiom.extensions.builtins.schedule.api import Cadence


def test_parse_cron_shortcuts():
    assert formats.parse_cron("@daily").cron == "0 0 * * *"
    assert formats.parse_cron("@hourly").cron == "0 * * * *"
    assert formats.parse_cron("@weekly").cron == "0 0 * * 0"


def test_parse_cron_passthrough_and_validation():
    assert formats.parse_cron("*/15 9-17 * * 1-5").cron == "*/15 9-17 * * 1-5"
    with pytest.raises(formats.FormatError):
        formats.parse_cron("not a cron")
    with pytest.raises(formats.FormatError):
        formats.parse_cron("@bogus")


def test_to_cron_from_interval():
    assert formats.to_cron(Cadence(kind="interval", interval=timedelta(hours=1))) == "0 * * * *"
    assert formats.to_cron(Cadence(kind="interval", interval=timedelta(days=1))) == "0 0 * * *"
    assert formats.to_cron(Cadence(kind="interval", interval=timedelta(minutes=30))) == "*/30 * * * *"
    assert formats.to_cron(Cadence(kind="interval", interval=timedelta(hours=2))) == "0 */2 * * *"


def test_to_cron_rejects_inexpressible():
    with pytest.raises(formats.FormatError):
        formats.to_cron(Cadence(kind="interval", interval=timedelta(minutes=7)))
    with pytest.raises(formats.FormatError):
        formats.to_cron(Cadence(kind="one_shot"))


def test_parse_iso8601_durations():
    assert formats.parse_iso8601("PT1H").interval == timedelta(hours=1)
    assert formats.parse_iso8601("P1D").interval == timedelta(days=1)
    assert formats.parse_iso8601("PT30M").interval == timedelta(minutes=30)
    assert formats.parse_iso8601("R/PT1H").interval == timedelta(hours=1)  # repeating interval
    with pytest.raises(formats.FormatError):
        formats.parse_iso8601("PT0S")


def test_to_iso8601():
    assert formats.to_iso8601(Cadence(kind="interval", interval=timedelta(hours=1))) == "PT1H"
    assert formats.to_iso8601(Cadence(kind="interval", interval=timedelta(days=1))) == "P1D"
    assert formats.to_iso8601(Cadence(kind="interval", interval=timedelta(hours=1, minutes=30))) == "PT1H30M"


def test_autodetect_dialect():
    assert formats.parse("@daily").kind == "cron"
    assert formats.parse("*/15 * * * *").kind == "cron"
    assert formats.parse("PT1H").kind == "interval"
    assert formats.parse("R/P1D").kind == "interval"


def test_round_trip_cron_and_interval():
    c = formats.parse("0 9 * * 1-5")
    assert formats.serialize(c, dialect="cron") == "0 9 * * 1-5"
    i = Cadence(kind="interval", interval=timedelta(hours=6))
    assert formats.parse(formats.serialize(i, dialect="iso8601"), dialect="iso8601").interval == i.interval


def test_rrule_ships_and_systemd_is_announced_not_silently_missing():
    assert formats.parse("RRULE:FREQ=WEEKLY;BYDAY=MO").kind == "rrule"  # rrule ships now
    with pytest.raises(NotImplementedError):
        formats.serialize(Cadence(kind="interval", interval=timedelta(hours=1)), dialect="systemd")
