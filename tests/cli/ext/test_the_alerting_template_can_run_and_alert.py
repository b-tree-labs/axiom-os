# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The alerting scaffold is a monitor that can run and alert (#1162).

It called ``client.latest()`` on a client nobody supplied, compared to a
threshold with no unit, and delivered nothing ("wired at promotion"). Now the
threshold carries a unit, a reading in another unit or no reading at all is
``not_checked`` (never "all clear"), it reads through ``data.aggregate`` and
delivers through ``notifications.alert`` with a stated classification, and
its own generated tests pass as written.
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.provider import CliContext


def _scaffold(tmp_path: Path) -> Path:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["watch_it", "--template", "alerting", "--dir", str(tmp_path)])
    assert provider.run(args, CliContext(cwd=tmp_path)) == 0
    return tmp_path / "watch_it"


def test_the_generated_tests_pass(tmp_path):
    root = _scaffold(tmp_path)
    tests = sorted(root.rglob("test_*.py"))
    assert tests
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *map(str, tests)],
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]


def test_the_monitor_states_a_unit_a_classification_and_a_delivery(tmp_path):
    root = _scaffold(tmp_path)
    monitor = next(root.rglob("monitor.py"))
    sys.path.insert(0, str(monitor.parents[1]))
    try:
        mod = importlib.import_module(f"{monitor.parent.name}.monitor")
    finally:
        sys.path.pop(0)
    assert mod.UNIT and mod.CLASSIFICATION
    assert mod.check(None)["status"] == "not_checked"
    text = monitor.read_text()
    assert "notifications.skills import alert" in text and "receipt_id=" in text
    assert "wired at promotion" not in text
