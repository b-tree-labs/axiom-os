# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One window vocabulary on every verb that takes a window.

``compare`` took ``--from/--to`` while ``aggregate`` and ``series`` took ``--start/--end``, so
a colleague typing the spelling that worked on one verb was refused by the next. They are the
same flags now, and ``--day``, ``--last`` and ``--yesterday`` name a window the way a person
does. These tests go through the real parser and the real translation to skill params.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform import cli

AGG = ["aggregate", "--table", "signals", "--column", "value", "--fn", "mean"]
SER = ["series", "--table", "signals", "--column", "value", "--bucket", "1 hour"]
CMP = ["compare", "--role", "fluid_temperature", "--bucket", "1 hour"]
SHOW = ["show", "T1"]


def _params(argv: list[str]) -> dict:
    return cli._args_to_params(cli._parser().parse_args(argv))


def _window(p: dict) -> tuple:
    w = p.get("window") or {}
    return w.get("start"), w.get("end")


WINDOWED = [AGG, SER, CMP]


@pytest.mark.parametrize("verb", WINDOWED, ids=["aggregate", "series", "compare"])
@pytest.mark.parametrize(("lo", "hi"), [("--start", "--end"), ("--from", "--to"), ("--start", "--to"), ("--from", "--end")])
def test_start_end_and_from_to_are_the_same_flags_on_every_windowed_verb(verb, lo, hi):
    p = _params([*verb, lo, "2026-03-10T00:00:00Z", hi, "2026-03-11T00:00:00Z"])
    assert _window(p) == ("2026-03-10T00:00:00Z", "2026-03-11T00:00:00Z")


@pytest.mark.parametrize("verb", WINDOWED, ids=["aggregate", "series", "compare"])
def test_a_day_is_a_window(verb):
    assert _window(_params([*verb, "--day", "2026-03-10"])) == ("2026-03-10T00:00:00Z", "2026-03-11T00:00:00Z")


@pytest.mark.parametrize("verb", WINDOWED, ids=["aggregate", "series", "compare"])
def test_last_is_a_window_ending_now(verb):
    before = datetime.now(UTC).replace(microsecond=0)
    start, end = _window(_params([*verb, "--last", "7d"]))
    after = datetime.now(UTC)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    assert before <= end_dt <= after
    assert datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00")) == timedelta(days=7)


@pytest.mark.parametrize("verb", WINDOWED, ids=["aggregate", "series", "compare"])
def test_yesterday_is_the_previous_utc_day(verb):
    expected = {(datetime.now(UTC).date() - timedelta(days=1)).isoformat()}
    start, _ = _window(_params([*verb, "--yesterday"]))
    expected.add((datetime.now(UTC).date() - timedelta(days=1)).isoformat())  # across midnight
    assert start[:10] in expected and start.endswith("T00:00:00Z")


def test_existing_spellings_keep_working():
    p = _params([*AGG, "--filter", "site = 'a'", "--start", "2026-03-10T00:00:00Z", "--time-column", "ts"])
    assert p["window"] == {"column": "ts", "start": "2026-03-10T00:00:00Z", "end": None}
    assert p["filter"] == "site = 'a'"


def test_two_ways_of_naming_a_window_at_once_is_an_error_in_words():
    p = _params([*AGG, "--day", "2026-03-10", "--last", "7d"])
    assert "each pick a window" in p["_window_error"]


def test_a_day_that_is_not_a_day_is_an_error_in_words():
    assert "YYYY-MM-DD" in _params([*SER, "--day", "march"])["_window_error"]


def test_show_takes_the_whole_vocabulary():
    p = _params([*SHOW, "--from", "2026-03-10", "--to", "2026-03-11", "--every", "1h", "--site", "example-loop"])
    assert (p["target"], p["start"], p["end"], p["every"], p["site"]) == (
        "T1", "2026-03-10", "2026-03-11", "1h", "example-loop")
    assert _params([*SHOW, "--day", "2026-03-10"])["day"] == "2026-03-10"
    assert _params([*SHOW, "--last", "36h"])["last"] == "36h"
    assert _params([*SHOW, "--yesterday"])["yesterday"] is True


def test_the_kit_verbs_that_take_a_window_take_the_same_flags():
    p = _params(["kit-pull", "--from", "2026-03-10", "--to", "2026-03-11"])
    assert (p["start"], p["end"]) == ("2026-03-10", "2026-03-11")
    d = _params(["kit-pull", "--day", "2026-03-10"])
    assert (d["start"], d["end"]) == ("2026-03-10T00:00:00Z", "2026-03-11T00:00:00Z")
    s = _params(["kit-snapshot", "--start", "2026-03-10", "--end", "2026-03-11"])
    assert (s["start"], s["end"]) == ("2026-03-10", "2026-03-11")
