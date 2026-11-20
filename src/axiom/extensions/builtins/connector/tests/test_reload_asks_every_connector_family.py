# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``connector reload [<name>]`` applies changed configuration for every family, not only storage.

ADR-220 §3: reload applies a change without a restart and reports each
connector's health. It knew one family (storage), so ``reload <name>`` for any
other connector said "no storage connector named ...". A family now answers
reload the way it answers status: a builtin's ``connector_status.reload(state_dir,
name)`` by convention, or an ``axiom.connector_reload`` entry point from a
portfolio distribution (ADR-023-A1). The connector extension still names no kind.
``ok`` is False when a named connector is unknown to everyone, or any row is
``failed``; a ``degraded`` row still serves its previous configuration and says why.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.connector import configured
from axiom.extensions.builtins.connector.configured import reload_configured


def _row(name, kind="gizmo", health="ready", detail="applied"):
    return {"connector": name, "kind": kind, "health": health, "detail": detail}


def test_every_reloader_is_asked_with_the_state_dir_and_the_name(tmp_path):
    seen = []

    def fam(state_dir, name):
        seen.append((state_dir, name))
        return [_row("cam-1")] if name in (None, "cam-1") else []

    rows, problems = reload_configured(tmp_path, "cam-1", reloaders=[("fam", fam)])
    assert seen == [(tmp_path, "cam-1")]
    assert [(r["connector"], r["family"]) for r in rows] == [("cam-1", "fam")]
    assert problems == []


def test_a_reloader_that_fails_is_reported_and_hides_nothing(tmp_path):
    def broken(state_dir, name):
        raise RuntimeError("cannot read its files")

    rows, problems = reload_configured(
        tmp_path, None, reloaders=[("broken", broken), ("ok", lambda s, n: [_row("x")])]
    )
    assert [r["connector"] for r in rows] == ["x"]
    assert problems == ["broken: RuntimeError: cannot read its files"]


def test_a_health_outside_the_vocabulary_is_refused(tmp_path):
    rows, problems = reload_configured(
        tmp_path, None, reloaders=[("odd", lambda s, n: [_row("x", health="fine")])]
    )
    assert rows == [] and "fine" in problems[0]


def test_builtin_reloaders_are_found_by_convention():
    found = dict(configured.reloaders())
    assert "video" in found, "the video family answers reload"
    for name, fn in found.items():
        assert fn.__module__ == f"{configured.BUILTINS_PACKAGE}.{name}.{configured.MODULE}"
        assert fn.__name__ == configured.RELOAD_FUNCTION


def test_a_reload_entry_point_from_outside_the_portfolio_is_refused(monkeypatch, caplog):
    loaded = []
    ep = SimpleNamespace(
        name="stranger",
        dist=SimpleNamespace(name="not-ours"),
        load=lambda: loaded.append(1) or (lambda s, n: []),
    )
    monkeypatch.setattr(configured, "_reload_entry_points", lambda: [ep])
    with caplog.at_level(logging.WARNING):
        names = [n for n, _ in configured.reloaders(allow=frozenset({"axiom-os-lm"}))]
    assert configured.RELOAD_GROUP == "axiom.connector_reload"
    assert "stranger" not in names and loaded == []
    assert "not-ours" in caplog.text


class _NoStorage:
    def reload(self, name=None):
        return {}

    def role_status(self):
        return {}


@pytest.fixture
def reload_skill(monkeypatch, tmp_path):
    from axiom.extensions.builtins.connector.skills import reload

    answers = {
        "cam-1": _row("cam-1"),
        "cam-2": _row("cam-2", health="degraded", detail="kept the previous"),
    }

    def fam(state_dir, name):
        return [answers[name]] if name in answers else ([] if name else list(answers.values()))

    monkeypatch.setattr(configured, "reloaders", lambda **kw: [("fam", fam)])
    ctx = SimpleNamespace(state_dir=tmp_path)

    def run(**params):
        return reload.run({"manager": _NoStorage(), "no_stamp": True, **params}, ctx)

    run.answers = answers
    return run


def test_reload_of_a_family_connector_is_not_an_unknown_storage_connector(reload_skill):
    result = reload_skill(connector="cam-1")
    assert result.ok, result.errors
    assert [r["connector"] for r in result.value["configured"]] == ["cam-1"]


def test_reload_without_a_name_reloads_every_family_connector(reload_skill):
    result = reload_skill()
    assert [r["connector"] for r in result.value["configured"]] == ["cam-1", "cam-2"]
    assert result.ok, "a degraded connector still serves its previous configuration"


def test_reload_of_a_name_nobody_has_is_an_error(reload_skill):
    result = reload_skill(connector="ghost")
    assert not result.ok and any("ghost" in e for e in result.errors)


def test_a_failed_family_connector_fails_the_reload_and_says_why(reload_skill):
    reload_skill.answers["cam-1"] = _row("cam-1", health="failed", detail="motoin is not a trigger")
    result = reload_skill(connector="cam-1")
    assert not result.ok and any("motoin" in e for e in result.errors)


def test_the_cli_prints_the_family_rows(reload_skill, monkeypatch, tmp_path, capsys):
    from axiom.extensions.builtins.connector import cli
    from axiom.extensions.builtins.connector.storage import manager

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(manager, "StorageConnectorManager", _NoStorage)
    assert cli.main(["reload", "cam-2"]) == 0
    out = capsys.readouterr().out
    assert "cam-2" in out and "kept the previous" in out
    assert "no storage connectors configured" not in out


class _StorageThatAnswersAnyName:
    """Like the real manager: it has what is configured, and reports any name it is asked
    to reload as unmounted, "no longer configured"."""

    def __init__(self):
        self.asked = []

    def names(self):
        return ["share"]

    def reload(self, name=None):
        self.asked.append(name)
        row = SimpleNamespace(name=name, health=None, reason="no longer configured")
        row.as_dict = lambda: {
            "name": name,
            "kind": "local",
            "health": "unmounted",
            "reason": "no longer configured",
        }
        return {name: row}

    def role_status(self):
        return {}


def test_a_name_no_one_has_is_an_error_even_though_storage_would_answer_it(
    reload_skill, monkeypatch, tmp_path
):
    from axiom.extensions.builtins.connector.skills import reload

    storage = _StorageThatAnswersAnyName()
    result = reload.run(
        {"manager": storage, "no_stamp": True, "connector": "cma-1"},
        SimpleNamespace(state_dir=tmp_path),
    )
    assert not result.ok and any("cma-1" in e for e in result.errors)
    assert storage.asked == [], "storage was never configured with it, so it is not asked"
    assert result.value["connectors"] == []
