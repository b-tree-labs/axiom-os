# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``connector reload`` for video sources: apply a changed trigger attachment (VID-015, ADR-220 §3).

A trigger is attached by editing a source's ``[[connector.params.triggers]]`` and
running ``connector reload``, not by a verb. This checks the attachments (every
trigger known, every config builds, one attachment per trigger), asks the copy
holding the source to apply them (``<state>/video/reload/<name>``, which the
holder reads within a second), and waits briefly for its answer, so the row says
whether the change is running, pending, or waits for capture to start.

Health is ADR-220 §3's: an attachment that cannot be applied is ``failed`` when
nothing is running it and ``degraded`` while the holder still runs the previous
one. Only triggers are applied: a changed source (URL, transport, fps cap)
applies at the next start, and the row says so.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import state as vs
from .triggers import TriggerCatalog

#: How long reload waits for the holder to say it applied the change.
ACK_WAIT_S = 3.0


def reload_sources(
    state_dir: Path,
    name: str | None = None,
    *,
    wait_s: float = ACK_WAIT_S,
    catalog: TriggerCatalog | None = None,
) -> list[dict[str, Any]]:
    """One row per video source reloaded (``[]`` if ``name`` is not one):
    ``connector``, ``kind``, ``health``, ``detail``, ``triggers``, ``errors``, ``applied``
    (``running`` | ``pending`` | ``not-running``)."""
    from .connector import ConnectorNotFound, NotAVideoConnector, list_connectors, load
    from .runner import check_attachments
    from .sources import InvalidSourceConfig
    from .status import source_status
    from .triggers import catalog as default_catalog

    sd = Path(state_dir)
    if name:
        try:
            connectors = [load(name, state_dir=sd)]
        except (ConnectorNotFound, NotAVideoConnector):
            return []
        except InvalidSourceConfig as exc:
            return [_row(name, "failed", f"{exc}. Fix: {exc.fix}", [], [str(exc)], "not-running")]
    else:
        connectors = list_connectors(state_dir=sd)
    found = catalog if catalog is not None else default_catalog(refresh=True)

    asked: dict[str, tuple[str, int | None]] = {}
    checked: dict[str, list[str]] = {}
    for connector in connectors:
        checked[connector.name] = check_attachments(connector.triggers, found)
        holder = _holder(connector.name, sd)
        request = vs.request_reload(connector.name, sd)
        if holder is not None:
            asked[connector.name] = (request, holder)

    acks = _wait_for_acks(sd, asked, wait_s)
    rows = []
    for connector in connectors:
        attached = _names(connector.triggers)
        held = connector.name in asked
        ack = acks.get(connector.name)
        errors = list(ack["errors"]) if ack else checked[connector.name]
        if held and ack and ack.get("applied") is False:
            applied = "running"
            pid = asked[connector.name][1]
            kept = ", ".join(ack["running"]) or "none"
            detail = (
                f"not applied: the running capture (pid {pid}) keeps its previous triggers ({kept})"
            )
        elif held and ack:
            applied = "running"
            pid = asked[connector.name][1]
            detail = f"applied to the running capture (pid {pid}): {', '.join(ack['running']) or 'no triggers'}"
        elif held:
            applied = "pending"
            detail = (
                "asked the running capture to apply them; it has not answered yet "
                f"(`{_command()} status {connector.name}` shows when it has)"
            )
        else:
            applied = "not-running"
            detail = (
                f"checked; they apply when capture starts: `{_command()} start {connector.name}`"
            )
        if errors:
            health = "degraded" if held else "failed"
            detail += "; " + "; ".join(errors)
        else:
            health = source_status(connector, state_dir=sd)["health"]
        if ack and ack.get("note"):
            detail += f"; {ack['note']}"
        rows.append(_row(connector.name, health, detail, attached, errors, applied))
    return rows


def _command() -> str:
    """``<cli> instrument video`` (ADR-256 D4)."""
    from axiom.extensions.builtins.video.naming import command

    return command()


def _row(
    name: str, health: str, detail: str, triggers: list[str], errors: list[str], applied: str
) -> dict[str, Any]:
    return {
        "connector": name,
        "kind": "video",
        "health": health,
        "detail": detail,
        "triggers": triggers,
        "errors": errors,
        "applied": applied,
    }


def _names(tables: Any) -> list[str]:
    return [str(t.get("name")) for t in tables if isinstance(t, dict) and t.get("name")]


def _holder(name: str, state_dir: Path) -> int | None:
    """The pid of the live copy holding ``name`` and saying so, or ``None``."""
    holder = vs.claim_holder(name, state_dir)
    live = vs.read_live(name, state_dir) or {}
    if not holder or not holder["alive"] or holder["pid"] != live.get("pid"):
        return None
    if live.get("state") in ("stopped", "finished", "failed", "waiting"):
        return None
    return int(holder["pid"]) if holder["pid"] is not None else None


def _wait_for_acks(
    state_dir: Path, asked: dict[str, tuple[str, int | None]], wait_s: float
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, wait_s)
    acks: dict[str, dict[str, Any]] = {}
    while True:
        for name, (request, _pid) in asked.items():
            if name in acks:
                continue
            ack = (vs.read_live(name, state_dir) or {}).get("reload") or {}
            if ack.get("request") == request:
                acks[name] = ack
        if len(acks) == len(asked) or time.monotonic() >= deadline:
            return acks
        time.sleep(0.1)


__all__ = ["ACK_WAIT_S", "reload_sources"]
