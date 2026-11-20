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
import subprocess
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

    Returns True if ``path`` is (or becomes) a repo, False if git is missing,
    the user declines, or the init fails. Silent on the happy path. A person
    answering the prompt is the only way a repository gets created. With no TTY,
    or with ``assume_yes``, it prints how to initialize and returns False:
    nothing unattended creates a repository, because the directory it happens to
    run in is rarely one anybody meant to version.
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
    if assume_yes or not interactive:
        output_fn(f"  {p} is not a git repository.")
        output_fn(f"  Initialize one with:  git init {p}")
        return False
    answer = (
        input_fn(f"  {p} is not a git repository. Initialize one here? [y/N] ")
        .strip()
        .lower()
    )
    if answer not in ("y", "yes"):
        output_fn("  Aborted — no repository created.")
        return False
    try:
        init_repo(p)
    except subprocess.CalledProcessError:
        output_fn(f"  git init failed in {p} — no repository created.")
        return False
    output_fn(f"  ✓ Initialized empty git repository at {p}")
    return True


__all__ = ["ensure_repo_or_offer_init"]
