# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE CLI failure listener.

Subscribes to `cli.arg_error` events on the platform bus, runs the event
through the diagnosis pattern catalog (`cli_diagnoses.match_failure`),
and on hit appends a record to `~/.axi/agents/triage/pending-diagnoses.jsonl`.
The pre-command hook in `axiom.infra.cli_hooks` reads that file on the
next CLI invocation and surfaces the diagnosis to the user.

Closes the loop the user described 2026-05-03: "triage should have seen the
error and should have understood the installation error, along with a
remedy that the user would be prompted for the next time they ran a cli
command." This module is the "see" + "understand" + "remember to prompt"
half; cli_hooks is the "prompt" half.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.diagnostics import cli_diagnoses
from axiom.infra.bus import EventBus

log = logging.getLogger(__name__)

PENDING_FILENAME = "pending-diagnoses.jsonl"


def pending_path(state_dir: Path | None = None) -> Path:
    """Resolve the pending-diagnoses log path under the user state dir.

    Accepts an explicit `state_dir` for tests; otherwise resolves to
    `~/.axi/agents/triage/`.
    """
    if state_dir is None:
        from axiom.infra.paths import get_user_state_dir
        state_dir = get_user_state_dir()
    return state_dir / "agents" / "triage" / PENDING_FILENAME


def read_pending(state_dir: Path | None = None) -> list[dict]:
    """Return the pending diagnoses, oldest first.

    Returns an empty list when the file does not exist yet (fresh install).
    Skips corrupt lines silently — a partial JSONL write should not break
    the next CLI invocation.
    """
    path = pending_path(state_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            log.debug("cli_listener: skipping corrupt pending line: %r", raw[:80])
    return out


def append_diagnosis(state_dir: Path | None, diagnosis: dict) -> None:
    """Append one diagnosis dict; dedupe by fingerprint within the file."""
    path = pending_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_pending(state_dir)
    if any(d.get("fingerprint") == diagnosis.get("fingerprint") for d in existing):
        return  # dedupe — same root cause already pending
    with path.open("a") as f:
        f.write(json.dumps(diagnosis) + "\n")


def clear_pending(state_dir: Path | None, *, fingerprint: str | None) -> None:
    """Remove pending diagnoses.

    With `fingerprint=None`, clears all entries (the user said "I'm done,
    don't surface anything more"). With a specific fingerprint, removes
    only the matching entry — the typical post-fix path where one issue
    is resolved but others remain pending.
    """
    path = pending_path(state_dir)
    if not path.exists():
        return
    if fingerprint is None:
        path.unlink()
        return
    remaining = [d for d in read_pending(state_dir) if d.get("fingerprint") != fingerprint]
    if not remaining:
        path.unlink()
        return
    path.write_text("\n".join(json.dumps(d) for d in remaining) + "\n")


def _on_cli_arg_error(subject: str, data: dict[str, Any]) -> None:
    """Bus handler. Match → (LLM fallback if no match) → append. Soft-fails
    on any unexpected error so a broken handler can never block the CLI."""
    try:
        # `diagnose` first runs the deterministic catalog; on miss it asks
        # the LLM gateway for a best-guess diagnosis (with loop protection
        # for LLM-related errors). Disable via AXI_DIAGNOSES_NO_LLM=1.
        import os
        allow_llm = not os.environ.get("AXI_DIAGNOSES_NO_LLM")
        diagnosis = cli_diagnoses.diagnose(data or {}, allow_llm=allow_llm)
        if diagnosis is None:
            return
        append_diagnosis(state_dir=None, diagnosis=diagnosis.to_dict())
        log.info(
            "TRIAGE matched cli.arg_error to pattern %s (fingerprint %s)",
            diagnosis.pattern_id,
            diagnosis.fingerprint,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("cli_listener handler failed: %s", exc)


def register(bus: EventBus) -> None:
    """Subscribe the listener to `cli.arg_error` on the given bus.

    Idempotent at the call-site of the diagnostics extension's bootstrap;
    re-subscribing the same handler is harmless because the bus tracks
    subscriptions independently.
    """
    bus.subscribe("cli.arg_error", _on_cli_arg_error, source="diagnostics.triage")
    log.debug("cli_listener registered for cli.arg_error")
