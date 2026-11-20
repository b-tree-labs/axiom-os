# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flag declarations: every flag is declared by the extension that owns it (ADR-210, ADR-254).

The ``flag`` kind of the switchboard (``axiom.extensions.builtins.features.registry``).
An extension declares its flags in its manifest::

    [[flags]]
    key = "control"
    description = "The Control verb in the rail"
    default = "off"                       # on | off | soon (true/false accepted)
    default_by_env = { development = "on" }
    protected_in = ["production"]
    bucket_by = "principal"               # the unit a percentage buckets (LOOP-F14): any catalogue
                                          # attribute, or "cohort:a,b" (default principal)
    kind = "release"                      # release | operational | permission | experiment | kill_switch
    remove_by = 2027-03-01                # a release flag must say when it goes (LOOP-F22)
    owner = "@maintainers"                # who is asked to remove it (LOOP-F11): the maintainers of this extension
    review_by = 2027-02-01                # when it is reviewed (LOOP-F11; a release flag's defaults to remove_by)
    requires = [{ flag = "editor", value = "on" }]   # prerequisites (LOOP-F20)
    approvals = { production = { approvals = 1, roles = ["release_manager"] } }  # LOOP-F23
    set_by = "person"                     # only a person changes it: no MCP or agent path (#1811)

A multivariate flag (LOOP-F17) declares its variants, each a string, a number or a
document, and names one of them as its default::

    [[flags]]
    key = "layout"
    variants = { compact = "compact", wide = { columns = 3 }, ratio = 0.5 }
    default = "compact"
    off_variant = "compact"               # what the kill switch serves (default: the default)

or, for a package that is not an Axiom extension directory, through the
``axiom.flags`` entry-point group: the entry point is a callable returning a
list of the same mappings.

A flag nobody declared resolves to ``off``. Keys are lowercase identifiers so
the environment variable ``AXIOM_FLAG_<KEY>`` is always spellable.

A flag declared by an optional extension this node has switched off is still
in the registry, so the switchboard can list it and say why it is off; the
resolver (``features.resolver``) is what makes it resolve ``off`` (ADR-254).
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
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

#: Who a percentage rollout buckets (LOOP-F5). The principal (a person or an agent) is the
#: default until the owner decides (PRD the loop, open question 2); a flag may declare
#: ``account`` so every principal of one account shares one assignment. Since LOOP-F14 the
#: unit may be any catalogue attribute (``node``, ``site``, ``country``, ``local_timezone``...)
#: or ``cohort:<a>,<b>`` (each listed cohort moves as one); these two are the units whose
#: bucket is the bare id, so assignments made before F14 stay where they were.
BUCKET_SUBJECTS: tuple[str, ...] = ("principal", "account")
DEFAULT_BUCKET_BY = "principal"
UNIT_RE = re.compile(r"^(?:[a-z][a-z0-9_]{0,63}|cohort:[a-z][a-z0-9_-]{0,63}(?:,[a-z][a-z0-9_-]{0,63})*)$")

#: What a flag is for (LOOP-F22). The kind sets defaults: a release flag must declare a
#: removal date; a kill switch defaults on and is never reported stale. ``""`` is a flag
#: declared before kinds existed: listed as unclassified, resolved exactly as before.
FLAG_KINDS: tuple[str, ...] = ("release", "operational", "permission", "experiment", "kill_switch")
_STALE_EXEMPT_KINDS = frozenset({"kill_switch", "permission"})

#: A flag's owner (LOOP-F11): a principal or a role, Matrix style.
OWNER_RE = re.compile(r"^@[\w.\-]+(?::[\w.\-]+)?$")

#: A variant name (LOOP-F17): a lowercase identifier, so it can be written anywhere a value can.
VARIANT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class Prerequisite:
    """This flag needs ``flag`` to resolve to one of ``values`` for the same subject (LOOP-F20)."""

    flag: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ApprovalPolicy:
    """Who approves a change to this flag's cohorts, rollout or stage plan in one environment
    (LOOP-F23): ``approvals`` people other than the author, each holding one of ``roles``
    (any role when empty). ``two_person`` is ``approvals >= 1``."""

    approvals: int = 0
    roles: tuple[str, ...] = ()
    source: str = ""

    @classmethod
    def from_mapping(cls, raw: Any, *, source: str = "") -> ApprovalPolicy:
        if not isinstance(raw, Mapping):
            raise ValueError("an approval policy is a table: { approvals = 1, roles = [...] }")
        extra = set(raw) - {"approvals", "roles", "two_person"}
        if extra:
            raise ValueError(f"unexpected keys in an approval policy: {sorted(extra)}")
        n = raw.get("approvals", 0) or 0
        if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > 5:
            raise ValueError("approvals is a whole number from 0 to 5")
        if raw.get("two_person"):
            n = max(n, 1)
        roles = raw.get("roles") or ()
        if isinstance(roles, str):
            roles = [roles]
        return cls(approvals=n, roles=tuple(str(r).strip() for r in roles if str(r).strip()), source=source)

    def as_dict(self) -> dict[str, Any]:
        return {"approvals": self.approvals, "roles": list(self.roles), "source": self.source}


def _date(value: Any, what: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{what} {value!r} is not a date (YYYY-MM-DD)") from exc

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
    bucket_by: str = DEFAULT_BUCKET_BY
    #: One of :data:`FLAG_KINDS`, or ``""`` when the flag declares none (LOOP-F22).
    kind: str = ""
    #: When a release flag is to be removed (LOOP-F22).
    remove_by: date | None = None
    #: Who answers for this flag (LOOP-F11): the manifest's ``owner``, a principal or a role
    #: (``@name[:context]``). ``owner`` above is the declaring extension. ``""``: unowned.
    owned_by: str = ""
    #: When the flag is next reviewed (LOOP-F11): fully on past it, the flag is reported to its
    #: owner as work to remove. A release flag's defaults to its ``remove_by``.
    review_by: date | None = None
    #: name -> value (a string, a number or a document); empty for an on/off/soon flag (LOOP-F17).
    variants: Mapping[str, Any] = field(default_factory=dict)
    #: The variant the kill switch, a failed prerequisite and an off extension serve.
    off_variant: str = ""
    requires: tuple[Prerequisite, ...] = ()
    #: environment -> who approves a change here (LOOP-F23).
    approvals: Mapping[str, ApprovalPolicy] = field(default_factory=dict)
    #: ``"person"``: only a person changes this flag, at the command line's prompt or on the web
    #: with a fresh sign-in; never an MCP tool, an agent tool or an in-process caller (#1811).
    set_by: str = ""

    def default_for(self, env: str) -> str:
        """The registry default in ``env``: the per-environment value, else the flat one."""
        return self.default_by_env.get(env, self.default)

    def is_protected(self, env: str) -> bool:
        return env in self.protected_in

    @property
    def multivariate(self) -> bool:
        return bool(self.variants)

    @property
    def values(self) -> tuple[str, ...]:
        """What this flag may resolve to: its variant names, or on/off/soon."""
        return tuple(self.variants) if self.variants else VALUES

    @property
    def off_value(self) -> str:
        """What "off" means for this flag: ``off``, or its off variant."""
        if not self.variants:
            return OFF
        return self.off_variant or self.default

    def normalize(self, value: Any) -> str | None:
        """A value this flag can take, spelled as it resolves, else ``None``."""
        if not self.variants:
            return normalize_value(value)
        text = str(value).strip() if isinstance(value, str) else None
        return text if text in self.variants else None

    def variant_value(self, name: str) -> Any:
        return self.variants.get(name) if self.variants else None

    @property
    def stale_exempt(self) -> bool:
        """A kill switch or a permission flag is meant to stay; it is never reported stale."""
        return self.kind in _STALE_EXEMPT_KINDS

    def removal_due(self, today: date) -> bool:
        """Whether this flag is past its declared removal date (LOOP-F11 reads this)."""
        return not self.stale_exempt and self.remove_by is not None and today >= self.remove_by

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, owner: str = "") -> FlagDef:
        key = str(raw.get("key") or "").strip()
        if not KEY_RE.match(key):
            raise ValueError(f"flag key {key!r} is not a lowercase identifier")
        kind = str(raw.get("kind") or "").strip().lower()
        if kind and kind not in FLAG_KINDS:
            raise ValueError(f"flag {key}: kind {kind!r} is one of {', '.join(FLAG_KINDS)}")
        remove_by = _date(raw.get("remove_by"), f"flag {key}: remove_by")
        if kind == "release" and remove_by is None:
            raise ValueError(f"flag {key}: a release flag declares remove_by, the date it is to be removed")
        owned_by = str(raw.get("owner") or "").strip()
        if owned_by and not OWNER_RE.match(owned_by):
            raise ValueError(f"flag {key}: owner {owned_by!r} is a principal or a role (@name or @name:context)")
        if kind == "release" and not owned_by:
            raise ValueError(f"flag {key}: a release flag declares owner, who is asked to remove it (LOOP-F11)")
        review_by = _date(raw.get("review_by"), f"flag {key}: review_by") or (remove_by if kind == "release" else None)
        variants = _variants(key, raw.get("variants"))

        def norm(value: Any, where: str) -> str:
            if variants:
                text = str(value).strip() if isinstance(value, str) else None
                if text not in variants:
                    raise ValueError(f"flag {key}: {where} {value!r} is not a declared variant "
                                     f"({', '.join(variants)})")
                return text
            out = normalize_value(value)
            if out is None:
                raise ValueError(f"flag {key}: {where} {value!r} is not on, off or soon")
            return out

        if "default" in raw:
            default = norm(raw.get("default"), "default")
        elif variants:
            raise ValueError(f"flag {key}: a flag with variants names its default variant")
        else:
            default = ON if kind == "kill_switch" else OFF
        by_env: dict[str, str] = {}
        for env, value in dict(raw.get("default_by_env") or {}).items():
            by_env[str(env).lower()] = norm(value, f"default_by_env.{env}")
        off_variant = norm(raw["off_variant"], "off_variant") if variants and raw.get("off_variant") else ""
        protected = tuple(str(e).lower() for e in (raw.get("protected_in") or ()))
        bucket_by = str(raw.get("bucket_by") or DEFAULT_BUCKET_BY).strip().lower()
        if not UNIT_RE.match(bucket_by):
            raise ValueError(f"flag {key}: bucket_by {bucket_by!r} is an attribute name or cohort:<key>,...")
        requires: list[Prerequisite] = []
        for i, req in enumerate(raw.get("requires") or ()):
            if not isinstance(req, Mapping) or not KEY_RE.match(str(req.get("flag") or "")):
                raise ValueError(f"flag {key}: requires[{i}] needs flag = \"<key>\"")
            want = req.get("value", ON)
            wants = tuple(str(w).strip() for w in (want if isinstance(want, list | tuple) else [want]))
            if not wants or not all(wants):
                raise ValueError(f"flag {key}: requires[{i}] needs the value(s) the other flag must have")
            if str(req["flag"]) == key:
                raise ValueError(f"flag {key}: a flag cannot require itself")
            requires.append(Prerequisite(flag=str(req["flag"]), values=wants))
        set_by = str(raw.get("set_by") or "").strip().lower()
        if set_by not in ("", "person"):
            raise ValueError(f"flag {key}: set_by is \"person\" or absent, not {set_by!r}")
        approvals: dict[str, ApprovalPolicy] = {}
        raw_approvals = raw.get("approvals") or {}
        if not isinstance(raw_approvals, Mapping):
            raise ValueError(f"flag {key}: approvals is a table of environments")
        for env, policy in raw_approvals.items():
            approvals[str(env).lower()] = ApprovalPolicy.from_mapping(policy, source=f"flag {key}")
        return cls(
            key=key,
            description=str(raw.get("description") or ""),
            default=default,
            default_by_env=by_env,
            protected_in=protected,
            owner=owner,
            bucket_by=bucket_by,
            kind=kind,
            remove_by=remove_by,
            owned_by=owned_by,
            review_by=review_by,
            variants=variants,
            off_variant=off_variant,
            requires=tuple(requires),
            approvals=approvals,
            set_by=set_by,
        )


def _variants(key: str, raw: Any) -> dict[str, Any]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"flag {key}: variants is a table of name = value")
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if not VARIANT_RE.match(str(name)):
            raise ValueError(f"flag {key}: variant name {name!r} is not a lowercase identifier")
        if isinstance(value, bool) or not isinstance(value, str | int | float | Mapping | list):
            raise ValueError(f"flag {key}: variant {name} is a string, a number or a document")
        out[str(name)] = value
    if len(out) < 2:
        raise ValueError(f"flag {key}: a flag with variants declares at least two")
    return out


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
        # An optional extension that is off still declares its flags: the
        # resolver turns them off (ADR-254), and the switchboard says why.
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


def prerequisite_problems(flags: Mapping[str, FlagDef]) -> list[str]:
    """Every prerequisite cycle among ``flags``, each named by its path (LOOP-F20)."""
    from .targeting import find_cycle

    graph = {k: [r.flag for r in f.requires] for k, f in flags.items()}
    problems: list[str] = []
    seen: set[str] = set()
    for key in sorted(graph):
        cycle = find_cycle(key, graph)
        if cycle and not (set(cycle) <= seen):
            seen |= set(cycle)
            problems.append(f"flags form a prerequisite cycle: {' -> '.join(cycle)}")
    return problems


def build_registry(flags: Iterable[FlagDef]) -> dict[str, FlagDef]:
    """Key the declarations (the first of a key wins) and refuse every flag on a
    prerequisite cycle, naming the cycle. A flag that requires a refused or undeclared flag
    stays: its prerequisite is simply never met (it resolves off, and explain says why)."""
    from .targeting import find_cycle

    found: dict[str, FlagDef] = {}
    for flag in flags:
        if flag.key in found:
            if found[flag.key].owner != flag.owner:
                log.warning(
                    "flag %s is declared by both %s and %s; %s wins",
                    flag.key, found[flag.key].owner, flag.owner, found[flag.key].owner,
                )
            continue
        found[flag.key] = flag
    graph = {k: [r.flag for r in f.requires] for k, f in found.items()}
    refused: set[str] = set()
    for key in sorted(graph):
        if key in refused:
            continue
        cycle = find_cycle(key, graph)
        if cycle:
            on_cycle = cycle[cycle.index(cycle[-1]):]
            log.warning("refusing flags %s: they form a prerequisite cycle: %s",
                        ", ".join(sorted(set(on_cycle))), " -> ".join(on_cycle))
            refused |= set(on_cycle)
    return {k: f for k, f in found.items() if k not in refused}


def load_registry(search_dirs: Iterable[Path] = (), *, entry_points: bool = True) -> dict[str, FlagDef]:
    """Every declared flag, keyed by flag key. The first declaration of a key wins,
    in discovery order (project, user, builtin, then entry points); a prerequisite cycle
    is refused (:func:`build_registry`)."""
    return build_registry(_manifest_flags(search_dirs) + (_entry_point_flags() if entry_points else []))


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
    "FLAG_KINDS",
    "OWNER_RE",
    "UNIT_RE",
    "VARIANT_RE",
    "ApprovalPolicy",
    "Prerequisite",
    "build_registry",
    "prerequisite_problems",
    "BUCKET_SUBJECTS",
    "DEFAULT_BUCKET_BY",
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
