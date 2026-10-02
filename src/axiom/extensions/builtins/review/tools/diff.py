# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Diff tool — shells out to git diff to produce a unified diff string."""

from __future__ import annotations

import subprocess


def local_diff(base: str = "main") -> str:
    """Return the unified diff of HEAD against *base*.

    Shells out to ``git diff <base>...HEAD --unified=5``.
    Returns an empty string if there are no changes or git is unavailable.
    Pure function — no state, no side effects beyond the subprocess call.
    """
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            ["git", "diff", f"{base}...HEAD", "--unified=5"],
            capture_output=True,
            text=True,
            check=False,
            env=safe_git_env(),
        )
        return result.stdout
    except FileNotFoundError:
        return ""


__all__ = ["local_diff"]
