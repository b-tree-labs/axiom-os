# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gold read verbs, on the CLI, in formats a script can use.

They existed as skills — so MCP had them and a terminal did not. A researcher
who wanted yesterday's readings had to press Export in a browser, or write
Python against a DSN nobody gave them.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from axiom.extensions.builtins.data_platform import cli
from axiom.infra.skills import SkillResult


def _envelope(series, unit=None, note=None):
    prov = {"source": "gold.signals", "method": "avg(value) by 1 hour", "rows": len(series)}
    if note:
        prov["note"] = note
    value = {"data": {"series": series} if series else None, "provenance": prov}
    if unit:
        value["unit"] = {"value": unit}
    return SkillResult(ok=True, value=value)


class TestEveryGoldSkillIsReachable:
    """A verb that exists only as a skill is a verb a shell cannot use."""

    def test_the_six_read_verbs_resolve_to_their_skills(self):
        assert cli._GOLD_VERBS == {
            "tables": "catalog",
            "describe": "describe",
            "series": "series",
            "aggregate": "aggregate",
            "roles": "roles",
            "compare": "compare",
        }

    @pytest.mark.parametrize("verb", sorted(cli._GOLD_VERBS))
    def test_each_one_names_a_skill_that_exists(self, verb):
        """A mapping onto a renamed skill resolves cleanly and fails at run time."""
        from .. import skills

        assert cli._GOLD_VERBS[verb] in skills.verbs()

    @pytest.mark.parametrize("verb", sorted(cli._GOLD_VERBS))
    def test_the_parser_accepts_each_one(self, verb):
        # Parsing is what proves the subcommand exists; resolution is what
        # proves it reaches a skill rather than a name nobody implements.
        import argparse

        args = argparse.Namespace(verb=verb, dry_run=False)
        skill, err = cli.resolve_skill(args)
        assert err is None
        assert skill == cli._GOLD_VERBS[verb]

    def test_the_explicit_spelling_reaches_the_same_skill(self):
        import argparse

        args = argparse.Namespace(verb="gold-series", dry_run=False)
        assert cli.resolve_skill(args)[0] == "series"


class TestUnitsTravelWithValues:
    """A column called `value` and not `value_degC` is the bug this platform
    spent a day undoing. It must not be reintroduced by an exporter."""

    def test_csv_names_the_unit_in_the_header(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli._write_records(
                _envelope([{"t": "2026-09-10T00:00:00Z", "value": 1.5}], unit="W"), "csv"
            )
        assert code == 0
        assert out.getvalue().splitlines()[0] == "t,value_W"

    def test_ndjson_carries_it_on_every_record(self):
        # A stream has no header to hold it.
        out = io.StringIO()
        with redirect_stdout(out):
            cli._write_records(
                _envelope(
                    [{"t": "a", "value": 1.0}, {"t": "b", "value": 2.0}], unit="degC"
                ),
                "ndjson",
            )
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        assert [r["unit"] for r in rows] == ["degC", "degC"]

    def test_an_undeclared_unit_is_not_invented(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli._write_records(_envelope([{"t": "a", "value": 1.0}]), "csv")
        assert out.getvalue().splitlines()[0] == "t,value"


class TestARefusalIsNotAnEmptyFile:
    """`axi data series … > day.csv && process day.csv` must not process an
    empty file as though the pull had worked."""

    def test_nothing_is_written_and_the_exit_is_non_zero(self, capsys):
        code = cli._write_records(_envelope([], note="no rows matched"), "csv")
        assert code == 1
        assert capsys.readouterr().out == ""

    def test_the_reason_is_the_envelope_s_own_words(self, capsys):
        # It lives under `provenance`, and reading it off the top level printed
        # "no rows" while throwing away the sentence that says what to do.
        spans = (
            "this answer spans 5 units (W, console_units, degC, mol, pct) and rows "
            "declaring none … Narrow the filter, or pass allow_mixed_units"
        )
        cli._write_records(_envelope([], note=spans), "csv")
        assert "allow_mixed_units" in capsys.readouterr().err

    def test_a_failed_skill_reports_its_errors(self, capsys):
        code = cli._write_records(SkillResult(ok=False, errors=["no database"]), "csv")
        assert code != 0
        assert "no database" in capsys.readouterr().err


class TestTheGuardIsNotAFlag:
    """`--tiers` is honoured only for an assured principal. The CLI must not
    become the way around a guard the skill enforces."""

    def test_the_cli_passes_tiers_through_rather_than_applying_them(self):
        import argparse

        args = argparse.Namespace(
            verb="series", json=False, kind=None, dry_run=False,
            tiers=["restricted"], format="csv", table="signals", column="value",
            time_column="ts", bucket="1 hour", start=None, end=None,
        )
        params = cli._args_to_params(args)
        assert params["tiers"] == ["restricted"]
        # …and `format` is the CLI's business, never the skill's.
        assert "format" not in params


class TestTheWindowIsTwoFlagsAndOneParameter:
    def test_from_and_to_become_a_window_on_the_time_column(self):
        import argparse

        args = argparse.Namespace(
            verb="series", json=False, kind=None, dry_run=False, format="csv",
            table="signals", column="value", time_column="ts", bucket="1 hour",
            start="2026-09-10T00:00:00+00:00", end="2026-09-11T00:00:00+00:00",
        )
        params = cli._args_to_params(args)
        assert params["window"] == {
            "column": "ts",
            "start": "2026-09-10T00:00:00+00:00",
            "end": "2026-09-11T00:00:00+00:00",
        }

    def test_no_window_when_neither_end_is_given(self):
        import argparse

        args = argparse.Namespace(
            verb="series", json=False, kind=None, dry_run=False, format="text",
            table="signals", column="value", time_column="ts", bucket="1 hour",
            start=None, end=None,
        )
        assert "window" not in cli._args_to_params(args)


class TestFormatMeansDifferentThingsToDifferentVerbs:
    """`--format` is not this feature's word to take.

    `axi data register … http-tabular --format csv` is the tabular SOURCE's file
    format and predates the gold verbs. Reading the flag globally at dispatch
    sent a register result through the records writer, which printed nothing and
    exited 1 — two subprocess tests caught it, and they were right.
    """

    def test_only_a_gold_verb_writes_records(self):
        import argparse

        for verb in sorted(cli._GOLD_VERBS):
            args = argparse.Namespace(verb=verb, format="csv", json=False)
            assert cli._writes_records(args) is True
        for verb in ("register", "ingest", "install"):
            args = argparse.Namespace(verb=verb, format="csv", json=False)
            assert cli._writes_records(args) is False
