# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""connector.* skills — the single operation surface over the Registry Fabric
(ADR-074). The `axi connector` CLI, a catalog UI / Vyzier, and AXI all dispatch
these same `(params, ctx) -> SkillResult` functions (ADR-056). Verb names are
imperative per AEOS §4.3.1: list / show / install / enable / disable / check.

Skills read the platform default fabric + connection store + enabled state,
but accept injected ``fabric`` / ``connections`` / ``state`` in params for
testing and for scoped (per-cohort) fabrics later.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.connector_fabric import (
    ArtifactClass,
    ConnectionInstance,
    ConnectionStatus,
    default_connections,
    default_fabric,
    default_state,
)
from axiom.infra.skills import SkillResult


def _fabric(params: dict):
    return params.get("fabric") or default_fabric()


def _connections(params: dict):
    return params.get("connections") or default_connections()


def _state(params: dict):
    return params.get("state") or default_state()


def list_connectors(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """List registry entries (optionally filtered) — the browse surface."""
    fab = _fabric(params)
    ac = params.get("artifact_class")
    kind = params.get("kind")
    artifact_class = ArtifactClass(ac) if ac else None
    entries = fab.catalog(artifact_class=artifact_class, kind=kind)
    state = _state(params)
    return SkillResult(
        ok=True,
        value={
            "entries": [
                {
                    "name": d.name,
                    "version": d.version,
                    "title": d.title,
                    "artifact_class": d.artifact_class.value,
                    "kind": d.kind,
                    "trust_tier": d.trust_tier.value,
                    "availability": d.availability.value,
                    "enabled": state.is_enabled(d.name),
                    "setup": d.setup.to_dict() if d.setup else None,
                }
                for d in entries
            ]
        },
    )


def show(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Show one entry: its server.json descriptor, required secrets, bound
    connections, and (if any) its guided-setup plan with deep-linked URLs."""
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])
    d = _fabric(params).get(name)
    if d is None:
        return SkillResult(ok=False, errors=[f"no connector registered named {name!r}"])
    return SkillResult(
        ok=True,
        value={
            "descriptor": d.to_server_json(),
            "required_secrets": [e.name for e in d.env if e.is_secret and e.is_required],
            "connection_ref": d.connection_ref,
            "availability": d.availability.value,
            "enabled": _state(params).is_enabled(name),
            "connections": [c.name for c in _connections(params).for_connector(name)],
            "setup": d.setup.to_dict() if d.setup else None,
        },
    )


def install(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Create a connection instance for a connector (PENDING).

    Per-connector automated install (e.g. Slack app-manifest + OAuth) plugs
    in behind this; the connection record + secret_ref binding is the shared
    spine. Credentials never pass through here — only the secret_ref pointer.
    """
    connector = params.get("connector")
    name = params.get("name")
    owner = params.get("owner")
    secret_ref = params.get("secret_ref")
    missing = [k for k in ("connector", "name", "owner", "secret_ref") if not params.get(k)]
    if missing:
        return SkillResult(ok=False, errors=[f"missing required params: {', '.join(missing)}"])

    fab = _fabric(params)
    if fab.get(connector) is None:
        return SkillResult(ok=False, errors=[f"unknown connector {connector!r} — not in the fabric"])

    conn = ConnectionInstance(
        name=name, connector=connector, owner=owner, secret_ref=secret_ref,
        status=ConnectionStatus.PENDING,
    )
    _connections(params).put(conn)
    return SkillResult(
        ok=True,
        value={
            "connection": name,
            "connector": connector,
            "status": conn.status.value,
            "next_steps": "authenticate the connection, then check to activate",
        },
        actions_taken=[f"registered connection {name!r} for {connector!r} (pending)"],
    )


def enable(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Switch a connector ON (explicit per-connector opt-in)."""
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])
    if _fabric(params).get(name) is None:
        return SkillResult(ok=False, errors=[f"no connector registered named {name!r}"])
    _state(params).enable(name)
    return SkillResult(ok=True, value={"name": name, "enabled": True}, actions_taken=[f"enabled {name}"])


def disable(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Switch a connector OFF."""
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])
    _state(params).disable(name)
    return SkillResult(ok=True, value={"name": name, "enabled": False}, actions_taken=[f"disabled {name}"])


def status(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Connection health/status (optionally for one connection). Uses the
    platform-wide `status` verb for consistency (cf. 20+ other surfaces)."""
    store = _connections(params)
    name = params.get("name")
    conns = [store.get(name)] if name else store.all()
    conns = [c for c in conns if c is not None]
    if name and not conns:
        return SkillResult(ok=False, errors=[f"no connection named {name!r}"])
    return SkillResult(
        ok=all(c.status is not ConnectionStatus.ERROR for c in conns),
        value={
            "connections": [
                {
                    "name": c.name,
                    "connector": c.connector,
                    "status": c.status.value,
                    "owner": c.owner,
                    "scopes": c.scopes,
                }
                for c in conns
            ]
        },
    )


__all__ = ["list_connectors", "show", "install", "enable", "disable", "status"]
