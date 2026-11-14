# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One switchboard: every switch on this node, of three kinds (ADR-254).

===========  ==========================================================  ===================
Kind         What it switches                                            Scopes
===========  ==========================================================  ===================
extension    an optional extension (``[extension.optional]``)             node
function     an ADR-164 node function, a command, the background-agents  node
             service, and the site policies (agents, maintenance,
             record of truth)
flag         a product surface declared with ``[[flags]]`` (ADR-210)      node, account, user
===========  ==========================================================  ===================

Everything is declared in manifests, as before; this module only reads the
declarations together and gives each switch one row: its kind, its owner (the
extension that declares it, ``""`` for the platform's own), the scopes it can
be set at, its state here and where that state came from.

The one resolution rule that spans kinds: **an extension that is off makes
every function and flag it owns resolve off** (source ``extension-off``). The
flag half lives in :mod:`.resolver`; the function half is applied here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from axiom.infra import node_functions as nf

from .flag_registry import ON
from .resolver import EXTENSION_OFF, Resolution

KINDS: tuple[str, ...] = ("extension", "function", "flag")

#: Where a switch of each kind can be set. Extension and function switches
#: change what runs on a machine, so they are the node's alone.
SCOPES_BY_KIND: dict[str, tuple[str, ...]] = {
    "extension": ("node",),
    "function": ("node",),
    "flag": ("node", "account", "user"),
}

#: The site policies, listed as function switches whose value is a level.
POLICIES: tuple[str, ...] = ("agents", "maintenance", "record-of-truth")


@dataclass(frozen=True)
class Switch:
    """One row of the switchboard."""

    name: str
    kind: str  # extension | function | flag
    subkind: str  # extension | node-function | command | service | policy | flag
    enabled: bool
    value: str  # on | off | soon, or a policy's level
    source: str  # role | feature | always | in-use | default | env | variant | node | account | user | extension-off | declared | ""
    owner: str = ""  # the declaring extension; "" is the platform
    scopes: tuple[str, ...] = ("node",)
    commands: tuple[str, ...] = ()
    description: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scopes"] = list(self.scopes)
        d["commands"] = list(self.commands)
        d["details"] = dict(self.details)
        return d


def _first_sentence(text: str, limit: int = 120) -> str:
    """A manifest description's first sentence, for a table cell."""
    head = text.strip().split(". ", 1)[0].split("\n", 1)[0].rstrip(".")
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def extension_rows(cfg: nf.NodeConfig, optional: Mapping[str, Mapping[str, Any]]) -> list[Switch]:
    rows: list[Switch] = []
    for name, info in sorted(optional.items()):
        enabled = bool(info.get("enabled"))
        if name in cfg.features:
            source = "feature"
        elif name in cfg.off:
            source = "off"
        elif enabled and str(info.get("default", "off")).lower() != "on":
            source = "in-use"
        else:
            source = "default"
        rows.append(
            Switch(
                name=name,
                kind="extension",
                subkind="extension",
                enabled=enabled,
                value=ON if enabled else "off",
                source=source,
                owner=name,
                scopes=SCOPES_BY_KIND["extension"],
                description=_first_sentence(str(info.get("description") or "")),
                details={
                    "default": str(info.get("default", "off")),
                    "in_use_when": list(info.get("in_use_when") or []),
                },
            )
        )
    return rows


def function_rows(
    cfg: nf.NodeConfig,
    commands: Mapping[str, Mapping[str, Any]],
    off: Iterable[str] = (),
) -> list[Switch]:
    """Node functions, commands and the background-agents service, then the policies."""
    off = frozenset(off)
    rows: list[Switch] = []
    for r in nf.feature_rows(cfg, commands):
        owner = ""
        subkind = {"function": "node-function"}.get(r.kind, r.kind)
        if r.kind == "command":
            owner = str((commands.get(r.name) or {}).get("extension") or "")
        enabled, source = r.enabled, r.source
        if owner and owner in off:
            enabled, source = False, EXTENSION_OFF
        rows.append(
            Switch(
                name=r.name,
                kind="function",
                subkind=subkind,
                enabled=enabled,
                value=ON if enabled else "off",
                source=source,
                owner=owner,
                scopes=SCOPES_BY_KIND["function"],
                commands=tuple(r.commands),
                description=str((commands.get(r.name) or {}).get("description") or "")
                if r.kind == "command"
                else "",
            )
        )
    declared = {
        "agents": (cfg.agent_policy, bool(cfg.agents)),
        "maintenance": (cfg.maintenance_level, bool(cfg.maintenance)),
        "record-of-truth": (cfg.record_of_truth_in_force, bool(cfg.record_of_truth)),
    }
    for name in POLICIES:
        level, said = declared[name]
        rows.append(
            Switch(
                name=name,
                kind="function",
                subkind="policy",
                enabled=True,
                value=level,
                source="declared" if said else "default",
                scopes=SCOPES_BY_KIND["function"],
                description={
                    "agents": "the site's agent policy: none, assist or local",
                    "maintenance": "how much remote maintenance this site accepts: off, requests or sessions",
                    "record-of-truth": "which copy of this site's data wins: upstream or local (ADR-180)",
                }[name],
            )
        )
    return rows


def flag_rows(
    resolution: Resolution,
    metadata: Mapping[str, Mapping[str, Any]],
    overrides: Mapping[str, list[dict[str, Any]]] | None = None,
    *,
    prefix: str = "",
) -> list[Switch]:
    rows: list[Switch] = []
    for key, meta in sorted(metadata.items()):
        if prefix and not key.startswith(prefix):
            continue
        value = resolution.flags.get(key, "off")
        waiting_on = resolution.waiting_on.get(key)
        staged = resolution.staged.get(key)
        rows.append(
            Switch(
                name=key,
                kind="flag",
                subkind="flag",
                enabled=value == ON,
                value=value,
                source=resolution.sources.get(key, "undeclared"),
                owner=str(meta.get("owner") or ""),
                scopes=SCOPES_BY_KIND["flag"],
                description=str(meta.get("description") or ""),
                details={
                    "default": meta.get("default"),
                    "protected": bool(meta.get("protected")),
                    "owner_off": bool(meta.get("owner_off")),
                    # Held by an off extension: what it waits on, and what applies once that is on.
                    "waiting_on": waiting_on,
                    "staged_value": staged[0] if staged else None,
                    "staged_source": staged[1] if staged else None,
                    "overrides": list((overrides or {}).get(key, [])),
                    # LOOP-F22: what the flag is for (None: declared before kinds), and what it
                    # may serve (LOOP-F17) and needs (LOOP-F20).
                    "flag_kind": meta.get("kind"),
                    "remove_by": meta.get("remove_by"),
                    "variants": meta.get("variants"),
                    "variant_value": resolution.values.get(key) if hasattr(resolution, "values") else None,
                    "requires": meta.get("requires") or [],
                },
            )
        )
    return rows


def switchboard(
    cfg: nf.NodeConfig,
    *,
    commands: Mapping[str, Mapping[str, Any]],
    optional: Mapping[str, Mapping[str, Any]],
    resolution: Resolution,
    metadata: Mapping[str, Mapping[str, Any]],
    overrides: Mapping[str, list[dict[str, Any]]] | None = None,
    kind: str | None = None,
    prefix: str = "",
) -> list[Switch]:
    """Every switch, or those of one ``kind``; flag names may be narrowed by ``prefix``."""
    off = frozenset(name for name, info in optional.items() if not info.get("enabled"))
    # An internal-tier command (a hidden alias, say) is not a switch anyone is offered.
    shown = {n: i for n, i in commands.items() if str(i.get("tier") or "") != "internal"}
    rows: list[Switch] = []
    if kind in (None, "extension"):
        rows += extension_rows(cfg, optional)
    if kind in (None, "function"):
        rows += function_rows(cfg, shown, off)
    if kind in (None, "flag"):
        rows += flag_rows(resolution, metadata, overrides, prefix=prefix)
    return rows


def classify(
    name: str,
    *,
    commands: Mapping[str, Any],
    optional: Mapping[str, Any],
    flags: Mapping[str, Any],
    prefer: str = "switch",
) -> str | None:
    """Which kind a bare name refers to, or ``None`` when nothing is called that.

    A name can be both a command and a flag (a verb and the flag that gates
    it). ``prefer="switch"`` picks the node switch, which is what ``enable``
    and ``disable`` have always meant; ``prefer="flag"`` picks the flag, which
    is what the scoped ``set`` is for. ``--kind`` says it outright.
    """
    is_switch = (
        name in optional
        or name in nf.FUNCTIONS
        or name in commands
        or name == nf.AGENTS_FEATURE
    )
    is_flag = name in flags
    if is_switch and is_flag:
        return "flag" if prefer == "flag" else ("extension" if name in optional else "function")
    if is_switch:
        return "extension" if name in optional else "function"
    if is_flag:
        return "flag"
    return None


__all__ = [
    "KINDS",
    "POLICIES",
    "SCOPES_BY_KIND",
    "Switch",
    "classify",
    "extension_rows",
    "flag_rows",
    "function_rows",
    "switchboard",
]
