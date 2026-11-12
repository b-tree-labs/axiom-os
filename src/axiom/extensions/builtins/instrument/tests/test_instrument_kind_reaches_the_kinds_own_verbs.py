# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-004 and INS-005: ``<cli> instrument <kind> <verb> ...`` is the kind's, and an
unknown kind is refused with what is registered and the command the person typed."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.instrument.commands import cli


def test_the_rest_of_the_command_line_and_the_full_prog_reach_the_kind(kinds, gauges, capsys):
    assert cli.main(["gauges", "read", "temp", "--unit", "degC"]) == 0
    assert gauges.calls == [(["read", "temp", "--unit", "degC"], "axi instrument gauges")]
    assert "temp 21.5 degC" in capsys.readouterr().out


def test_the_kinds_exit_code_is_the_commands(kinds, capsys):
    assert cli.main(["gauges", "read", "pressure"]) == 3
    assert "axi instrument gauges list" in capsys.readouterr().out


def test_a_kinds_usage_names_the_full_command(kinds, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["gauges", "--help"])
    assert exc.value.code == 0
    assert "usage: axi instrument gauges" in capsys.readouterr().out


def test_under_a_brand_the_kind_is_given_the_brands_command(kinds, gauges, acme):
    assert cli.main(["gauges", "list"]) == 0
    assert gauges.calls[-1][1] == "acme instrument gauges"


def test_an_unknown_kind_exits_2_with_the_kinds_and_the_nearest(kinds, capsys):
    assert cli.main(["guages", "read", "temp"]) == 2
    err = capsys.readouterr().err
    assert "no instrument kind named 'guages'" in err
    assert "registered: gauges" in err
    assert "Did you mean: axi instrument gauges" in err
    assert "axi instrument list --kinds" in err


def test_an_unknown_kind_names_the_brands_command(kinds, capsys, acme):
    assert cli.main(["guages"]) == 2
    err = capsys.readouterr().err
    assert "acme instrument gauges" in err and "axi " not in err


def test_with_no_kinds_the_refusal_says_so(monkeypatch, capsys):
    from axiom.extensions.builtins.instrument import registry

    empty = registry.KindCatalog()
    monkeypatch.setattr(registry, "catalog", lambda *, refresh=False: empty)
    assert cli.main(["video", "status"]) == 2
    assert "registered: none" in capsys.readouterr().err


def test_nothing_or_help_prints_the_verbs_and_the_kinds(kinds, capsys):
    assert cli.main([]) == 2
    out = capsys.readouterr().out
    assert "list" in out and "status" in out and "gauges" in out
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: axi instrument" in out and "gauges" in out


def test_an_option_where_a_kind_belongs_is_a_usage_error(kinds, capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "usage:" in capsys.readouterr().err
