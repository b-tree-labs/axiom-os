# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Saving a browser session without ever saving an empty one (#358).

A capture that closes the browser before the operator logs in produces
``{"cookies": [], "origins": []}``. Written to disk, that file looks like a session
(``has_session()`` is true), every later API call fails, and nothing says why. So an empty
capture is an error, raised at the moment of capture, and no file is written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class EmptySessionError(RuntimeError):
    """The browser context held no cookies and no origin storage: nobody logged in."""


def is_empty_session_state(state: Any) -> bool:
    """True when a Playwright storage state carries neither cookies nor origin storage."""
    if not isinstance(state, dict):
        return True
    return not state.get("cookies") and not state.get("origins")


def session_file_has_state(path: Path) -> bool:
    """True when ``path`` exists and holds a non-empty storage state."""
    try:
        return not is_empty_session_state(json.loads(path.read_text()))
    except (OSError, ValueError):
        return False


def save_session_state(context: Any, path: Path, *, provider: str) -> None:
    """Write ``context``'s storage state to ``path`` (mode 0600), refusing an empty one."""
    state = context.storage_state()
    if is_empty_session_state(state):
        raise EmptySessionError(
            f"No {provider} login was captured (no cookies, no storage), so no session was saved. "
            "Log in within the browser window and wait for the page to load before it closes."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.chmod(str(path), 0o600)
