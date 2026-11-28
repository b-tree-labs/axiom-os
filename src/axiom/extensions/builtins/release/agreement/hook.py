# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``UserPromptSubmit`` hook: append every submitted prompt to the local log.

Claude Code runs this, not the model. It reads the hook's JSON from stdin
(``session_id``, ``prompt_id``, ``transcript_path``, ``cwd``, ``prompt``), appends
one line, prints nothing (stdout on exit 0 would be added to the model's context)
and always exits 0 (a hook must never block or slow the owner's prompt).

The hook also fires for a scheduled task, a background agent reporting back and a
message from another session, and gives no field that says which. It therefore
logs the prompt as received and marks relayed shapes ``wrapped``. Whether a person
typed it is decided later, from the transcript row's own ``origin``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import store


def handle(
    payload: object, *, state_dir: Path | str | None = None, now: datetime | None = None
) -> str | None:
    """Log one hook payload. Returns the entry id, or None when there was nothing to log."""
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt")
    session_id = payload.get("session_id")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or not isinstance(session_id, str)
        or not session_id
    ):
        return None
    return store.append_prompt(
        state_dir,
        session_id=session_id,
        prompt_id=str(payload.get("prompt_id") or ""),
        transcript_path=str(payload.get("transcript_path") or ""),
        cwd=str(payload.get("cwd") or ""),
        prompt=prompt,
        now=now,
    )


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    """Entry point. Silent and exit 0 whatever happens."""
    try:
        raw = (stdin or sys.stdin).read()
        handle(json.loads(raw) if raw.strip() else None)
    except Exception:  # noqa: BLE001 - a logging hook must never stop a prompt
        pass
    return 0
