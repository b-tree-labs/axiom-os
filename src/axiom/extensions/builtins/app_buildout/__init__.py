# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``app`` — the web app's build-out tooling.

``axi app acceptance`` lists every acceptance row of the build-out spec with
the tests that carry its ID and a verdict of pass, fail or unproven, across
the repositories a workspace names (ADR-056: one skill behind CLI, MCP and
chat).
"""

from __future__ import annotations

__all__ = ["acceptance"]
