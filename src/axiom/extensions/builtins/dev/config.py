# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A checkout's optional ``dev.toml``: its frontend dev server and dev settings.

The engine owns ports, state and sign-in; a repo declares only what is
different about it. Placeholders ``{port}`` (the lane's front port) and
``{node_url}`` (the node on the lane's api port) are filled in here, so a
repo never names a port.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILE = "dev.toml"


class DevConfigError(ValueError):
    """dev.toml is present but unusable; the message says what to fix."""


@dataclass(frozen=True)
class Web:
    cwd: str
    command: str
    ready_path: str = "/"
    env: dict[str, str] = field(default_factory=dict)


#: Persona name -> a role the gate already has (ADR-151 / ADR-222). A repo may
#: re-map them under ``[personas]``; the right-hand side must be a real role.
DEFAULT_PERSONAS = {
    "site-admin": "site-admin",
    "experimenter": "creator",
    "manager": "admin",
    "visitor": "guest",
    "auditor": "viewer",
}


def canonical_persona(name: str) -> str:
    """A persona or role name in its current id: ``operator`` is ``site-admin`` (ADR-261)."""
    from axiom.governance.roles import canonical_role

    return canonical_role(name)


@dataclass(frozen=True)
class Tenant:
    id: str
    site: str
    config: str = ""
    flags: dict[str, str] = field(default_factory=dict)
    brand: str = ""


@dataclass(frozen=True)
class DevConfig:
    tenants: tuple[Tenant, ...] = ()
    personas: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PERSONAS))
    web: Web | None = None
    config_dir: str = ""
    seed: str = ""
    shots: str = ""


def load(root: Path) -> DevConfig:
    path = Path(root) / FILE
    if not path.is_file():
        return DevConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DevConfigError(f"{path} is not valid TOML: {exc}") from exc
    web = None
    if "web" in data:
        w = data["web"]
        if not isinstance(w, dict) or not str(w.get("command") or "").strip():
            raise DevConfigError(f"{path}: [web] needs a `command`")
        env = w.get("env") or {}
        if not isinstance(env, dict):
            raise DevConfigError(f"{path}: [web].env must be a table of strings")
        ready = str(w.get("ready_path") or "/")
        web = Web(
            cwd=str(w.get("cwd") or "."),
            command=str(w["command"]),
            ready_path=ready if ready.startswith("/") else "/" + ready,
            env={str(k): str(v) for k, v in env.items()},
        )
    tenants = []
    for t in data.get("tenant") or []:
        if not isinstance(t, dict) or not str(t.get("id") or "").strip():
            raise DevConfigError(f"{path}: every [[tenant]] needs an `id`")
        from axiom.webauth.users import validate_site

        tid = str(t["id"]).strip()
        try:
            site = validate_site(t.get("site") or tid) or tid
        except ValueError as exc:
            raise DevConfigError(f"{path}: tenant {tid!r}: {exc}") from exc
        tenants.append(
            Tenant(
                id=tid,
                site=site,
                config=str(t.get("config") or ""),
                flags={str(k): str(v) for k, v in (t.get("flags") or {}).items()},
                brand=str(t.get("brand") or ""),
            )
        )
    if len({t.id for t in tenants}) != len(tenants):
        raise DevConfigError(f"{path}: tenant ids must be unique")
    personas = {
        **DEFAULT_PERSONAS,
        # A dev.toml written before ADR-261 may say ``operator = "operator"``.
        **{canonical_persona(str(k)): canonical_persona(str(v))
           for k, v in (data.get("personas") or {}).items()},
    }
    dev = data.get("dev") or {}
    cfg_dir = str(dev.get("config_dir") or "")
    if cfg_dir and not (Path(root) / cfg_dir).is_dir():
        raise DevConfigError(f"{path}: [dev].config_dir {cfg_dir!r} is not a directory")
    return DevConfig(
        tenants=tuple(tenants),
        personas=personas,
        web=web,
        config_dir=cfg_dir,
        seed=str(dev.get("seed") or ""),
        shots=str(dev.get("shots") or ""),
    )


def fill(text: str, *, port: int, node_url: str) -> str:
    return text.replace("{port}", str(port)).replace("{node_url}", node_url)
