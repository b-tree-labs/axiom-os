# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-009: help, Tab and the ``next:`` lines reach a kind's verbs and flags, and know each
verb's skill and risk (spec-instrument, INS-P1 and INS-P2).

The second word after ``instrument`` is a registered name, so the platform's usual
join (``data show`` is ``data.show``) cannot find a kind's skill; the noun exports
``SKILL_FOR_VERB`` instead, the map ADR-243 introduced. And the noun's parser is
assembled from the kinds' parsers, so the Tab cache must notice when one changes.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from types import SimpleNamespace

from axiom import axiom_cli
from axiom.cli import command_space as cs
from axiom.cli import completion_index as ci
from axiom.cli import next_steps
from axiom.extensions.builtins.instrument.commands import cli

MODULE = "axiom.extensions.builtins.instrument.commands"


class _Spec(SimpleNamespace):
    @property
    def effective_risk(self):
        return self.risk


SPECS = {
    "instrument.list": _Spec(inputs={}, values=None, risk="read"),
    "instrument.status": _Spec(inputs={}, values=None, risk="read"),
    "gauges.read": _Spec(inputs={"channel": "str!"}, values=None, risk="read"),
    "gauges.list": _Spec(inputs={}, values=None, risk="read"),
}


def _tree(noun_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The top-level tree the way ``axiom_cli.get_parser`` builds it for one noun."""
    top = argparse.ArgumentParser(prog="axi")
    sub = top.add_subparsers(dest="subcommand")
    node = sub.add_parser("instrument", help="Instruments", description=noun_parser.description)
    axiom_cli._copy_subparsers(noun_parser, node)
    axiom_cli._copy_top_level_args(noun_parser, node)
    return top


def _doc(kinds):
    doc = ci.build_doc(_tree(cli.get_parser()), {})
    return cs.annotate(doc, SPECS, maps={"instrument": dict(cli.SKILL_FOR_VERB)})


def test_the_noun_module_exports_the_parser_builder_and_the_map(kinds):
    mod = importlib.import_module(MODULE)
    assert axiom_cli._parser_builder(mod) is not None
    assert mod.SKILL_FOR_VERB == {
        "list": "instrument.list",
        "status": "instrument.status",
        "gauges read": "gauges.read",
        "gauges list": "gauges.list",
    }


def test_the_tree_reaches_a_kinds_verbs_and_flags(kinds):
    tree = _doc(kinds)["commands"][0]["tree"]
    read = tree["subs"]["gauges"]["subs"]["read"]
    names = {n for a in read["args"] for n in a["names"]}
    assert {"channel", "--unit"} <= names
    unit = next(a for a in read["args"] if "--unit" in a["names"])
    assert unit["choices"] == ["degC", "W"]
    assert {"list", "status", "gauges"} <= set(tree["subs"])


def test_a_kinds_verbs_get_their_skill_and_risk(kinds):
    tree = _doc(kinds)["commands"][0]["tree"]
    read = tree["subs"]["gauges"]["subs"]["read"]
    assert read["skill"] == "gauges.read" and read["risk"] == "read"
    assert tree["subs"]["status"]["skill"] == "instrument.status"


def test_without_the_map_the_join_finds_nothing(kinds):
    """The guard can fail: this is what every kind verb got before INS-P1."""
    doc = cs.annotate(ci.build_doc(_tree(cli.get_parser()), {}), SPECS)
    read = doc["commands"][0]["tree"]["subs"]["gauges"]["subs"]["read"]
    assert "skill" not in read


def test_next_lines_offer_a_kinds_reads(kinds):
    space = cs.from_doc(_doc(kinds))
    lines = next_steps.suggest(("instrument",), space=space, cache=None, recent=[], limit=5)
    assert any(line.startswith("axi instrument gauges list") for line in lines), lines


def test_the_map_is_collected_from_noun_modules_already_imported(kinds):
    importlib.import_module(MODULE)
    maps = axiom_cli.exported_skill_maps({"instrument": {"module": MODULE}, "ghost": {"module": "x.y"}})
    assert maps["instrument"]["gauges read"] == "gauges.read"
    assert "ghost" not in maps


def test_the_parser_sources_cover_the_noun_and_each_kind(kinds):
    sources = cli.parser_sources()
    assert any(s.endswith(os.path.join("instrument", "commands", "cli.py")) for s in sources)
    assert any(s.endswith(os.path.join("instrument", "tests", "fakes.py")) for s in sources)
    assert all(os.path.isabs(s) for s in sources)


def test_a_stored_tree_whose_sources_changed_is_a_miss(tmp_path):
    src = tmp_path / "kind_cli.py"
    src.write_text("x = 1\n")
    stored = {"commands": [], "sources": ci.source_tokens([str(src)])}
    assert ci.sources_unchanged(stored)
    src.write_text("x = 22222\n")
    assert not ci.sources_unchanged(stored)
    assert ci.sources_unchanged({"commands": []})  # a tree that recorded none


def test_completion_doc_rebuilds_a_tree_whose_sources_changed(tmp_path, monkeypatch):
    from axiom.extensions import index_cache

    src = tmp_path / "kind_cli.py"
    src.write_text("x = 1\n")
    stored = {"commands": [], "annotated": True, "sources": ci.source_tokens([str(src)])}
    monkeypatch.setattr(index_cache, "load", lambda kind, key: stored)
    assert axiom_cli.completion_doc(build=False) is stored
    src.write_text("x = 22222\n")
    assert axiom_cli.completion_doc(build=False) is None


def test_installing_a_package_changes_the_tree_fingerprint():
    """An entry-point kind ships in a distribution, and installing one changes no manifest
    the fingerprint already covers; the installed set must be part of it."""
    import sysconfig

    parts = ci.fingerprint_parts({}, {}, axiom_cli.__file__)
    for key in ("purelib", "platlib"):
        where = sysconfig.get_paths()[key]
        assert any(p.startswith(where + ":") for p in parts), key


def test_completing_through_the_real_top_level_tree(kinds, monkeypatch):
    # The noun the person typed is the one parser built (axiom_cli._parser_wanted_for).
    monkeypatch.setattr(sys, "argv", ["axi", "instrument"])
    top = axiom_cli.get_parser(gate=False)
    sub = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    noun = sub.choices["instrument"]
    kinds_action = next(a for a in noun._actions if isinstance(a, argparse._SubParsersAction))
    assert "gauges" in kinds_action.choices
    read = next(
        a for a in kinds_action.choices["gauges"]._actions if isinstance(a, argparse._SubParsersAction)
    ).choices["read"]
    assert any("--unit" in a.option_strings for a in read._actions)
