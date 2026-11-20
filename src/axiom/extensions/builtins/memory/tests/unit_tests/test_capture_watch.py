# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The capture watchdog can say "I could not measure" (#703, class 2).

A launchd script ran ``capture-sweep`` through a runtime that did not have the
verb, read ``invalid choice`` as unparseable output, mapped that to STALE and
alerted every five minutes for two days: 754 identical alerts about a ledger
that was fine. Two rules fix the class:

- only a schema-versioned sweep result can say OK or STALE; anything else is
  UNKNOWN (the tooling is broken), with its own text;
- alert on transitions: once on entering a bad state, once on recovery, and a
  daily reminder while it persists.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.memory.skills.capture_sweep import SWEEP_SCHEMA
from axiom.extensions.builtins.memory.skills.capture_watch import (
    capture_watch,
    classify_sweep,
    decide_alert,
)

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _sweep(ok: bool, age: float | None = 1.0, **extra) -> str:
    return json.dumps({"schema": SWEEP_SCHEMA, "ok": ok, "ledger_age_hours": age, **extra})


class TestClassify:
    def test_a_fresh_sweep_is_ok(self):
        assert classify_sweep(_sweep(True), 0)[0] == "OK"

    def test_a_stale_ledger_is_stale(self):
        state, detail = classify_sweep(_sweep(False, age=30.0, errors=["ledger is stale"]), 1)
        assert state == "STALE"
        assert "30.0h" in detail

    def test_invalid_choice_is_unknown_not_stale(self):
        """The 754-alert case: a runtime without the verb."""
        out = "usage: axi memory ...\naxi memory: error: invalid choice: 'capture-sweep'"
        state, detail = classify_sweep(out, 2)
        assert state == "UNKNOWN"
        assert "runtime/contract drift" in detail

    def test_json_without_the_schema_is_unknown(self):
        assert classify_sweep(json.dumps({"ok": False}), 1)[0] == "UNKNOWN"

    def test_a_newer_schema_major_is_unknown(self):
        out = json.dumps({"schema": "axiom.memory.capture-sweep/2", "ok": True})
        assert classify_sweep(out, 0)[0] == "UNKNOWN"


class TestDecideAlert:
    def test_first_bad_state_alerts_once(self):
        alert, rec = decide_alert(None, "UNKNOWN", "broken", now=T0)
        assert alert and alert.startswith("UNKNOWN")
        again, _ = decide_alert(rec, "UNKNOWN", "broken", now=T0 + timedelta(minutes=5))
        assert again is None

    def test_754_ticks_of_the_same_state_give_one_alert_and_daily_reminders(self):
        rec, alerts = None, []
        for i in range(754):  # two and a half days at 5 minutes
            alert, rec = decide_alert(rec, "UNKNOWN", "broken", now=T0 + timedelta(minutes=5 * i))
            if alert:
                alerts.append(alert)
        assert len(alerts) == 3  # enter, +24h, +48h
        assert "still" in alerts[1]

    def test_recovery_alerts_once(self):
        _, rec = decide_alert(None, "STALE", "old", now=T0)
        alert, rec = decide_alert(rec, "OK", "", now=T0 + timedelta(hours=1))
        assert alert and "recovered" in alert
        alert, _ = decide_alert(rec, "OK", "", now=T0 + timedelta(hours=2))
        assert alert is None

    def test_ok_from_the_start_is_silent(self):
        assert decide_alert(None, "OK", "", now=T0)[0] is None

    def test_stale_to_unknown_is_a_new_alert(self):
        _, rec = decide_alert(None, "STALE", "old", now=T0)
        alert, _ = decide_alert(rec, "UNKNOWN", "broken", now=T0 + timedelta(minutes=5))
        assert alert and alert.startswith("UNKNOWN")


class TestCaptureWatchSkill:
    def test_runs_the_target_runtime_and_alerts_once(self, tmp_path):
        sent: list[str] = []
        calls: list[list[str]] = []

        def run(cmd, timeout=600):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 2, "", "error: invalid choice: 'capture-sweep'")

        params = {
            "axi": "/svc/bin/axi",
            "state_path": tmp_path / "watch.json",
            "run": run,
            "alert": sent.append,
            "now": T0,
        }
        first = capture_watch(params, None)
        second = capture_watch({**params, "now": T0 + timedelta(minutes=5)}, None)
        assert calls[0][:3] == ["/svc/bin/axi", "memory", "capture-sweep"]
        assert "--json" in calls[0]
        assert first.value["state"] == "UNKNOWN" and second.value["state"] == "UNKNOWN"
        assert len(sent) == 1
        assert first.ok is False

    def test_a_runtime_that_cannot_start_is_unknown(self, tmp_path):
        def run(cmd, timeout=600):
            raise FileNotFoundError(cmd[0])

        res = capture_watch(
            {"axi": "/gone/axi", "state_path": tmp_path / "w.json", "run": run,
             "alert": lambda _t: None, "now": T0},
            None,
        )
        assert res.value["state"] == "UNKNOWN"


def test_the_cli_falls_back_to_the_axi_beside_this_interpreter(monkeypatch, tmp_path, capsys):
    """With no --axi and no CLI on PATH, the command still names a runtime."""
    import argparse
    import shutil
    import sys
    from pathlib import Path

    from axiom.extensions.builtins.memory import cli
    from axiom.extensions.builtins.memory.skills import capture_watch as watch_mod
    from axiom.infra.skills import SkillResult

    seen: dict = {}

    def fake_watch(params, ctx):
        seen.update(params)
        return SkillResult(ok=True, value={"state": watch_mod.OK})

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(watch_mod, "capture_watch", fake_watch)
    args = argparse.Namespace(
        axi=None, recipients=None, state_file=str(tmp_path / "w.json"), json=False
    )

    assert cli._cmd_capture_watch(args) == 0
    assert seen["axi"] == str(Path(sys.executable).parent / "axi")
