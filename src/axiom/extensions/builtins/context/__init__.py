# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""context — cross-provider project-context fan-out (ADR-051).

AGENTS.md is the single canonical project-context file; this extension
generates the per-tool files (Cursor, JetBrains Junie, Copilot) from it and
keeps them honest with a drift check.
"""

from .core import check, has_drift, init, sync

__all__ = ["sync", "check", "has_drift", "init"]
