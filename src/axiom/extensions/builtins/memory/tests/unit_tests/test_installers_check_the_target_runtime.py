# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An installer refuses a runtime that lacks the verb it will call (#703).

A launchd job was pointed at a service venv that did not have
``memory capture-sweep``; it failed every five minutes for two days. The
installers now probe the TARGET runtime for the exact verb the installed
script or hook will run, refuse when it is missing, and record the runtime
they bound to so node health can compare it with the CLI's later.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from axiom.infra import runtime_record


def _proc(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class TestProbeVerb:
    def test_a_runtime_with_the_verb_passes(self):
        seen = []

        def run(cmd, timeout=30):
            seen.append(cmd)
            return _proc(0, "usage: ...")

        assert runtime_record.probe_verb("/svc/bin/axi", ["memory", "capture-sweep"], run=run) == (
            True,
            "",
        )
        assert seen == [["/svc/bin/axi", "memory", "capture-sweep", "--help"]]

    def test_a_runtime_without_the_verb_says_why(self):
        def run(cmd, timeout=30):
            return _proc(2, err="axi memory: error: invalid choice: 'capture-sweep'")

        ok, why = runtime_record.probe_verb("/svc/bin/axi", ["memory", "capture-sweep"], run=run)
        assert ok is False
        assert "invalid choice" in why

    def test_a_missing_binary_is_a_refusal_not_a_crash(self):
        def run(cmd, timeout=30):
            raise FileNotFoundError(cmd[0])

        ok, why = runtime_record.probe_verb("/gone/axi", ["memory", "hook"], run=run)
        assert ok is False and "FileNotFoundError" in why


class TestRecordedRuntimes:
    def test_record_then_reprobe(self, tmp_path: Path):
        axi = tmp_path / "axi"
        axi.write_text("#!/svc/bin/python3\nimport sys\n")
        ident = {"interpreter": "/svc/bin/python3", "version": "0.70.1", "package_root": "/x"}

        def run(cmd, timeout=30):
            assert cmd[0] == "/svc/bin/python3"
            return _proc(0, json.dumps(ident))

        rec = runtime_record.record_runtime("com.example.job", axi, state_dir=tmp_path, run=run)
        assert rec["version"] == "0.70.1" and rec["axi_binary"] == str(axi)
        probed = runtime_record.probe_recorded_runtimes(state_dir=tmp_path, run=run)
        assert probed == {"com.example.job": runtime_record.RuntimeIdentity(**ident)}

    def test_a_runtime_that_no_longer_imports_is_reported_with_its_error(self, tmp_path: Path):
        (tmp_path / "runtimes").mkdir()
        (tmp_path / "runtimes" / "j.json").write_text(
            json.dumps({"label": "j", "interpreter": "/svc/bin/python3"})
        )

        def run(cmd, timeout=30):
            return _proc(1, err="ModuleNotFoundError: No module named 'axiom'")

        (ident,) = runtime_record.probe_recorded_runtimes(state_dir=tmp_path, run=run).values()
        assert "ModuleNotFoundError" in ident.error

    def test_interpreter_of_reads_the_shebang(self, tmp_path: Path):
        direct = tmp_path / "a"
        direct.write_text("#!/venv/bin/python\n")
        via_env = tmp_path / "b"
        via_env.write_text("#!/usr/bin/env python3\n")
        assert runtime_record.interpreter_of(direct) == "/venv/bin/python"
        assert runtime_record.interpreter_of(via_env) == "python3"
        assert runtime_record.interpreter_of(tmp_path / "missing") == ""


@pytest.fixture
def recorded(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime_record, "record_runtime", lambda label, axi, **k: calls.append((label, str(axi)))
    )
    return calls


def _lacks(verb_word: str):
    def probe(axi, verb, **k):
        if verb_word in verb:
            return False, f"{axi} {' '.join(verb)} --help exited 2: invalid choice: '{verb_word}'"
        return True, ""

    return probe


class TestHookInstall:
    def test_refuses_a_runtime_without_memory_hook_and_leaves_settings_alone(
        self, tmp_path: Path, monkeypatch, recorded
    ):
        from axiom.extensions.builtins.memory.hook_install import install_capture_hook

        monkeypatch.setattr(runtime_record, "probe_verb", _lacks("hook"))
        settings = tmp_path / "settings.json"
        settings.write_text('{"model": "opus"}')
        with pytest.raises(ValueError, match="invalid choice"):
            install_capture_hook(settings_path=settings, axi_path="/svc/bin/axi")
        assert settings.read_text() == '{"model": "opus"}'
        assert recorded == []

    def test_records_the_runtime_it_bound_to(self, tmp_path: Path, monkeypatch, recorded):
        from axiom.extensions.builtins.memory.hook_install import RUNTIME_LABEL, install_capture_hook

        monkeypatch.setattr(runtime_record, "probe_verb", lambda *a, **k: (True, ""))
        install_capture_hook(settings_path=tmp_path / "s.json", axi_path="/svc/bin/axi")
        assert recorded == [(RUNTIME_LABEL, "/svc/bin/axi")]


class TestHeartbeatInstall:
    def test_refuses_a_runtime_without_memory_heartbeat(self, tmp_path: Path, monkeypatch, recorded):
        from axiom.extensions.builtins.memory.heartbeat_install import install_heartbeat_plist

        monkeypatch.setattr(runtime_record, "probe_verb", _lacks("heartbeat"))
        target = tmp_path / "job.plist"
        with pytest.raises(ValueError, match="heartbeat"):
            install_heartbeat_plist(
                axi_binary="/svc/bin/axi", plist_path=target, log_dir=tmp_path, load=False
            )
        assert not target.exists()
        assert recorded == []

    def test_records_the_runtime_it_bound_to(self, tmp_path: Path, monkeypatch, recorded):
        from axiom.extensions.builtins.memory.heartbeat_install import (
            LABEL,
            install_heartbeat_plist,
        )

        monkeypatch.setattr(runtime_record, "probe_verb", lambda *a, **k: (True, ""))
        install_heartbeat_plist(
            axi_binary="/svc/bin/axi", plist_path=tmp_path / "j.plist", log_dir=tmp_path, load=False
        )
        assert recorded == [(LABEL, "/svc/bin/axi")]
