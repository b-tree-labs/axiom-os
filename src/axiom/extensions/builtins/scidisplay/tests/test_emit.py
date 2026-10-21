# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One emitter, so `--json` means the same thing in every extension.

These tests exist because `triga_telemetry._emit` accepted an `as_json`
flag, ignored it, and printed JSON down both paths — so the eight verbs
routing through it had no human output at all, and the flag that was
supposed to ask for JSON changed nothing. A reviewer cannot see that by
reading a table; it is only visible if something asserts the two paths
DIFFER.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.scidisplay.emit import emit

ROWS = [
    {"metric": "FuelTemp1", "unit": "degC", "n": 182344},
    {"metric": "Power", "unit": "kW", "n": 4},
]
COLUMNS = (
    ("metric", "metric", True, "left"),
    ("unit", "unit", False, "left"),
    ("n", "samples", True, "right"),
)


def _out(capsys) -> str:
    return capsys.readouterr().out


class TestTheFlagChangesTheOutput:
    """The regression that started this. Both assertions are load-bearing."""

    def test_json_path_is_parseable_json(self, capsys):
        emit(ROWS, as_json=True, title="metrics", columns=COLUMNS)
        assert json.loads(_out(capsys)) == ROWS

    def test_human_path_is_not_json(self, capsys):
        emit(ROWS, as_json=False, title="metrics", columns=COLUMNS)
        text = _out(capsys)
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
        # the column LABEL, not the raw key — proves it went through the spec
        assert "SAMPLES" in text.upper()

    def test_the_two_paths_differ(self, capsys):
        emit(ROWS, as_json=True, title="metrics", columns=COLUMNS)
        as_json = _out(capsys)
        emit(ROWS, as_json=False, title="metrics", columns=COLUMNS)
        assert _out(capsys) != as_json


class TestHumanOutputIsIndented:
    def test_every_line_carries_the_standard_indent(self, capsys):
        emit(ROWS, as_json=False, title="metrics", columns=COLUMNS)
        lines = [ln for ln in _out(capsys).splitlines() if ln.strip()]
        assert lines, "emitted nothing"
        assert all(ln.startswith("  ") for ln in lines), lines

    def test_json_is_never_indented_by_us(self, capsys):
        """Indenting JSON would break `| jq`, which is the point of --json."""
        emit(ROWS, as_json=True, title="metrics", columns=COLUMNS)
        assert not _out(capsys).startswith(" ")


class TestShapesAnExtensionActuallyReturns:
    def test_columns_are_derived_when_not_declared(self, capsys):
        """A caller with nothing to say about columns still gets a table."""
        emit(ROWS, as_json=False, title="metrics")
        assert "METRIC" in _out(capsys).upper()

    def test_a_single_mapping_renders_as_fields_not_a_one_row_table(self, capsys):
        emit({"metric": "Power", "value": 950.0}, as_json=False, title="latest")
        text = _out(capsys)
        assert "metric" in text and "Power" in text

    def test_empty_says_so_rather_than_printing_a_headed_void(self, capsys):
        emit([], as_json=False, title="metrics")
        assert "no metrics" in _out(capsys)

    def test_empty_still_round_trips_as_json(self, capsys):
        emit([], as_json=True, title="metrics")
        assert json.loads(_out(capsys)) == []

    def test_none_is_not_rendered_as_the_word_none(self, capsys):
        emit(None, as_json=False, title="latest")
        assert "None" not in _out(capsys)

    def test_a_scalar_is_printed_plainly(self, capsys):
        emit(42, as_json=False, title="count")
        assert "42" in _out(capsys)


class TestItReturnsAnExitCode:
    """`return 0 if as_json else 0` was the original. Both branches were 0,
    which is a tautology dressed as a decision — so the return value is
    asserted here rather than assumed."""

    @pytest.mark.parametrize("as_json", [True, False])
    def test_success_is_zero(self, as_json, capsys):
        assert emit(ROWS, as_json=as_json, title="metrics") == 0
        capsys.readouterr()


class TestTheEmitterDoesNotReIndentWhatIsAlreadyIndented:
    """`cli_format.table` and `kv_line` each apply the standard indent.

    Wrapping their output in another indent is invisible in a unit test that
    only asserts `startswith("  ")` — four spaces start with two. So these
    assert the EXACT depth.
    """

    def test_a_table_sits_at_the_standard_indent_not_twice_it(self, capsys):
        emit(ROWS, as_json=False, title="metrics", columns=COLUMNS)
        lines = [ln for ln in _out(capsys).splitlines() if ln.strip()]
        depths = {len(ln) - len(ln.lstrip(" ")) for ln in lines}
        assert min(depths) == 2, f"table indented to {sorted(depths)}, expected 2"

    def test_fields_sit_at_the_standard_indent(self, capsys):
        emit({"metric": "Power", "value": 950.0}, as_json=False, title="latest")
        lines = [ln for ln in _out(capsys).splitlines() if ln.strip()]
        assert {len(ln) - len(ln.lstrip(" ")) for ln in lines} == {2}

    def test_a_field_label_is_separated_from_its_value(self, capsys):
        """`metric:Power` — the colon ate the padding when key_width forgot it."""
        emit({"metric": "Power", "value": 950.0}, as_json=False, title="latest")
        for line in _out(capsys).splitlines():
            if ":" in line:
                assert line.split(":", 1)[1].startswith(" "), repr(line)
