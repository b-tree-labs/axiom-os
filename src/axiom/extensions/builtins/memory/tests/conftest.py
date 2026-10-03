# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Test config for the memory extension.

``axiom-tests`` is a ``pytest11`` plugin — installing it in the active
environment is sufficient for its fixtures to be available here.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_harness_configs(tmp_path, monkeypatch):
    """Backstop: every MCP registrar config path points into ``tmp_path``.

    Registrars honour ``AXIOM_<TOOL>_CONFIG`` before their default path, so
    a test that calls one without an explicit ``config_path`` can never
    reach the developer's real IDE config. (A stub test once did exactly
    that the moment the stub became real, and wrote a fixture interpreter
    into a live ``~/.gemini/settings.json``.) Tests that need a specific
    path still set the env var or pass ``config_path`` themselves — their
    own monkeypatch runs after this one and wins.
    """
    from axiom.extensions.builtins.mcp.install import TOOL_SPECS

    for spec in TOOL_SPECS:
        monkeypatch.setenv(
            spec.env_override,
            str(tmp_path / "harness-configs" / f"{spec.name}.cfg"),
        )
