# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Register manifest-declared ``[[extension.schedule]]`` cadences into PULSE.

This is the discovery hook the pure transform in :mod:`.manifest` anticipated
("wired into AEOS extension install in a follow-up PR") — spec-schedule-consumer-
seam §6: *manifest-declared ``[[extension.schedule]]`` discovery wired in*.

Conservative wiring (the seam's §5 open questions, refined as it matures):

- **Idempotent by ``(extension, action)``** — a re-install or upgrade
  re-registers nothing already present. ``api.register`` otherwise writes a
  fresh uuid row on every call, so without this an upgrade would duplicate.
- **Local schedules register with ``envelope=None``** — PULSE decides the
  capability at fire time (adr-004; peer-defined capability envelopes are
  PULSE-2), and ``api.register`` already tolerates a null envelope.
- **Fail-soft** — a malformed block, an unreadable manifest, or an absent PULSE
  database skips that item and never aborts install.

Not yet wired (follow-ups, tracked with the seam): removal at *uninstall* (needs
an unregister call the seam does not expose yet) and ``on_fire`` delivery.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.schedule.manifest import parse_manifest_block

log = logging.getLogger(__name__)


@dataclass
class ScheduleSyncResult:
    """What a sync pass did — ``"<ext>:<action>"`` entries per bucket."""

    registered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _read_schedule_blocks(manifest_path: Path) -> list[dict[str, Any]]:
    """The raw ``[[extension.schedule]]`` blocks from one manifest (or none)."""
    if not manifest_path.is_file():
        return []
    try:
        data = tomllib.loads(manifest_path.read_text())
    except Exception as exc:  # noqa: BLE001 — bad manifest must not abort install
        log.warning("schedule: unreadable manifest %s: %s", manifest_path, exc)
        return []
    blocks = data.get("extension", {}).get("schedule", [])
    return blocks if isinstance(blocks, list) else []


def _already_registered(ext_name: str, action: str) -> bool:
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        row = (
            s.query(ScheduleDefinition)
            .filter_by(extension=ext_name, action=action)
            .first()
        )
        return row is not None


def sync_extension_schedules(
    ext_name: str, manifest_path: Path, *, now: datetime | None = None
) -> ScheduleSyncResult:
    """Register one extension's declared schedules. Idempotent, fail-soft."""
    from axiom.extensions.builtins.schedule import api

    result = ScheduleSyncResult()
    for raw in _read_schedule_blocks(manifest_path):
        try:
            ms = parse_manifest_block(raw)
        except Exception as exc:  # noqa: BLE001 — one bad block skips itself
            result.errors.append(f"{ext_name}: invalid schedule block: {exc}")
            continue
        tag = f"{ext_name}:{ms.action}"
        try:
            if _already_registered(ext_name, ms.action):
                result.skipped.append(tag)
                continue
            api.register(
                envelope=None,
                cadence=ms.cadence,
                action=ms.action,
                description=ms.description or ms.name,
                extension=ext_name,
                retry_policy=ms.retry_policy or None,
                classification_ceiling=ms.classification_ceiling,
                raci_default=ms.raci_default,
                now=now,
            )
            result.registered.append(tag)
        except Exception as exc:  # noqa: BLE001 — PULSE DB absent etc.
            result.errors.append(f"{tag}: {exc}")
    return result


def sync_manifest_schedules(extensions: Any, *, now: datetime | None = None) -> ScheduleSyncResult:
    """Register the declared schedules of every discovered extension.

    ``extensions`` is any iterable of objects exposing ``.name`` and ``.root``
    (``discovery.discover_extensions()`` output). Aggregate result across all.
    """
    agg = ScheduleSyncResult()
    for ext in extensions:
        name = getattr(ext, "name", None)
        root = getattr(ext, "root", None)
        if not name or root is None:
            continue
        r = sync_extension_schedules(
            name, Path(root) / "axiom-extension.toml", now=now
        )
        agg.registered.extend(r.registered)
        agg.skipped.extend(r.skipped)
        agg.errors.extend(r.errors)
    return agg


def _rows_for(ext_name: str, action: str | None = None) -> list[tuple[str, str, Any, Any]]:
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        q = s.query(ScheduleDefinition).filter(
            ScheduleDefinition.extension == ext_name, ScheduleDefinition.state != "cancelled"
        )
        if action is not None:
            q = q.filter(ScheduleDefinition.action == action)
        return [(r.id, r.state, r.cadence_kind, r.cadence_payload) for r in q.order_by(ScheduleDefinition.created_at)]


def sync_hosted_schedules(extensions: Any, *, now: datetime | None = None) -> ScheduleSyncResult:
    """Register the ``hosted = true`` schedules of every extension, for the scheduler host.

    Called by the host that runs PULSE when it starts, so an extension puts its
    jobs on the schedule by declaration alone and the host never names it. For
    each hosted block of an **enabled** extension: register it when absent (with
    its declared ``params``), resume it when paused. A row whose cadence was
    changed at runtime (an extension retuning its own interval) is left as it
    is: the declaration is the default, not a pin. For an extension that is
    discovered but **not enabled** (an optional extension a node has switched
    off), its active rows are paused, so nothing fires against skills this host
    no longer binds. Idempotent and fail-soft.
    """
    from axiom.extensions.builtins.schedule import api

    result = ScheduleSyncResult()
    for ext in extensions:
        name = getattr(ext, "name", None)
        root = getattr(ext, "root", None)
        if not name or root is None:
            continue
        blocks: list[Any] = []
        for raw in _read_schedule_blocks(Path(root) / "axiom-extension.toml"):
            try:
                ms = parse_manifest_block(raw)
            except Exception as exc:  # noqa: BLE001 — one bad block skips itself
                result.errors.append(f"{name}: invalid schedule block: {exc}")
                continue
            if ms.hosted:
                blocks.append(ms)
        if not blocks:
            continue
        enabled = bool(getattr(ext, "enabled", True))
        for ms in blocks:
            tag = f"{name}:{ms.action}"
            try:
                rows = _rows_for(name, ms.action)
                if not enabled:
                    for sid, state, _kind, _payload in rows:
                        if state == "active":
                            api.pause(api.ScheduleId(sid), f"extension {name} is not enabled on this node", now=now)
                    result.skipped.append(tag)
                    continue
                if not rows:
                    envelope = {"action": ms.action, "params": dict(ms.params or {})}
                    api.register(
                        envelope=envelope,
                        cadence=ms.cadence,
                        action=ms.action,
                        description=ms.description or ms.name,
                        extension=name,
                        retry_policy=ms.retry_policy or {"max_attempts": 1},
                        misfire_policy="fire_once",
                        classification_ceiling=ms.classification_ceiling,
                        raci_default=ms.raci_default,
                        now=now,
                    )
                    result.registered.append(tag)
                    continue
                sid, state, _kind, _payload = rows[0]
                if state == "paused":
                    api.resume(api.ScheduleId(sid), now=now)
                    result.registered.append(f"{tag} (resumed)")
                else:
                    result.skipped.append(tag)
            except Exception as exc:  # noqa: BLE001 — PULSE DB absent etc.
                result.errors.append(f"{tag}: {exc}")
    return result


__all__ = [
    "ScheduleSyncResult",
    "sync_extension_schedules",
    "sync_hosted_schedules",
    "sync_manifest_schedules",
]
