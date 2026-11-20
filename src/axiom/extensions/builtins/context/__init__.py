# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""context — cross-provider project-context fan-out (ADR-051).

AGENTS.md is the single canonical project-context file; this extension
generates the per-tool files (Cursor, JetBrains Junie, Copilot, Gemini) from
it and keeps them honest with a drift check. It also keeps the repo
standard's managed block, "Working here with an AI", current inside
AGENTS.md (ADR-221; see ``standard``).
"""

from .core import check, has_drift, init, sync

__all__ = ["sync", "check", "has_drift", "init"]
