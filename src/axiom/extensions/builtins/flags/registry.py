# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flag registry: every flag is declared by the extension that owns it (ADR-210).

An extension declares its flags in its manifest::

    [[flags]]
    key = "control"
    description = "The Control verb in the rail"
    default = "off"                       # on | off | soon (true/false accepted)
    default_by_env = { development = "on" }
    protected_in = ["production"]

or, for a package that is not an Axiom extension directory, through the
``axiom.flags`` entry-point group: the entry point is a callable returning a
list of the same mappings.

A flag nobody declared resolves to ``off``. Keys are lowercase identifiers so
the environment variable ``AXIOM_FLAG_<KEY>`` is always spellable.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ON, OFF, SOON = "on", "off", "soon"
VALUES: tuple[str, ...] = (ON, OFF, SOON)

#: The env var a deployment sets to fix a flag for the whole process.
ENV_PREFIX = "AXIOM_FLAG_"

#: The entry-point group a non-extension package declares flags through.
ENTRY_POINT_GROUP = "axiom.flags"

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled"}
_SOON = {"soon", "coming_soon", "coming-soon"}


def normalize_value(value: Any) -> str | None:
    """``on | off | soon`` for anything that means one of them, else ``None``.

    Booleans map to on/off, as do the usual spellings of each (SoilMetrix's
    ``_parse_bool``); ``soon`` is the third state a boolean cannot carry.
    """
    if isinstance(value, bool):
        return ON if value else OFF
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return ON
    if text in _FALSE:
        return OFF
    if text in _SOON:
        return SOON
    return None


def get_environment() -> str:
    """The deployment environment the defaults and protections are read for.

    ``AXIOM_ENVIRONMENT`` (the name the rest of Axiom reads), then the generic
    ``ENVIRONMENT``, then ``development`` (SoilMetrix's default).
    """
    raw = os.environ.get("AXIOM_ENVIRONMENT") or os.environ.get("ENVIRONMENT") or "development"
    return raw.strip().lower() or "development"


@dataclass(frozen=True)
class FlagDef:
    """One declared flag."""

    key: str
    description: str = ""
    default: str = OFF
    default_by_env: Mapping[str, str] = field(default_factory=dict)
    protected_in: tuple[str, ...] = ()
    owner: str = ""

    def default_for(self, env: str) -> str:
        """The registry default in ``env``: the per-environment value, else the flat one."""
        return self.default_by_env.get(env, self.default)

    def is_protected(self, env: str) -> bool:
        return env in self.protected_in

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, owner: str = "") -> FlagDef:
        key = str(raw.get("key") or "").strip()
        if not KEY_RE.match(key):
            raise ValueError(f"flag key {key!r} is not a lowercase identifier")
        default = normalize_value(raw.get("default", OFF))
        if default is None:
            raise ValueError(f"flag {key}: default {raw.get('default')!r} is not on, off or soon")
        by_env: dict[str, str] = {}
        for env, value in dict(raw.get("default_by_env") or {}).items():
            norm = normalize_value(value)
            if norm is None:
                raise ValueError(f"flag {key}: default_by_env.{env} {value!r} is not on, off or soon")
            by_env[str(env).lower()] = norm
        protected = tuple(str(e).lower() for e in (raw.get("protected_in") or ()))
        return cls(
            key=key,
            description=str(raw.get("description") or ""),
            default=default,
            default_by_env=by_env,
            protected_in=protected,
            owner=owner,
        )


def parse_manifest_flags(manifest: Mapping[str, Any], *, owner: str = "") -> list[FlagDef]:
    """The ``[[flags]]`` of a parsed manifest. A malformed entry is skipped and logged."""
    out: list[FlagDef] = []
    for raw in manifest.get("flags") or ():
        try:
            out.append(FlagDef.from_mapping(raw, owner=owner))
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("skipping a flag declared by %s: %s", owner or "an extension", exc)
    return out


def _manifest_flags(search_dirs: Iterable[Path] = ()) -> list[FlagDef]:
    from axiom.extensions.discovery import discover_extensions

    out: list[FlagDef] = []
    for ext in discover_extensions(*search_dirs):
        if not getattr(ext, "enabled", True):
            continue
        try:
            manifest = tomllib.loads(ext.manifest_path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        out.extend(parse_manifest_flags(manifest, owner=ext.name))
    return out


def _entry_point_flags() -> list[FlagDef]:
    import concurrent.futures
    from importlib.metadata import entry_points

    try:
        # entry_points() walks every distribution and can hang on Python 3.14
        # (see discovery._scan_installed_package_extension_dirs): bound it.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            eps = pool.submit(lambda: list(entry_points(group=ENTRY_POINT_GROUP))).result(timeout=3)
    except Exception:  # noqa: BLE001 - no entry points is an empty registry, not a failure
        return []
    out: list[FlagDef] = []
    for ep in eps:
        try:
            declared = ep.load()
            declared = declared() if callable(declared) else declared
            out.extend(parse_manifest_flags({"flags": list(declared or ())}, owner=ep.name))
        except Exception as exc:  # noqa: BLE001 - one broken package must not hide the rest
            log.warning("skipping flags from entry point %s: %s", ep.name, exc)
    return out


def load_registry(search_dirs: Iterable[Path] = (), *, entry_points: bool = True) -> dict[str, FlagDef]:
    """Every declared flag, keyed by flag key. The first declaration of a key wins,
    in discovery order (project, user, builtin, then entry points)."""
    found: dict[str, FlagDef] = {}
    sources = _manifest_flags(search_dirs) + (_entry_point_flags() if entry_points else [])
    for flag in sources:
        if flag.key in found:
            if found[flag.key].owner != flag.owner:
                log.warning(
                    "flag %s is declared by both %s and %s; %s wins",
                    flag.key, found[flag.key].owner, flag.owner, found[flag.key].owner,
                )
            continue
        found[flag.key] = flag
    return found


_cached: dict[str, FlagDef] | None = None


def registry() -> dict[str, FlagDef]:
    """The process's registry, discovered once."""
    global _cached
    if _cached is None:
        _cached = load_registry()
    return _cached


def set_registry(flags: Iterable[FlagDef] | Mapping[str, FlagDef] | None) -> None:
    """Pin the registry (tests), or ``None`` to rediscover on next use."""
    global _cached
    if flags is None:
        _cached = None
    elif isinstance(flags, Mapping):
        _cached = dict(flags)
    else:
        _cached = {f.key: f for f in flags}


__all__ = [
    "ENTRY_POINT_GROUP",
    "ENV_PREFIX",
    "KEY_RE",
    "OFF",
    "ON",
    "SOON",
    "VALUES",
    "FlagDef",
    "get_environment",
    "load_registry",
    "normalize_value",
    "parse_manifest_flags",
    "registry",
    "set_registry",
]
