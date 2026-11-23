# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Turn a command string into an argument vector.

The string is split the way a shell would split words. It is not handed to
a shell. A pipeline, a redirect, or a command substitution is refused:
those are shell programs, and this function does not run shell programs.
"""

from __future__ import annotations

import os
import re
import shlex

_SHELL_META = re.compile(r"[|&;<>`$()\n]")
# One command name or path. No spaces, no flags-as-the-program, no `..`.
_EXECUTABLE = re.compile(r"[A-Za-z0-9/][A-Za-z0-9_./+-]*")
# A file stem used as one argument. Not a path.
_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def argv_from_command(command: str) -> list[str]:
    """``shlex.split`` of ``command``, or ``ValueError`` when it needs a shell."""
    if not isinstance(command, str) or _SHELL_META.search(command):
        raise ValueError("command must be an argument list, not a shell pipeline")
    argv = shlex.split(command)
    if not argv:
        raise ValueError("command is empty")
    return argv


def require_executable(value: str, *, what: str = "command") -> str:
    """A single command name or path, with no shell syntax and no ``..``."""
    if (
        not isinstance(value, str)
        or not _EXECUTABLE.fullmatch(value)
        or ".." in value.split("/")
    ):
        raise ValueError(f"{what} must be a command name or path, got {value!r}")
    return value


def quoted_editor(*, default: str = "vi", visual: bool = True) -> str:
    """``$EDITOR`` (then ``$VISUAL``) as one argv element.

    ``shlex.quote`` is a no-op on the accepted alphabet, so ``vim`` stays
    ``vim``. A value with a space or a metacharacter is refused.
    """
    raw = os.environ.get("EDITOR")
    if not raw and visual:
        raw = os.environ.get("VISUAL")
    return shlex.quote(require_executable(raw or default, what="EDITOR"))


def require_absolute_path(value: str, *, what: str = "path") -> str:
    """An absolute path with no shell syntax and no ``..`` component."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{what} must be an absolute path, got {value!r}")
    return require_executable(value, what=what)


def require_stem(value: str, *, what: str = "name") -> str:
    """A file stem: letters, digits, and ``._-``. Not a path."""
    if not isinstance(value, str) or not _STEM.fullmatch(value) or ".." in value.split("."):
        raise ValueError(f"{what} must be a file stem, got {value!r}")
    return value


__all__ = [
    "argv_from_command",
    "quoted_editor",
    "require_absolute_path",
    "require_executable",
    "require_stem",
]
