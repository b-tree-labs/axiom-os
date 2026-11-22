# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The record of a node-scope switch, and the node file as its cache (ADR-254 §6).

An extension or function switch is written to two places:

* the node file (``axiom.infra.node_functions``), always. Discovery reads it
  before any database is up, and a node with no database has nothing else.
* the switchboard's table (``flag_overrides``, schema ``flags``) when this node
  has a database, as a node-scope row keyed ``extension:<name>`` or
  ``function:<name>``. Flag keys never contain ``:``, so the two namespaces
  share the table without touching its schema. The row carries ``updated_by``
  and ``updated_at`` like every flag override.

Reads of the switch state come from the node file, the copy discovery itself
reads, so what the switchboard shows is what runs. When the table and the file
disagree (a hand-edited file, a restored database) the switchboard says so
(:func:`drift`) and names the fix; it never flips the file on its own, in
either direction. Each drift also raises one alert through the platform's one
notification path (:func:`alert_drift`), so nobody has to be reading the
switchboard to learn of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.infra import node_functions as nf

_log = logging.getLogger("axiom.features.record")

SCOPE = "node"
PREFIXES: dict[str, str] = {"extension": "extension:", "function": "function:"}


def switch_key(kind: str, name: str) -> str:
    return f"{PREFIXES[kind]}{name}"


def this_node() -> str:
    from axiom.infra.topology import this_node as _this_node

    return _this_node()["node"]


def _store():
    from axiom.extensions.builtins.flags.store import OverrideStore

    return OverrideStore()


def mirror(kind: str, name: str, value: str | None, *, by: str, cfg: nf.NodeConfig | None = None) -> str:
    """Write the switch's row (``value`` on/off) or remove it (``None``).

    Returns ``recorded``, ``cleared`` or ``unavailable``. A node whose role
    needs no database is never asked for one; a database that cannot be
    reached is reported, not raised: the node file already holds the switch.
    """
    cfg = cfg if cfg is not None else nf.load()
    if not nf.needs_database(cfg):
        return "unavailable"
    key = switch_key(kind, name)
    try:
        store = _store()
        if value is None:
            store.delete(SCOPE, this_node(), key)
            return "cleared"
        store.put(SCOPE, this_node(), key, "on" if value == "on" else "off", by=by)
        return "recorded"
    except Exception:  # noqa: BLE001 - no database is not an error for a switch
        return "unavailable"


def recorded(cfg: nf.NodeConfig | None = None) -> dict[tuple[str, str], str] | None:
    """``{(kind, name): on|off}`` recorded for this node, or ``None`` with no database."""
    cfg = cfg if cfg is not None else nf.load()
    if not nf.needs_database(cfg):
        return None
    try:
        rows = _store().overrides_for(SCOPE, this_node())
    except Exception:  # noqa: BLE001
        return None
    out: dict[tuple[str, str], str] = {}
    for key, value in rows.items():
        for kind, prefix in PREFIXES.items():
            if key.startswith(prefix):
                out[(kind, key[len(prefix):])] = value
    return out


@dataclass(frozen=True)
class Drift:
    """One switch whose record and node file disagree."""

    kind: str
    name: str
    recorded: str  # on | off
    file: str  # on | off
    fix: str  # the command that makes them agree

    @property
    def subject(self) -> str:
        return switch_key(self.kind, self.name)

    @property
    def line(self) -> str:
        return (
            f"{self.kind} {self.name} is recorded {self.recorded} but the node file says {self.file}; "
            f"`{self.fix}` makes them agree"
        )


def drift_items(
    cfg: nf.NodeConfig, rows: list[Any], *, recorded_rows: dict[tuple[str, str], str] | None = None
) -> list[Drift]:
    """Where the recorded switch disagrees with the node file, with the fix for each."""
    rec = recorded(cfg) if recorded_rows is None else recorded_rows
    if not rec:
        return []
    cli = _cli()
    by_name = {(r.kind, r.name): r for r in rows if r.kind in PREFIXES}
    out: list[Drift] = []
    for (kind, name), want in sorted(rec.items()):
        row = by_name.get((kind, name))
        if row is None or row.subkind == "policy":
            continue
        have = "on" if row.enabled else "off"
        if have != want:
            verb = "enable" if want == "on" else "disable"
            out.append(Drift(kind, name, want, have, f"{cli} features {verb} {name}"))
    return out


def drift(cfg: nf.NodeConfig, rows: list[Any], *, recorded_rows: dict[tuple[str, str], str] | None = None) -> list[str]:
    """Where the recorded switch disagrees with the node file: one line each, with the fix."""
    return [d.line for d in drift_items(cfg, rows, recorded_rows=recorded_rows)]


# ---------------------------------------------------------------- the alert

#: The steward that owns node drift (KEEL); the incident names it as owner.
ALERT_ACTOR = "@keel"
ALERT_RECIPIENT = "@operator"
_ALERTED_FILE = "switch-drift-alerted.json"


def _alerted_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "features" / _ALERTED_FILE


def _signature(d: Drift) -> str:
    return f"{d.subject}={d.recorded}/{d.file}"


def _read_alerted() -> set[str]:
    try:
        return set(json.loads(_alerted_path().read_text()))
    except Exception:  # noqa: BLE001 - no memo means nothing has been alerted yet
        return set()


def _write_alerted(signatures: set[str]) -> None:
    try:
        path = _alerted_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(signatures)))
        tmp.replace(path)
    except Exception:  # noqa: BLE001 - a memo that cannot be written means a repeat alert, not a failure
        pass


def _cause(d: Drift):
    from axiom.extensions.builtins.notifications.incidents.model import Cause, Unmet

    return Cause(
        area="node",
        kind="switch_drift",
        action="features.list",
        subject=d.subject,
        error_class="SwitchDrift",
        unmet=(
            Unmet(
                id="switch-record-matches-node-file",
                why=f"the {d.kind} {d.name} is recorded {d.recorded} but the node file says {d.file}",
                fix=d.fix,
            ),
        ),
        detail={"kind": d.kind, "name": d.name, "recorded": d.recorded, "file": d.file},
    )


def _send(d: Drift) -> None:
    import importlib

    from axiom.extensions.builtins.notifications.skills.send import _ctx

    # The module, not the package attribute: the package re-exports ``send`` the function.
    notify = importlib.import_module("axiom.extensions.builtins.notifications.send")
    from axiom.governance import Classification

    notify.send(
        _ctx(),
        actor=ALERT_ACTOR,
        recipient=ALERT_RECIPIENT,
        payload=notify.NotificationPayload(
            summary=f"This node's {d.kind} {d.name} is recorded {d.recorded} but runs {d.file}.",
            body=f"{d.line}. Nothing was changed: the node file is what runs, and Axiom "
            f"never reconciles on its own.",
        ),
        classification=Classification.INTERNAL,
        priority=notify.Priority.HIGH,
        intent="features.switch_drift",
        cause=_cause(d),
    )


def alert_drift(items: list[Drift]) -> list[str]:
    """Raise one alert per drifted switch, once (ADR-254 §6, owner decision 2026-11-12).

    Each new drift goes through the one notification path with a cause, so the
    incident stage folds every drifted switch on this node into one incident.
    A memo in the state dir remembers what was already raised: reading the same
    drift again raises nothing; a drift that is fixed drops out of the memo, so
    if it comes back it is raised again. Nothing is reconciled. Never raises.
    Returns the subjects alerted now.
    """
    try:
        alerted = _read_alerted()
        current = {_signature(d): d for d in items}
        raised: list[str] = []
        kept = alerted & set(current)
        for sig, d in current.items():
            if sig in alerted:
                continue
            try:
                _send(d)
            except Exception as exc:  # noqa: BLE001 - the listing is never punished for the alert path
                _log.warning("switch drift alert for %s not raised: %s", d.subject, type(exc).__name__)
                continue
            kept.add(sig)
            raised.append(d.subject)
        if kept != alerted:
            _write_alerted(kept)
        return raised
    except Exception as exc:  # noqa: BLE001
        _log.warning("switch drift alert skipped: %s", type(exc).__name__)
        return []


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


__all__ = [
    "ALERT_ACTOR",
    "PREFIXES",
    "SCOPE",
    "Drift",
    "alert_drift",
    "drift",
    "drift_items",
    "mirror",
    "recorded",
    "switch_key",
    "this_node",
]
