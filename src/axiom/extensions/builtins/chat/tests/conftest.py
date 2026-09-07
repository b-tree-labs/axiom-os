# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Test config for the chat extension."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user state dir at a per-test temp directory.

    ``ChatAgent`` loads persisted tool permissions from
    ``get_user_state_dir()`` on construction and writes back on an
    ``A``/``D`` approval choice. Without this every test that builds an
    agent would read from, and could write into, the developer's real
    state tree. Tests that need a specific location still override
    ``AXI_STATE_DIR`` themselves; a test-level ``monkeypatch`` wins.
    """
    from axiom.infra.branding import get_branding

    state = tmp_path / "axi-state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    # ``get_user_state_dir`` consults ``<CLI_NAME>_STATE_DIR`` first, so a
    # branded override in the developer's shell must not leak in either.
    branded = f"{get_branding().cli_name.upper()}_STATE_DIR"
    if branded != "AXI_STATE_DIR":
        monkeypatch.setenv(branded, str(state))
    return state
