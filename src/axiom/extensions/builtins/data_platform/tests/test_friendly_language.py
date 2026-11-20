# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The words of the everyday verbs: windows, buckets, suggestions and the ``next:`` block."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform import friendly as fr

NOW = datetime(2026, 10, 26, 15, 30, 45, 123456, tzinfo=UTC)


def test_durations():
    assert fr.parse_duration("7d") == timedelta(days=7)
    assert fr.parse_duration("36h") == timedelta(hours=36)
    assert fr.parse_duration("90 minutes") == timedelta(minutes=90)
    assert fr.parse_duration("2w") == timedelta(weeks=2)
    for bad in ("", "soon", "0d", "-3h", "7"):
        with pytest.raises(fr.WindowError):
            fr.parse_duration(bad)


def test_a_bucket_in_any_spelling_is_the_interval_the_door_takes():
    assert fr.interval_words("1h") == "1 hour"
    assert fr.interval_words("15m") == "15 minutes"
    assert fr.interval_words("2 days") == "2 days"
    assert fr.interval_words("1d") == "1 day"
    with pytest.raises(fr.WindowError, match="15m, 1h or 1d"):
        fr.interval_words("often")


def test_windows():
    assert fr.resolve_window() is None
    day = fr.resolve_window(day="2026-03-10")
    assert (day.start, day.end) == ("2026-03-10T00:00:00Z", "2026-03-11T00:00:00Z")
    y = fr.resolve_window(yesterday=True, now=NOW)
    assert (y.start, y.end) == ("2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z")
    last = fr.resolve_window(last="36h", now=NOW)
    assert (last.start, last.end) == ("2026-10-25T03:30:45Z", "2026-10-26T15:30:45Z")
    span = fr.resolve_window(start="2026-03-10", end="2026-03-12T06:00:00+00:00")
    assert (span.start, span.end) == ("2026-03-10T00:00:00Z", "2026-03-12T06:00:00Z")
    assert fr.resolve_window(start="2026-03-10").end is None


def test_one_window_at_a_time():
    with pytest.raises(fr.WindowError, match="each pick a window"):
        fr.resolve_window(day="2026-03-10", yesterday=True)
    with pytest.raises(fr.WindowError, match="--start/--end and --last"):
        fr.resolve_window(start="2026-03-10", last="7d")


def test_a_bad_date_says_what_a_good_one_looks_like():
    with pytest.raises(fr.WindowError, match="YYYY-MM-DD"):
        fr.resolve_window(day="10/03/2026")
    with pytest.raises(fr.WindowError, match="2026-03-10"):
        fr.resolve_window(start="last tuesday")


@pytest.mark.parametrize(("seconds", "bucket"), [
    (1800, "1 minute"), (4 * 3600, "5 minutes"), (86400, "30 minutes"), (2 * 86400, "1 hour"),
    (7 * 86400, "6 hours"), (30 * 86400, "1 day"), (365 * 86400, "7 days"), (5 * 365 * 86400, "30 days"),
])
def test_a_bucket_follows_the_window(seconds, bucket):
    assert fr.choose_bucket(seconds) == bucket


def test_every_chosen_bucket_is_one_the_door_accepts():
    import re

    grammar = re.compile(r"^\d+\s+(second|minute|hour|day|week|month|year)s?$", re.I)
    for _, bucket in fr._BUCKET_STEPS:
        assert grammar.match(bucket)
    assert grammar.match(fr.choose_bucket(10**9))


def test_nearest_names():
    names = ["fluid_temperature", "wall_temperature", "line_pressure"]
    assert fr.close_matches("fluid_temp", names)[0] == "fluid_temperature"
    assert fr.close_matches("Fluid_Temperature", names) == ["fluid_temperature"]
    assert fr.close_matches("line_presure", names) == ["line_pressure"]
    assert fr.close_matches("zzz", names) == []


def test_unknown_message_suggests_or_lists():
    assert fr.unknown_message("role", "fluid_temp", "example-loop", ["fluid_temperature"]) == (
        "no role 'fluid_temp' at example-loop; did you mean fluid_temperature?")
    assert fr.unknown_message("role", "zzz", "example-loop", ["a"], listing="roles here: a") == (
        "no role 'zzz' at example-loop; roles here: a")
    assert fr.unknown_message("site", "zzz", "", []) == "no site 'zzz'"


def test_next_block_is_one_block_of_at_most_three_lines():
    block = fr.next_block(["a", "b", "c", "d"])
    assert block.count("next:") == 1
    assert block.strip().splitlines()[1:] == ["  a", "  b", "  c"]
    assert fr.next_block([]) == ""


def test_next_lines_are_built_from_the_values_in_the_result():
    roles = [{"role": "fluid_temperature", "sites": ["example-loop", "other-loop"]}]
    assert fr.next_for_roles(roles, "axi") == [
        "axi data show fluid_temperature --site example-loop",
        "axi data channels --site example-loop --role fluid_temperature",
        "axi data latest --site example-loop --role fluid_temperature",
    ]
    assert fr.next_for_roles([], "axi") == []
    chans = [{"channel": "T1", "role": "fluid_temperature", "last_ts": "2026-03-11T23:50:00Z"}]
    assert fr.next_for_channels(chans, "example-loop", "axi") == [
        "axi data show fluid_temperature --site example-loop",
        "axi data latest --site example-loop --role fluid_temperature",
        "axi data show T1 --site example-loop --day 2026-03-11",
    ]
    show = {"site": "example-loop", "target": "T1", "kind": "channel", "day": "2026-03-11"}
    assert fr.next_for_show(show, "axi") == [
        "axi data show T1 --site example-loop --start 2026-03-05 --end 2026-03-12 --every 1d",
        "axi data latest --site example-loop --channel T1",
    ]
