# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Early reads stay: a node with no database switches extensions through the node
file alone, and discovery honours it (ADR-254 §6)."""

from __future__ import annotations

import contextlib

from axiom.extensions.builtins.features import cli
from axiom.extensions.builtins.flags import store
from axiom.infra import node_functions as nf
from axiom.infra import optional_extensions as oe


@contextlib.contextmanager
def _no_database():
    raise RuntimeError("no database on this node")
    yield  # pragma: no cover


def test_the_switch_is_written_and_read_with_no_database(switchboard, capsys):
    store.set_provider(_no_database)
    assert cli.main(["enable", "widgets"]) == 0
    assert "record unavailable" in capsys.readouterr().out
    cfg = nf.load()
    assert "widgets" in cfg.features
    assert oe.is_enabled("widgets", oe.optional_blocks()["widgets"], cfg=cfg)
    assert "widgets" not in oe.switched_off(cfg=cfg)

    from axiom.extensions.discovery import _switched_on

    commands = {"widget": {"extension": "widgets", "optional": {"default": "off"}}, "chat": {"extension": "chat"}}
    assert set(_switched_on(commands)) == {"widget", "chat"}
    assert cli.main(["disable", "widgets"]) == 0
    assert set(_switched_on(commands)) == {"chat"} and "widgets" in nf.load().off
    assert cli.main(["--json"]) == 0  # listing works without a database too


def test_a_confined_node_without_database_functions_is_never_asked_for_one(switchboard, monkeypatch):
    calls = []

    @contextlib.contextmanager
    def counting():
        calls.append(1)
        raise RuntimeError("should not be called")
        yield  # pragma: no cover

    switchboard["node"].write_text('[node]\nrole = "collector"\nfunctions = ["acquire", "transmit"]\nfeatures = []\n')
    store.set_provider(counting)
    assert cli.main(["enable", "widgets"]) == 0
    assert calls == [] and "widgets" in nf.load().features


def test_the_resolver_reads_the_node_file_fresh_on_every_call(switchboard):
    from axiom.extensions.builtins.features.resolver import FlagService

    svc = FlagService()
    assert svc.resolve_flag("panel") == "off"
    switchboard["node"].write_text('[node]\nfunctions = []\nfeatures = ["widgets"]\n')
    assert svc.resolve_flag("panel") == "on"
