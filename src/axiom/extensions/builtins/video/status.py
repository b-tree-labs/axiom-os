# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``status`` view: declaration, last verify and live session, joined per source (VID-006).

Capture runs in another process, so the session half comes from the snapshot the
claim holder writes. A snapshot is believed only while its writer still holds the
claim: one left by a process that has exited reads as ``stopped``. Health follows
ADR-220 §3; the source's own description is ``redacted()``, so no output holds a
credential.

Each row also carries the triggers attached in the source's file (``attached``)
and, while a copy holds the source, each running trigger's health (``triggers``).
A failed trigger makes the source ``degraded`` and the note says which and its
fix; a file whose attachments differ from the running ones says that
``connector reload`` applies them.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .naming import command as _command
from . import state as vs
from .connector import ConnectorNotFound, VideoConnector, list_connectors, load
from .sources import VideoSourceError, source_kind

#: Session states that mean a started source is not delivering frames.
_TROUBLED = ("reconnecting", "waiting")
#: States a snapshot may say without a live writer behind it.
_TERMINAL = ("stopped", "finished", "failed")


def _cli() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


def _describe_source(connector: VideoConnector) -> dict[str, Any]:
    """The kind's own redacted description, built without resolving the credential."""
    try:
        shown = dict(
            source_kind(connector.source_kind).build(connector.source_params(), None).redacted()
        )
    except (VideoSourceError, LookupError, ValueError) as exc:
        return {"kind": connector.source_kind, "error": str(exc)}
    if "authenticated" in shown:
        shown["authenticated"] = bool(connector.credential_ref)
    return shown


def _attached(connector: VideoConnector) -> dict[str, Any]:
    """``{name: config}`` as the runner would build it; a table it cannot read maps to itself."""
    from .triggers import TriggerConfigError, split_attachment

    out: dict[str, Any] = {}
    for table in connector.triggers:
        try:
            name, config = split_attachment(table)
        except TriggerConfigError:
            name = str(table.get("name") or "?") if isinstance(table, Mapping) else "?"
            config = dict(table) if isinstance(table, Mapping) else {}
        out[name] = None if name in out else config
    return out


def _trigger_notes(
    name: str, triggers: list[dict[str, Any]], attached: dict[str, Any]
) -> list[str]:
    cli = _cli()
    notes = [
        f"trigger {t['name']} failed: {t.get('last_error')}"
        + (f". Fix: {t['fix']}" if t.get("fix") else "")
        for t in triggers
        if t.get("state") == "failed"
    ]
    running = {t["name"]: t.get("config") for t in triggers}
    if running != attached:
        notes.append(
            f"the triggers in its file differ from the running ones: `{cli} connector reload {name}` "
            "applies them"
        )
    return notes


def source_status(
    connector: VideoConnector, *, state_dir: Path, now: float | None = None
) -> dict[str, Any]:
    """One row: kind, source, credential_ref, health, state, started, pid, session_id, fps,
    frames, drops, reconnects, last_frame_age_s, error, fix, note, verify, standby,
    attached, triggers."""
    now = time.time() if now is None else now
    name = connector.name
    started = vs.is_started(name, state_dir)
    record = vs.read_verify_record(name, state_dir)
    live = vs.read_live(name, state_dir) or {}
    holder = vs.claim_holder(name, state_dir)
    believed = (
        bool(live) and holder is not None and holder["alive"] and holder["pid"] == live.get("pid")
    )

    if believed:
        state = str(live.get("state") or "connecting")
    elif live.get("state") in _TERMINAL:
        state = str(live["state"])
    else:
        state = "stopped"
    session = live if (believed or state in _TERMINAL) else {}
    last_frame_at = session.get("last_frame_at")
    standby = [
        {"copy_id": s.get("copy_id"), "pid": s.get("pid")} for s in vs.read_standby(name, state_dir)
    ]

    attached = _attached(connector)
    triggers = [t for t in (live.get("triggers") or []) if isinstance(t, dict)] if believed else []
    failed_trigger = any(t.get("state") == "failed" for t in triggers)

    if record is None:
        health, note = "unmounted", f"not verified yet: `{_command()} verify {name}`"
    elif not record.ok:
        health, note = "failed", record.fix or "the last verify failed"
    elif (
        state in _TROUBLED
        or (state == "failed")
        or (started and state == "stopped")
        or failed_trigger
    ):
        health, note = "degraded", ""
    else:
        health, note = "ready", ""
    if started and not believed and state not in ("finished",):
        note = f"started, but nothing is capturing it: `{_command()} start {name}` brings it back"
    elif state == "waiting":
        note = "waiting for another copy to release it (an overlap switch)"
    elif state == "reconnecting" and session.get("fix"):
        note = str(session["fix"])
    if believed:
        note = "; ".join(n for n in [note, *_trigger_notes(name, triggers, attached)] if n)

    return {
        "name": name,
        "kind": connector.source_kind,
        "source": _describe_source(connector),
        "credential_ref": connector.credential_ref,
        "fps_cap": connector.fps_cap,
        "health": health,
        "state": state,
        "started": started,
        "pid": live.get("pid") if believed else None,
        "copy_id": live.get("copy_id") if believed else None,
        "session_id": session.get("session_id"),
        "fps": session.get("fps"),
        "frames": int(session.get("frames") or 0),
        "drops": int(session.get("drops") or 0),
        "reconnects": int(session.get("reconnects") or 0),
        "last_frame_age_s": None
        if last_frame_at is None
        else round(max(0.0, now - float(last_frame_at)), 3),
        "probe": session.get("probe"),
        "error": session.get("error"),
        "fix": session.get("fix"),
        "note": note,
        "verify": record.as_dict() if record else None,
        "standby": standby,
        "attached": list(attached),
        "triggers": triggers,
    }


def all_status(*, state_dir: Path, name: str | None = None) -> list[dict[str, Any]]:
    """Every declared source's row, or just ``name``'s (``ConnectorNotFound`` if undeclared)."""
    if name:
        return [source_status(load(name, state_dir=state_dir), state_dir=state_dir)]
    return [source_status(c, state_dir=state_dir) for c in list_connectors(state_dir=state_dir)]


__all__ = ["ConnectorNotFound", "all_status", "source_status"]
