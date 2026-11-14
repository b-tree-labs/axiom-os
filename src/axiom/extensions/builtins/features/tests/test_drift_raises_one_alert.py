# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drift between the recorded switch and the node file raises one alert (ADR-254 §6).

The owner's decision of 2026-11-12: drift stays report-only (nothing is
reconciled in either direction), and it also raises ONE alert through the
platform's one notification path (``notifications.send`` with a cause, ADR-249
and ADR-252), so it does not depend on someone reading ``features list``. The
same drift read again raises nothing more; a drift that is fixed and comes back
is raised again.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.features import cli
from axiom.extensions.builtins.features.tests.conftest import mcp
from axiom.extensions.builtins.notifications.incidents.stage import OPEN_STATES

DRIFT_KIND = "switch_drift"


def _drifted(switchboard) -> None:
    assert cli.main(["enable", "widgets"]) == 0
    switchboard["node"].write_text('[node]\nfunctions = []\nfeatures = []\n')  # hand-edited back off


def _list(capsys) -> dict:
    capsys.readouterr()
    assert cli.main(["--json"]) == 0
    return json.loads(capsys.readouterr().out)


def _drift_incidents(switchboard) -> list:
    return [i for i in switchboard["incidents"].in_states(OPEN_STATES) if i.kind == DRIFT_KIND]


def test_drift_raises_one_alert_however_often_it_is_read(switchboard, capsys):
    _drifted(switchboard)
    for _ in range(3):
        assert _list(capsys)["drift"]
    assert mcp("axiom_features__list", {}, switchboard["state"])["ok"]

    found = _drift_incidents(switchboard)
    assert len(found) == 1, found
    inc = found[0]
    assert inc.subjects == ["extension:widgets"]
    assert inc.occurrences == 1, "the same drift read four times is one alert, not four"
    assert inc.owner_agent == "@keel"
    assert inc.unmet and "features enable widgets" in inc.unmet[0].fix
    assert switchboard["inbox"].all() == [], "held by the incident stage, delivered when it releases"


def test_the_alert_reconciles_nothing(switchboard, capsys):
    _drifted(switchboard)
    _list(capsys)
    assert "widgets" not in switchboard["node"].read_text(), "the node file is left as it is"
    rows = {r["key"]: r["value"] for r in switchboard["store"].rows()}
    assert rows["extension:widgets"] == "on", "the record is left as it is"


def test_no_drift_raises_no_alert(switchboard, capsys):
    assert cli.main(["enable", "widgets"]) == 0
    assert _list(capsys)["drift"] == []
    assert _drift_incidents(switchboard) == []


def test_a_drift_that_is_fixed_and_comes_back_is_raised_again(switchboard, capsys):
    _drifted(switchboard)
    _list(capsys)
    assert cli.main(["enable", "widgets"]) == 0  # the fix the note names: they agree again
    assert _list(capsys)["drift"] == []
    switchboard["node"].write_text('[node]\nfunctions = []\nfeatures = []\n')
    _list(capsys)
    (inc,) = _drift_incidents(switchboard)
    assert inc.occurrences == 2


def test_a_broken_alert_path_never_hides_the_listing(switchboard, capsys, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("alert path down")

    import importlib

    monkeypatch.setattr(importlib.import_module("axiom.extensions.builtins.notifications.send"), "send", boom)
    _drifted(switchboard)
    assert _list(capsys)["drift"]
