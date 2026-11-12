# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What a person sees: text that ends in what to type next, and pipes that never do.

Goes through ``main`` and the real skill registry, with a bound laptop whose node serves only
``data.aggregate``, ``data.series`` and ``data.roles``.
"""

from __future__ import annotations

import csv
import io
import json
import re

import pytest

from axiom.extensions.builtins.data_platform import cli
from axiom.extensions.builtins.data_platform import read_target as rt
from axiom.extensions.builtins.data_platform.skills import gold

from .fake_node import SITE, FakeNode


@pytest.fixture(autouse=True)
def node(monkeypatch):
    fake = FakeNode.standard()
    monkeypatch.setattr(gold, "_GUARD", None)
    monkeypatch.setattr(rt, "ask", fake.ask)
    monkeypatch.setattr(
        rt, "resolve_read_target",
        lambda: rt.ReadTarget("binding", site=SITE, face_url="https://node.example"),
    )
    monkeypatch.setattr(rt, "current_binding", lambda: (SITE, "https://node.example"))
    return fake


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _next_lines(text: str) -> list[str]:
    block = text.rstrip("\n").split("\nnext:\n")
    assert len(block) == 2, f"expected exactly one next: block in:\n{text}"
    return [ln.strip() for ln in block[1].splitlines()]


@pytest.mark.parametrize("argv", [
    ["roles"], ["channels"], ["latest"], ["show", "T1"], ["show", "fluid_temperature"],
], ids=["roles", "channels", "latest", "show-channel", "show-role"])
def test_text_ends_with_one_next_block_of_two_or_three_runnable_lines(capsys, argv):
    code, out, err = _run(capsys, *argv)
    assert code == 0, err
    lines = _next_lines(out)
    assert 2 <= len(lines) <= 3
    for line in lines:
        assert line.startswith("axi data "), line


def test_next_lines_use_values_that_are_in_the_answer(capsys):
    _, out, _ = _run(capsys, "channels")
    lines = _next_lines(out)
    roles_in_answer = {"line_pressure", "fluid_temperature"}
    assert any(f"show {r} --site example-loop" in ln for ln in lines for r in roles_in_answer)
    assert any("--day 2026-03-11" in ln for ln in lines)  # a day that has data, not today


@pytest.mark.parametrize("argv", [["roles"], ["channels"], ["latest"], ["show", "T1"]],
                         ids=["roles", "channels", "latest", "show"])
@pytest.mark.parametrize("fmt", ["json", "csv", "ndjson"])
def test_machine_formats_carry_no_next_block(capsys, argv, fmt):
    code, out, err = _run(capsys, *argv, "--format", fmt)
    assert code == 0, err
    assert "next:" not in out and "axi data" not in out


def test_json_is_the_result_and_only_the_result(capsys):
    _, out, _ = _run(capsys, "show", "T1", "--format", "json")
    value = json.loads(out)["value"]
    assert set(value) <= {"data", "provenance", "unit", "served"}
    assert value["data"]["series"][0]["channel"] == "T1"


def test_global_json_flag_is_clean_too(capsys):
    _, out, _ = _run(capsys, "--json", "channels")
    assert "next:" not in out
    assert json.loads(out)["ok"] is True


def test_csv_of_a_series_is_one_row_per_point_with_the_unit_in_the_header(capsys):
    _, out, _ = _run(capsys, "show", "fluid_temperature", "--every", "6h", "--format", "csv")
    rows = list(csv.DictReader(io.StringIO(out)))
    assert {"t", "value_degC", "channel", "feed"} <= set(rows[0])
    assert {r["channel"] for r in rows} == {"T1", "T2"}
    assert len(rows) == 8  # two channels, four six-hour buckets in the latest day


def test_ndjson_of_channels(capsys):
    _, out, _ = _run(capsys, "channels", "--format", "ndjson")
    records = [json.loads(ln) for ln in out.splitlines()]
    assert {r["channel"] for r in records} >= {"T1", "T2", "P1", "Old1"}


def test_show_text_names_what_it_chose(capsys):
    _, out, _ = _run(capsys, "show", "fluid_temperature")
    head = out.splitlines()[0]
    assert head == "fluid_temperature at example-loop, 2026-03-11 (UTC), mean every 30 minutes, degC"
    assert "showing the latest day with readings" in out
    assert re.search(r"^T1\s+48\s", out, re.M)


def test_an_error_is_a_sentence_on_stderr_with_no_next_block(capsys):
    code, out, err = _run(capsys, "show", "fluid_temp")
    assert code != 0 and out == ""
    assert "did you mean fluid_temperature?" in err


def test_an_empty_window_error_names_the_flag_that_reaches_data(capsys):
    code, _, err = _run(capsys, "show", "T1", "--yesterday")
    assert code != 0
    assert "its last reading was 2026-03-11; try --day 2026-03-11" in err


def test_the_window_flags_work_the_same_on_the_older_verbs(capsys, node):
    code, out, err = _run(
        capsys, "aggregate", "--table", "signals", "--column", "value", "--fn", "max",
        "--filter", "site = 'example-loop' and feed = 'loop.main' and channel = 'T1'",
        "--day", "2026-03-10", "--format", "json")
    assert code == 0, err
    asked = node.calls[-1][1]
    assert asked["window"] == {"column": "ts", "start": "2026-03-10T00:00:00Z", "end": "2026-03-11T00:00:00Z"}
