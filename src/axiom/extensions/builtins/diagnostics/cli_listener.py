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

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.diagnostics import cli_diagnoses
from axiom.infra.bus import EventBus

log = logging.getLogger(__name__)

PENDING_FILENAME = "pending-diagnoses.jsonl"


def install_id() -> str:
    """A short, stable id for the Python install this CLI is running from.

    Two aliases of one install (``axi`` and ``neut``) share a ``sys.prefix``
    and therefore an id — which is the whole point, and the property the
    machine-scoped path was introduced to get. Two genuinely different
    installs on one machine do not.
    """
    return hashlib.sha256(
        str(Path(sys.prefix).resolve()).encode("utf-8")
    ).hexdigest()[:12]


def pending_path(state_dir: Path | None = None) -> Path:
    """Resolve the pending-diagnoses log path for THIS install.

    A diagnosis describes a failure of this install on this machine. The
    path used to stop at "this machine" — ``~/.axi/agents/triage/`` — which
    made every install on the box share one queue. The partner onboarding
    smoke test caught what that costs: a freshly provisioned venv, running
    a partner's first command, printed five of the operator's pending
    failures, naming that node's Postgres role and vault state. None of
    them had anything to do with the install doing the printing.

    So the path carries the install too. ``axi`` and ``neut`` still share a
    queue, because they share a ``sys.prefix``; a separate venv gets its
    own, because it is a separate install with its own failures.

    Accepts an explicit ``state_dir`` for tests; otherwise resolves under
    the PLATFORM state dir rather than the branded one, so the alias typed
    never decides which queue is read.
    """
    if state_dir is None:
        from axiom.infra.paths import get_platform_state_dir

        state_dir = get_platform_state_dir()
    return state_dir / "agents" / "triage" / "installs" / install_id() / PENDING_FILENAME


def _adopt_legacy_diagnoses() -> None:
    """Move diagnoses filed at an older, wider scope into this install's queue.

    Two older locations exist, each the residue of a scope that turned out
    to be too wide:

    * the branded home (``~/.neut/...``) — split one install's queue in half
      by which alias was typed;
    * the machine-scoped path (``~/.axi/agents/triage/<file>``) — merged
      every install on the machine into one queue.

    Records in either are still real failures somebody should see, so the
    running install adopts them rather than leaving them where nothing will
    read them again. Deduped by fingerprint, and the old file is removed
    once its contents are safely here — leaving it in place would resurrect
    the leak on the next read.

    Adoption is best-effort by design: this runs ahead of somebody else's
    command, and a half-written legacy file must not stop it.
    """
    try:
        from axiom.infra.paths import get_platform_state_dir, get_user_state_dir

        mine = pending_path()
        platform = get_platform_state_dir()
        legacies = [platform / "agents" / "triage" / PENDING_FILENAME]
        branded = get_user_state_dir()
        if branded.resolve() != platform.resolve():
            legacies.append(branded / "agents" / "triage" / PENDING_FILENAME)

        known = {d.get("fingerprint") for d in read_pending()}
        for legacy in legacies:
            if not legacy.exists() or legacy.resolve() == mine.resolve():
                continue
            adopted = []
            for raw in legacy.read_text().splitlines():
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if record.get("fingerprint") not in known:
                    adopted.append(raw)
                    known.add(record.get("fingerprint"))
            if adopted:
                mine.parent.mkdir(parents=True, exist_ok=True)
                with mine.open("a") as fh:
                    fh.write("\n".join(adopted) + "\n")
            legacy.unlink()
            log.debug("cli_listener: adopted %d diagnoses from %s", len(adopted), legacy)
    except Exception as exc:  # noqa: BLE001
        log.debug("cli_listener: adopting legacy diagnoses failed: %s", exc)


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


def clear_pending(state_dir: Path | None, *, fingerprint: str | None) -> int:
    """Remove pending diagnoses. Returns how many were actually removed.

    With `fingerprint=None`, clears all entries (the user said "I'm done,
    don't surface anything more"). With a specific fingerprint, removes
    only the matching entry — the typical post-fix path where one issue
    is resolved but others remain pending.

    The count is the point. This used to return nothing, and its caller
    printed "Cleared diagnosis <fp>." either way — so a fingerprint that
    matched nothing reported success and the operator walked away believing
    the queue was empty. A dismissal that cannot fail cannot be trusted.
    """
    path = pending_path(state_dir)
    if not path.exists():
        return 0
    existing = read_pending(state_dir)
    if fingerprint is None:
        path.unlink()
        return len(existing)
    remaining = [d for d in existing if d.get("fingerprint") != fingerprint]
    removed = len(existing) - len(remaining)
    if removed == 0:
        return 0
    if not remaining:
        path.unlink()
        return removed
    path.write_text("\n".join(json.dumps(d) for d in remaining) + "\n")
    return removed


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
