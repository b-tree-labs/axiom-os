# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The real orchestrator, supervised, is switched with no lost or doubled work.

This is the data_platform service's ADR-182 proof (its `verified_by`): the
orchestrator process runs under `python -m axiom.infra.switch --worker`
against a throwaway Postgres, is switched on SIGHUP, and exits cleanly on
SIGTERM. Exactly-once across the overlap itself is proven in
test_orchestrator_service.py; this proves the process lifecycle around it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from axiom.infra.change_intent import changes
from axiom.infra.db import provision_extension


def test_the_supervised_orchestrator_switches_and_exits_cleanly(
    fresh_postgres, tmp_path, monkeypatch
):
    # The database is this test's alone, so it needs no worker-scoped schema.
    # (schedule's 0001 migration names schema="schedule" outright, so it cannot
    # run under one — a defect of its own, not this test's subject.)
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    provisioned = provision_extension("schedule")
    assert provisioned.ok, provisioned.error[:3000]
    env = {
        **os.environ,
        "AXIOM_DB_URL": fresh_postgres,
        "AXI_STATE_DIR": str(tmp_path / "state"),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "axiom.infra.switch",
            "--worker",
            "--drain-s",
            "10",
            "--subject",
            "orchestrator",
            "--",
            sys.executable,
            "-m",
            "axiom.extensions.builtins.data_platform.orchestration",
        ],
        env=env,
    )
    try:
        time.sleep(4.0)  # first copy up
        assert proc.poll() is None
        proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not any(
            c.kind == "deploy" and c.outcome for c in changes()
        ):
            time.sleep(0.25)
    finally:
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=60)
    recorded = [(c.kind, c.subject, c.outcome) for c in changes() if c.kind == "deploy"]
    assert recorded == [("deploy", "orchestrator reload", "switched")], recorded
    assert code == 0
