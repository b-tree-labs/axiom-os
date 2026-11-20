# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``memory.capture-watch`` skill — the capture watchdog that can say UNKNOWN.

A launchd script ran ``axi memory capture-sweep`` through a service runtime
that did not have the verb. It read ``invalid choice`` as unparseable output,
reported that as a STALE ledger, and alerted every five minutes for two days:
754 identical alerts about a ledger that was fine the whole time (#703).

This skill is that watchdog, with the two rules the script lacked:

1. **Only a real result can say OK or STALE.** The sweep is run through the
   TARGET runtime with ``--json``; its output must carry
   :data:`~.capture_sweep.SWEEP_SCHEMA`. Anything else (a usage error, a
   traceback, a runtime that will not start, a newer schema major) is
   ``UNKNOWN: capture tooling broken (runtime/contract drift)``, never STALE.
2. **Alert on transitions, not state.** One alert on entering a bad state, one
   on recovery, and a reminder once a day while it persists. The state lives
   in a small JSON file beside the other watch state.

Alerts go to the ``alert`` callable; the CLI wrapper routes them through HERALD
(logged and deduped like every other node-health signal) and prints them.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .capture_sweep import SWEEP_SCHEMA

#: How often a persisting bad state is re-announced.
REMIND_EVERY = timedelta(hours=24)

OK, STALE, UNKNOWN = "OK", "STALE", "UNKNOWN"

_UNKNOWN_DETAIL = "capture tooling broken (runtime/contract drift)"


def _schema_major(schema: str) -> tuple[str, str]:
    name, _, major = schema.partition("/")
    return name, major


def classify_sweep(stdout: str, returncode: int) -> tuple[str, str]:
    """``(state, detail)`` from one ``capture-sweep --json`` run."""
    doc: Any = None
    for candidate in (stdout, stdout[stdout.find("{"):] if "{" in stdout else ""):
        try:
            doc = json.loads(candidate)
            break
        except ValueError:
            continue
    want_name, want_major = _schema_major(SWEEP_SCHEMA)
    if not isinstance(doc, dict) or _schema_major(str(doc.get("schema", ""))) != (
        want_name,
        want_major,
    ):
        last = (stdout or "").strip().splitlines()[-1:] or [f"exit {returncode}, no output"]
        return UNKNOWN, f"{_UNKNOWN_DETAIL}: {last[0][:200]}"
    if doc.get("ok"):
        return OK, ""
    age = doc.get("ledger_age_hours")
    errors = doc.get("errors") or []
    if age is None:
        return STALE, (errors[0] if errors else "ledger has no captured turns")
    return STALE, f"newest captured turn is {float(age):.1f}h old"


def decide_alert(
    previous: dict | None, state: str, detail: str, *, now: datetime
) -> tuple[str | None, dict]:
    """``(alert text or None, new watch record)`` for one observation."""
    prev_state = (previous or {}).get("state", OK)
    since = (previous or {}).get("since") if prev_state == state else None
    last_alert = (previous or {}).get("last_alert") if prev_state == state else None
    record = {
        "state": state,
        "detail": detail,
        "since": since or now.isoformat(),
        "last_alert": last_alert,
        "checked_at": now.isoformat(),
    }
    alert: str | None = None
    if state != prev_state:
        alert = (
            f"Memory capture recovered (was {prev_state})."
            if state == OK
            else f"{state}: memory capture — {detail}"
        )
    elif state != OK and last_alert:
        if now - datetime.fromisoformat(last_alert) >= REMIND_EVERY:
            alert = f"{state}: memory capture still failing since {record['since']} — {detail}"
    if alert:
        record["last_alert"] = now.isoformat()
    return alert, record


def _default_run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def capture_watch(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """Run the sweep through the target runtime, classify it, alert on change.

    Params:

    - ``axi`` (required) — the console script the service runs.
    - ``state_path`` (required) — the watch record file.
    - ``sweep_args`` — extra arguments for ``capture-sweep``.
    - ``alert`` — ``callable(text)``; default: none sent.
    - ``run`` / ``now`` — injectable for tests.
    """
    axi = params.get("axi")
    state_path = params.get("state_path")
    if not axi or not state_path:
        return SkillResult(ok=False, errors=["axi and state_path are required"])
    run: Callable[..., subprocess.CompletedProcess] = params.get("run") or _default_run
    alert_fn: Callable[[str], Any] = params.get("alert") or (lambda _text: None)
    now: datetime = params.get("now") or datetime.now(UTC)
    state_path = Path(state_path)

    cmd = [str(axi), "memory", "capture-sweep", "--json", *(params.get("sweep_args") or [])]
    try:
        proc = run(cmd)
        state, detail = classify_sweep(proc.stdout or "", proc.returncode)
        if state == UNKNOWN and proc.stderr:
            detail = f"{_UNKNOWN_DETAIL}: {proc.stderr.strip().splitlines()[-1][:200]}"
    except (OSError, subprocess.SubprocessError) as exc:
        state, detail = UNKNOWN, f"{_UNKNOWN_DETAIL}: {type(exc).__name__}: {exc}"

    try:
        previous = json.loads(state_path.read_text())
    except (OSError, ValueError):
        previous = None
    alert, record = decide_alert(previous, state, detail, now=now)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(record, indent=2) + "\n")
    if alert:
        try:
            alert_fn(alert)
        except Exception:  # noqa: BLE001 - a failed send must not hide the state
            pass

    return SkillResult(
        ok=state == OK,
        value={"state": state, "detail": detail, "alert": alert, "since": record["since"]},
        errors=[] if state == OK else [f"{state}: {detail}"],
        actions_taken=[f"alerted: {alert}"] if alert else [],
    )
