# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tenant's kit folder, read from ``data/kit.toml``.

Everything a contribution needs to know about where it lives comes from here,
so the verbs, the notebook and an agent all agree on one layout.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KIT_DIRNAME = "data"
MANIFEST = "kit.toml"
STATE_DIRNAME = ".kit"

#: A tenant id becomes a schema name and a role name, so it is held to the
#: characters both accept. Refused, never sanitised: two tenants that sanitise
#: to one name would share a schema.
_TENANT = re.compile(r"^[a-z][a-z0-9-]{1,47}$")


class KitError(ValueError):
    """A kit folder that cannot be used, with the file and the fix in the message."""


def slug(tenant: str) -> str:
    """The tenant id as it appears in schema and role names."""
    return tenant.replace("-", "_")


@dataclass(frozen=True)
class KitProject:
    """One tenant's ``data/`` folder."""

    root: Path
    tenant: str
    sources: dict[str, str] = field(default_factory=dict)
    #: ``[uncertainty] posture`` from kit.toml: what the kit says about how well
    #: its readings are known when a row declares nothing. Empty = unstated.
    uncertainty_posture: str = ""

    @property
    def gold_schema(self) -> str:
        return f"gold_{slug(self.tenant)}"

    @property
    def role(self) -> str:
        return f"kit_{slug(self.tenant)}"

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def out_dir(self) -> Path:
        return self.state_dir / "out"

    def folder(self, name: str) -> Path:
        return self.root / name


def find_root(start: Path | str | None = None) -> Path:
    """The ``data/`` folder at or above ``start`` (a repo root or anywhere inside it)."""
    here = Path(start or Path.cwd()).expanduser().resolve()
    for directory in (here, *here.parents):
        if (directory / MANIFEST).is_file():
            return directory
        if (directory / KIT_DIRNAME / MANIFEST).is_file():
            return directory / KIT_DIRNAME
    from axiom.infra.branding import get_branding

    cli = get_branding().cli_name or "axi"
    raise KitError(
        f"no {KIT_DIRNAME}/{MANIFEST} at or above {here}. "
        f"Run `{cli} data kit-init --tenant <your-site>` in your site repository first."
    )


def load(start: Path | str | None = None) -> KitProject:
    """Read ``kit.toml`` into a :class:`KitProject`."""
    root = find_root(start)
    path = root / MANIFEST
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise KitError(f"{path}: not valid TOML: {exc}") from None
    tenant = str(doc.get("kit", {}).get("tenant", "")).strip()
    if not _TENANT.match(tenant):
        raise KitError(
            f"{path}: [kit] tenant = {tenant!r} must be lowercase letters, digits and "
            f"hyphens, starting with a letter (it becomes a schema and a role name)"
        )
    sources = {
        str(name): str(spec.get("schema_ref", ""))
        for name, spec in (doc.get("sources") or {}).items()
        if isinstance(spec, dict)
    }
    posture = str((doc.get("uncertainty") or {}).get("posture", "")).strip()
    return KitProject(root=root, tenant=tenant, sources=sources, uncertainty_posture=posture)


def validate_tenant(tenant: str) -> str:
    if not _TENANT.match(tenant or ""):
        raise KitError(
            f"tenant {tenant!r} must be lowercase letters, digits and hyphens, "
            f"starting with a letter, e.g. `rig-site`"
        )
    return tenant


__all__ = ["KitError", "KitProject", "find_root", "load", "slug", "validate_tenant"]
