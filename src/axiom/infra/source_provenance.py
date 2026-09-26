# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Say so when the CLI is running a different checkout than the one you are in.

One virtualenv serves every worktree, so ``pip install -e`` anchors the package
to whichever checkout ran it. Standing in another worktree and typing ``axi``
runs the anchor's code, and the symptom is a verb that "does not exist" or a fix
that "did not work" — with nothing on screen connecting either to the cause.

That cost real time before it was diagnosed, and the diagnosis is one string
comparison. So the CLI does the comparison and says what it found.

It warns rather than refuses. Running the installed build from inside a
checkout is a legitimate thing to do — comparing behaviour, reproducing a
report against the release — and a tool that refused would be wrong more often
than it was right. It is silenced with ``AXI_NO_SOURCE_WARNING=1`` for anyone
who does that deliberately and often.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["foreign_checkout_warning", "warn_if_foreign_checkout"]

#: Set to any non-empty value to silence the notice.
SUPPRESS_ENV = "AXI_NO_SOURCE_WARNING"


def _package_root(module_file: str | None) -> Path | None:
    """The ``src`` directory the running ``axiom`` package came from."""
    if not module_file:
        return None
    # .../<checkout>/src/axiom/__init__.py  ->  .../<checkout>
    axiom_dir = Path(module_file).resolve().parent
    src = axiom_dir.parent
    return src.parent if src.name == "src" else None


def _checkout_containing(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` that looks like an axiom checkout.

    "Looks like" means it has ``src/axiom/__init__.py``. Deliberately not a git
    query: this runs on every invocation, and shelling out to git to decide
    whether to print a warning would be a real cost for a rare notice.
    """
    for directory in (start, *start.parents):
        if (directory / "src" / "axiom" / "__init__.py").is_file():
            return directory
    return None


def foreign_checkout_warning(
    module_file: str | None, cwd: Path | None = None
) -> str | None:
    """The notice to print, or ``None`` when there is nothing to say.

    Pure, so the decision can be tested without a process or a chdir.
    """
    if os.environ.get(SUPPRESS_ENV):
        return None

    here = _checkout_containing((cwd or Path.cwd()).resolve())
    if here is None:
        return None  # not standing in a checkout; nothing to compare

    running = _package_root(module_file)
    if running is None or running == here:
        return None

    return (
        f"note: running axiom from {running}, but you are in {here}.\n"
        f"      One venv serves every worktree, so the editable install wins.\n"
        f"      To use this checkout:  source scripts/worktree-env.sh\n"
        f"      Silence this with {SUPPRESS_ENV}=1."
    )


def warn_if_foreign_checkout(stream=None) -> bool:
    """Print the notice to stderr if there is one. Returns whether it printed.

    Never raises. A CLI that failed to start because it could not work out
    where its own source lives would be a worse bug than the one this reports.
    """
    import sys

    try:
        import axiom

        message = foreign_checkout_warning(getattr(axiom, "__file__", None))
    except Exception:  # noqa: BLE001 — a diagnostic must not break the command
        return False
    if not message:
        return False
    print(message, file=stream or sys.stderr)
    return True
