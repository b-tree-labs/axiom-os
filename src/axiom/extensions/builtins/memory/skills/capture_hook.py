# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``memory.hook`` skill — capture a turn because the turn happened.

The scheduled sweep is a *scrape*: it reads whatever the harness still
has on disk when a timer fires, which means it silently inherits that
harness's retention policy. Claude Code's 30-day default deleted months
of sessions before capture ever saw them, and a scrape of an emptied
window is indistinguishable from a quiet week.

This runs on the harness's own events instead. Claude Code fires
``Stop`` when an assistant response completes and ``SessionEnd`` when a
session closes, handing over a payload with ``transcript_path``. The
turn is folded in at that moment. The sweep stays as a backstop for
whatever the hook misses (other tools, crashed sessions, a held lock).

**This code runs inside the user's conversation, so it is written to be
harmless first and useful second:** every failure path returns ok with a
zero count rather than raising, and the CLI wrapper always exits 0. A
memory backstop that can break the session it observes is worse than no
backstop at all.

Concurrency matters here in a way it does not for a timer. Turns land
fast and several sessions can run at once, so two hook runs can read the
ledger before either writes and both then write the same turn. A
non-blocking lock makes the loser skip; its turn is picked up by the
next Stop event or by the sweep, so skipping costs latency, never data.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_AGENT = "axi-memory"

_LOCK_NAME = "memory-capture.lock"


def _acquire_lock(lock_dir: Path | str):
    """Take the capture lock, or return None if another run holds it.

    Non-blocking on purpose: the hook runs in the user's turn, so waiting
    on a lock would trade their latency for our convenience.
    """
    import fcntl

    directory = Path(lock_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / _LOCK_NAME, "w")
    except OSError:
        return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _default_lock_dir() -> Path:
    return Path.home() / ".axi" / "locks"


def capture_hook(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """Fold the transcript named in a harness hook payload into the ledger.

    Params:

    - ``composition`` (required) — the principal's CompositionService.
    - ``principal`` (required) — ledger owner.
    - ``payload`` — the hook event dict; ``transcript_path`` is the only
      field used, and every other shape is tolerated.
    - ``lock_dir`` — where the capture lock lives.
    - ``salience_gate`` — set False for a lossless dump.
    """
    from axiom.memory.session_capture import ingest_claude_code_jsonl

    empty = {
        "written": 0, "skipped": 0, "filtered": 0,
        "skipped_locked": False, "transcript": "",
    }

    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=True, value=dict(empty),
                           errors=["no composition service provided"])
    principal = params.get("principal")
    if not principal:
        return SkillResult(ok=True, value=dict(empty),
                           errors=["no principal resolved"])

    payload = params.get("payload") or {}
    if not isinstance(payload, dict):
        return SkillResult(ok=True, value=dict(empty),
                           errors=["hook payload was not an object"])

    transcript = payload.get("transcript_path") or ""
    if not transcript or not os.path.exists(transcript):
        # Nothing to do. Not an error: SessionEnd can fire for a session
        # whose transcript was never written.
        return SkillResult(ok=True, value=dict(empty))

    lock = _acquire_lock(params.get("lock_dir") or _default_lock_dir())
    if lock is None:
        value = dict(empty)
        value["skipped_locked"] = True
        value["transcript"] = transcript
        return SkillResult(ok=True, value=value)

    try:
        report = ingest_claude_code_jsonl(
            composition=composition,
            path=transcript,
            principal_id=principal,
            salience_gate=params.get("salience_gate", True),
        )
    except Exception as exc:  # never surface a traceback into the session
        return SkillResult(ok=True, value=dict(empty), errors=[str(exc)])
    finally:
        lock.close()

    return SkillResult(
        ok=True,
        value={
            "written": report["written"],
            "skipped": report["skipped"],
            "filtered": report.get("filtered", 0),
            "skipped_locked": False,
            "transcript": transcript,
            "event": payload.get("hook_event_name", ""),
            "session_id": payload.get("session_id", ""),
        },
        actions_taken=(
            [f"captured {report['written']} turn(s) from {os.path.basename(transcript)}"]
            if report["written"] else []
        ),
    )
