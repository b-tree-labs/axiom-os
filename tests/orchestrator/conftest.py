# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Keep the durable approval queue out of the developer's real state tree.

``ApprovalGate`` defaults to in-memory precisely so constructing one is not a
write. But a test that asks for ``durable=True`` without redirecting state
writes to ``get_user_state_dir()``, which is the user's home.

That is not hypothetical: a test here pinned the wrong environment variable, so
the redirect silently did nothing and the suite left ``approvals.json`` in
``~/.axi/orchestrator``. The test still passed on the assertion it made, which
is what made it invisible.

Autouse, so it cannot be forgotten by a new test rather than remembered by
every one.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path_factory, monkeypatch):
    """Point ``AXI_STATE_DIR`` at a per-test directory.

    ``AXI_STATE_DIR`` is the platform's documented override — the directory it
    names is branding-aware, the variable is a fixed literal so a runbook can
    name it. A test setting anything else is redirecting nothing.
    """
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path_factory.mktemp("state")))
