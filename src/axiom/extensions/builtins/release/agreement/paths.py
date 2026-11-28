# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where the evidence lives: under the user's state folder, never in a repository."""

from __future__ import annotations

from pathlib import Path


def state_root(state_dir: Path | str | None = None) -> Path:
    """The state folder: the argument, else the user state folder every module shares
    (``axiom.infra.paths.get_user_state_dir``, which honors ``AXI_STATE_DIR``)."""
    if state_dir:
        return Path(state_dir)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def agreement_dir(state_dir: Path | str | None = None) -> Path:
    return state_root(state_dir) / "agents" / "rivet" / "agreement"


def default_projects_root() -> Path:
    """Where Claude Code keeps this machine's session transcripts. Local sessions only."""
    return Path.home() / ".claude" / "projects"
