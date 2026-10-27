# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`status` on a node with a role answers in that role's terms (ADR-164)."""

from __future__ import annotations

import os
import subprocess
import sys

from axiom.extensions.builtins.status import role_status
from axiom.infra.node_functions import NodeConfig

CFG = NodeConfig(role="collector", functions=("acquire", "transmit"))


def test_a_failing_row_carries_its_fix_and_sets_the_verdict():
    sections = [
        {"title": "Collector", "rows": [
            {"label": "running", "value": "no", "state": "fail", "fix": "start it"},
            {"label": "last reading", "value": "09:14", "state": "ok"},
        ]},
    ]
    text = role_status.render(CFG, sections)
    assert "This node's role: collector (acquire, transmit)" in text
    assert "✗ running" in text and "fix: start it" in text
    assert role_status.worst(sections) == "fail"


def test_an_ok_row_shows_no_fix_line():
    text = role_status.render(CFG, [{"title": "T", "rows": [
        {"label": "x", "value": "fine", "state": "ok", "fix": "never shown"}]}])
    assert "never shown" not in text


def test_status_on_a_role_node_never_shows_the_platform_dashboard(tmp_path):
    node = tmp_path / "node.toml"
    node.write_text('[node]\nrole = "collector"\nfunctions = ["acquire", "transmit"]\n')
    env = dict(os.environ, AXIOM_NODE_CONFIG=str(node), AXI_STATE_DIR=str(tmp_path / "s"),
               AXIOM_DISABLE_UPDATE_NUDGE="1", AXIOM_DISABLE_SELF_HEAL="1")
    out = subprocess.run([sys.executable, "-m", "axiom.axiom_cli", "status"],
                         capture_output=True, text=True, env=env, timeout=120)
    assert "This node's role: collector" in out.stdout, out.stderr
    assert "Database" not in out.stdout
