# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Interactive git-repository setup helpers.

Wraps the primitives in :mod:`axiom.infra.git` with the user-facing offer
flow used when a command needs a git repo but the current filespace isn't
one. Kept separate from ``git.py`` so the primitives stay free of any I/O.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from axiom.infra.git import git_available, init_repo, is_inside_work_tree


def ensure_repo_or_offer_init(
    path: Path | str,
    *,
    assume_yes: bool = False,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> bool:
    """Ensure ``path`` is inside a git work tree, offering ``git init`` if not.

    Returns True if ``path`` is (or becomes) a repo, False if git is missing
    or the user declines. Silent on the happy path. In a non-interactive
    context (no TTY) it does not prompt — it prints how to initialize and
    returns False, so automation fails fast rather than hanging on stdin.
    """
    p = Path(path)
    if not git_available():
        output_fn("  git is not installed — cannot initialize a repository.")
        output_fn("  Install git: https://git-scm.com/downloads")
        return False
    if is_inside_work_tree(p):
        return True
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not assume_yes and not interactive:
        output_fn(f"  {p} is not a git repository.")
        output_fn(f"  Initialize one with:  git init {p}")
        return False
    if not assume_yes:
        answer = (
            input_fn(f"  {p} is not a git repository. Initialize one here? [y/N] ")
            .strip()
            .lower()
        )
        if answer not in ("y", "yes"):
            output_fn("  Aborted — no repository created.")
            return False
    init_repo(p)
    output_fn(f"  ✓ Initialized empty git repository at {p}")
    return True


__all__ = ["ensure_repo_or_offer_init"]
