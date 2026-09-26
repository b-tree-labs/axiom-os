# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What `axi mcp status` and `list-tools` report.

The status screen used to be aligned key-value prints ending in a list of
"(N entries)" lines — a count and nothing else, so the next thing anyone did
was run `list-tools` and read all sixteen to find the two they wanted.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.mcp import cli


def _tool(name, description=""):
    return SimpleNamespace(name=name, description=description)


def _surface():
    return SimpleNamespace(
        tools=[
            _tool("axiom_memory__compose", "write"),
            _tool("axiom_expman__experiment_list", "list"),
            _tool("axiom_expman__experiment_show", "show"),
        ],
        resources=[],
        prompts=[],
        content_hash="a" * 64,
        generated_at=SimpleNamespace(isoformat=lambda: ""),
        sources=[
            SimpleNamespace(
                kind="platform",
                name="platform",
                tool_names=["axiom_memory__compose"],
                resource_names=[],
                prompt_names=[],
            ),
            SimpleNamespace(
                kind="extension",
                name="expman",
                tool_names=[
                    "axiom_expman__experiment_list",
                    "axiom_expman__experiment_show",
                ],
                resource_names=[],
                prompt_names=[],
            ),
        ],
    )


@pytest.fixture
def surface(monkeypatch):
    s = _surface()
    monkeypatch.setattr(cli, "_load_or_build_surface", lambda: s)
    monkeypatch.setattr(cli, "_axiom_home", lambda: "/tmp/axiom-home")
    return s


class TestStatusShowsWhatIsProvided:
    def test_it_names_the_tools_not_just_how_many(self, surface, capsys):
        cli._cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "experiment_list" in out and "experiment_show" in out, (
            "a count alone forces a second command to answer the obvious question"
        )

    def test_the_contributor_prefix_is_not_repeated_inside_its_own_row(
        self, surface, capsys
    ):
        """`triga_telemetry__telemetry_metrics` in a row already labelled
        `triga_telemetry` elided to `triga_telemetry_…emetry_metrics`, hiding
        the only half that distinguishes it."""
        cli._cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "expman__experiment_list" not in out
        assert "experiment_list" in out

    def test_it_leads_with_a_verdict(self, surface, capsys):
        cli._cmd_status(argparse.Namespace())
        first = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][0]
        assert "MCP surface" in first and "3 tools" in first

    def test_an_empty_surface_does_not_claim_to_be_serving(self, monkeypatch, capsys):
        empty = _surface()
        empty.tools = []
        empty.sources = []
        monkeypatch.setattr(cli, "_load_or_build_surface", lambda: empty)
        monkeypatch.setattr(cli, "_axiom_home", lambda: "/tmp/axiom-home")
        cli._cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "no tools published" in out

    def test_the_hash_is_shortened_but_still_comparable(self, surface, capsys):
        """Sixty-four characters nobody reads in full; what it is for is
        comparing two, which the head does."""
        from axiom.infra.cli_format import visible_len

        cli._cmd_status(argparse.Namespace())
        out = capsys.readouterr().out

        # Assert on the WIDTH of the rendered cell, not on the absence of a
        # 64-character run. Handing the table the full hash still produces an
        # ellipsis — the table elides any token wider than its column — so
        # "no 64-run" and "contains …" are both true either way and neither
        # can see the difference. What actually differs is how much of the
        # hash survives: two dozen characters, or sixty.
        row = next(ln for ln in out.splitlines() if "hash" in ln)
        cell = row.rstrip().rstrip("│").rsplit("│", 1)[-1].strip()
        assert "…" in cell, f"the hash is not shortened at all: {cell!r}"
        assert visible_len(cell) <= 26, (
            f"the hash cell is {visible_len(cell)} columns wide; it should be "
            f"shortened by the caller, not merely trimmed to fit: {cell!r}"
        )


class TestListToolsCanBeNarrowed:
    def test_it_filters_to_one_contributor(self, surface, capsys):
        rc = cli._cmd_list_tools(argparse.Namespace(source="expman"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "experiment_list" in out and "memory__compose" not in out

    def test_no_filter_still_lists_everything(self, surface, capsys):
        cli._cmd_list_tools(argparse.Namespace(source=None))
        assert len(capsys.readouterr().out.strip().splitlines()) == 3

    def test_an_unknown_contributor_fails_and_names_the_real_ones(
        self, surface, capsys
    ):
        """Silently printing nothing would read as "this contributor has no
        tools" rather than "you typed a name that does not exist"."""
        rc = cli._cmd_list_tools(argparse.Namespace(source="nope"))
        err = capsys.readouterr().err
        assert rc == 1
        assert "expman" in err and "platform" in err
