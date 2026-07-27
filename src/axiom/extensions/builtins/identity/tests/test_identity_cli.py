# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IDENT-7: the `axi identity` skills + a CLI subprocess smoke (per the
cli-subprocess-smoke rule)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from axiom.extensions.builtins.identity import skills
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext
from axiom.vega.identity.custody import InMemoryCustody


def _ctx(principal):
    return SkillContext(registry=None, state_dir=Path("."),
                        logger=logging.getLogger("t"), principal=principal)


def test_whoami_reports_the_acting_principal():
    ctx = _ctx(PrincipalContext("@ben:local", "open"))
    r = skills.whoami({}, ctx)
    assert r.ok and r.value == {"principal": "@ben:local", "posture": "open", "assured": False}


def test_status_includes_node_floor():
    r = skills.status({}, _ctx(PrincipalContext("@ben:local", "open")))
    assert r.ok and "node_floor" in r.value


def test_init_creates_keypair_with_injected_custody():
    r = skills.init({"custody": InMemoryCustody()}, _ctx(PrincipalContext("@ben:local", "open")))
    assert r.ok
    assert r.value["handle"].startswith("@")
    assert len(bytes.fromhex(r.value["public_key"])) == 32     # raw Ed25519 public key
    assert r.actions_taken


def test_cli_whoami_subprocess_smoke():
    # E2E: run the entry point as a subprocess and assert on stdout. Anchor-robust
    # (point at THIS worktree's src so a sibling editable install can't shadow it).
    import os
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[5]            # .../src
    env = {**os.environ, "PYTHONPATH": f"{src_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.identity.cli", "whoami"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["posture"] == "open" and out["assured"] is False   # default free-wheeling
