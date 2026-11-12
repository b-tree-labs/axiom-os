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
    #: Sources whose bronze is whole files mirrored from a share
    #: (``_records`` + ``_content``), conformed by a DEPOSIT normalizer.
    #: ``{connector: schema_ref}``; everything else in ``sources`` is rows.
    deposit_sources: dict[str, str] = field(default_factory=dict)
    #: ``[uncertainty] posture`` from kit.toml: what the kit says about how well
    #: its readings are known when a row declares nothing. Empty = unstated.
    uncertainty_posture: str = ""
    #: ``[units.undeclared]`` from kit.toml: the channels whose unit is KNOWN to
    #: be unknown, as glob patterns, and why. A row with no unit is a check
    #: failure unless its channel is listed here — absence is declared once,
    #: with a reason, rather than tolerated row by row.
    undeclared_unit_channels: tuple[str, ...] = ()
    undeclared_unit_reason: str = ""

    def unit_knowingly_undeclared(self, channel: str) -> bool:
        from fnmatch import fnmatchcase

        return any(fnmatchcase(channel, pattern) for pattern in self.undeclared_unit_channels)

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
    sources: dict[str, str] = {}
    deposit_sources: dict[str, str] = {}
    for name, spec in (doc.get("sources") or {}).items():
        if not isinstance(spec, dict):
            continue
        ref = str(spec.get("schema_ref", ""))
        kind = str(spec.get("kind", "rows")).strip() or "rows"
        if kind == "deposit":
            deposit_sources[str(name)] = ref
        elif kind == "rows":
            sources[str(name)] = ref
        else:
            raise KitError(
                f'{path}: [sources.{name}] kind = {kind!r} — a source is "rows" '
                f'(records under _rows/) or "deposit" (whole files under _records/)'
            )
    posture = str((doc.get("uncertainty") or {}).get("posture", "")).strip()
    undeclared = (doc.get("units") or {}).get("undeclared") or {}
    if not isinstance(undeclared, dict):
        raise KitError(f"{path}: [units.undeclared] must be a table with `channels` and `reason`")
    patterns = undeclared.get("channels") or []
    reason = str(undeclared.get("reason", "")).strip()
    if not isinstance(patterns, list) or not all(isinstance(x, str) and x for x in patterns):
        raise KitError(
            f"{path}: [units.undeclared] channels must be a list of channel names or globs"
        )
    if patterns and not reason:
        raise KitError(
            f"{path}: [units.undeclared] lists channels with no `reason`. Say why the unit is "
            f"unknown and what would settle it; an unexplained absence is the thing this replaces"
        )
    return KitProject(
        root=root,
        tenant=tenant,
        sources=sources,
        deposit_sources=deposit_sources,
        uncertainty_posture=posture,
        undeclared_unit_channels=tuple(patterns),
        undeclared_unit_reason=reason,
    )


def validate_tenant(tenant: str) -> str:
    if not _TENANT.match(tenant or ""):
        raise KitError(
            f"tenant {tenant!r} must be lowercase letters, digits and hyphens, "
            f"starting with a letter, e.g. `rig-site`"
        )
    return tenant


__all__ = ["KitError", "KitProject", "find_root", "load", "slug", "validate_tenant"]
