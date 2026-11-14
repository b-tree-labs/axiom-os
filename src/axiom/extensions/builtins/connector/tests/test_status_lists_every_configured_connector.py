# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``connector status`` lists the configured connectors of every family, not only delivery outcomes.

Before this, ``status`` read the in-process store of delivery outcomes, which a
fresh CLI process always finds empty, so a node with configured connectors was
told "(no connector outcomes recorded yet)".

A family reports its configured connectors through a status provider: a builtin
extension's ``connector_status`` module (found by convention, so nothing needs
reinstalling), or an ``axiom.connector_status`` entry point from a portfolio
distribution (ADR-023-A1: loading is execution). The connector extension names
no kind (ADR-220 §1). One provider failing is reported and hides nothing else.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.connector import configured
from axiom.extensions.builtins.connector.configured import HEALTH, configured_connectors


def _row(name, kind="widget", health="ready", detail=""):
    return {"connector": name, "kind": kind, "health": health, "detail": detail}


def test_rows_from_every_provider_sorted_by_connector(tmp_path):
    rows, problems = configured_connectors(
        tmp_path,
        providers=[
            ("b", lambda state_dir: [_row("zeta"), _row("alpha", kind="gizmo")]),
            ("a", lambda state_dir: [_row("mid", health="degraded", detail="reconnecting")]),
        ],
    )
    assert [r["connector"] for r in rows] == ["alpha", "mid", "zeta"]
    assert problems == []
    assert {r["family"] for r in rows} == {"a", "b"}


def test_a_provider_that_fails_is_reported_and_hides_nothing(tmp_path):
    def broken(state_dir):
        raise RuntimeError("its store is unreadable")

    rows, problems = configured_connectors(
        tmp_path, providers=[("broken", broken), ("ok", lambda s: [_row("x")])]
    )
    assert [r["connector"] for r in rows] == ["x"]
    assert problems == ["broken: RuntimeError: its store is unreadable"]


def test_a_health_outside_the_vocabulary_is_refused_not_shown(tmp_path):
    rows, problems = configured_connectors(
        tmp_path, providers=[("odd", lambda s: [_row("x", health="splendid")])]
    )
    assert rows == [] and "splendid" in problems[0]
    assert HEALTH == ("ready", "degraded", "failed", "unmounted")


def test_the_provider_gets_the_state_dir(tmp_path):
    seen = []
    configured_connectors(tmp_path, providers=[("p", lambda s: seen.append(s) or [])])
    assert seen == [tmp_path]


def test_builtin_providers_are_found_by_convention():
    """``<builtins>.<extension>.connector_status:rows``, named by the extension it lives in."""
    found = configured.providers()
    assert found, "the scan found no provider, so it proves nothing"
    for name, fn in found:
        assert fn.__module__ == f"{configured.BUILTINS_PACKAGE}.{name}.{configured.MODULE}"
        assert fn.__name__ == configured.FUNCTION


def test_an_entry_point_from_outside_the_portfolio_is_refused(monkeypatch, caplog):
    loaded = []
    ep = SimpleNamespace(
        name="stranger",
        dist=SimpleNamespace(name="not-ours"),
        load=lambda: loaded.append(1) or (lambda s: []),
    )
    monkeypatch.setattr(configured, "_entry_points", lambda: [ep])
    with caplog.at_level(logging.WARNING):
        names = [n for n, _ in configured.providers(allow=frozenset({"axiom-os-lm"}))]
    assert "stranger" not in names and loaded == [], "never imported"
    assert "not-ours" in caplog.text


def test_an_entry_point_from_the_portfolio_is_used(monkeypatch):
    ep = SimpleNamespace(
        name="partner", dist=SimpleNamespace(name="axiom-os-lm"), load=lambda: lambda s: [_row("p")]
    )
    monkeypatch.setattr(configured, "_entry_points", lambda: [ep])
    assert "partner" in [n for n, _ in configured.providers(allow=frozenset({"axiom-os-lm"}))]


@pytest.fixture
def status_skill(monkeypatch, tmp_path):
    from axiom.extensions.builtins.connector.skills import status
    from axiom.extensions.builtins.connector.status_store import InMemoryStatusStore

    monkeypatch.setattr(
        configured,
        "providers",
        lambda **kw: [("fam", lambda s: [_row("cam-1", health="degraded", detail="reconnecting")])],
    )
    ctx = SimpleNamespace(state_dir=tmp_path)
    return lambda **params: status.run({"store": InMemoryStatusStore(), **params}, ctx)


def test_status_includes_the_configured_connectors(status_skill):
    value = status_skill().value
    assert [r["connector"] for r in value["configured"]] == ["cam-1"]
    assert value["configured_problems"] == []


def test_status_of_one_connector_finds_a_configured_one(status_skill):
    value = status_skill(connector="cam-1").value
    assert value["found"] is True and value["configured"]["health"] == "degraded"


def test_the_cli_shows_configured_connectors_when_no_outcome_is_recorded(
    monkeypatch, capsys, tmp_path
):
    from axiom.extensions.builtins.connector import cli

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        configured,
        "providers",
        lambda **kw: [("fam", lambda s: [_row("cam-1", kind="gizmo", detail="running, 5 fps")])],
    )
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "cam-1" in out and "gizmo" in out and "running, 5 fps" in out
    assert "no connector outcomes recorded yet" not in out

    assert cli.main(["--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["value"]["configured"][0]["connector"] == "cam-1"
