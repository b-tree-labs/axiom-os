# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi instrument ...`` through the platform's own dispatcher, and what it costs to start.

The dispatcher hands the noun ``sys.argv = ["<cli> instrument", *rest]``; this is the
path a person's command takes, in process. And the noun's help must not drag a
kind's heavy dependencies into the process (prd-instrument §6).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _axi(monkeypatch, tmp_path: Path, *argv: str) -> int:
    from axiom import axiom_cli

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["axi", *argv])
    try:
        rc = axiom_cli.main()
    except SystemExit as exc:
        rc = exc.code
    return rc or 0


def test_axi_instrument_reaches_a_kinds_verb(kinds, gauges, monkeypatch, tmp_path, capsys):
    assert _axi(monkeypatch, tmp_path, "instrument", "gauges", "read", "temp") == 0
    assert "temp 21.5 degC" in capsys.readouterr().out
    assert gauges.calls[-1] == (["read", "temp"], "axi instrument gauges")


def test_axi_instrument_passes_the_kinds_exit_code(kinds, monkeypatch, tmp_path, capsys):
    assert _axi(monkeypatch, tmp_path, "instrument", "gauges", "read", "nope") == 3


def test_axi_instrument_status_runs(kinds, monkeypatch, tmp_path, capsys):
    assert _axi(monkeypatch, tmp_path, "instrument", "status") == 0
    assert "gauges" in capsys.readouterr().out


def test_axi_instrument_refuses_an_unknown_kind(kinds, monkeypatch, tmp_path, capsys):
    assert _axi(monkeypatch, tmp_path, "instrument", "guages") == 2
    assert "Did you mean: axi instrument gauges" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--help"], ["list", "--kinds"]])
def test_the_nouns_own_verbs_import_no_heavy_library(argv, tmp_path):
    code = (
        "import sys\n"
        "from axiom.extensions.builtins.instrument.commands import cli\n"
        "try:\n"
        f"    cli.main({argv!r})\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('HEAVY', sorted(m for m in ('numpy', 'cv2', 'av') if m in sys.modules))\n"
    )
    env = {**os.environ, "AXI_STATE_DIR": str(tmp_path / "state"), "AXI_NO_SOURCE_WARNING": "1"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert "HEAVY []" in out.stdout, out.stdout + out.stderr


def test_python_dash_m_is_the_noun(tmp_path):
    env = {**os.environ, "AXI_STATE_DIR": str(tmp_path / "state"), "AXI_NO_SOURCE_WARNING": "1"}
    out = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.instrument", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "list" in out.stdout and "status" in out.stdout
