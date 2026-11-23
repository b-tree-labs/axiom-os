# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The attribute catalogue cohort rules read (LOOP-F3, ADR-257 D5).

The platform declares the common attributes. Each has a resolver that reads only what
the platform can actually know about a subject; where it knows nothing the attribute is
UNKNOWN, and UNKNOWN never admits (:mod:`.targeting`).

| Attribute | Resolved from |
|---|---|
| ``principal`` | the subject's principal (the user id) |
| ``principal_kind`` | ``person`` or ``agent`` when the caller's surface knows it (a web session is a person); else UNKNOWN |
| ``role`` | the caller's roles when the surface knows them (a web session's roles); else UNKNOWN |
| ``account`` | the subject's account |
| ``account_kind`` | no platform resolver: UNKNOWN until an extension declares one |
| ``site`` | ``$AXIOM_SITE`` |
| ``node`` | the node the caller resolves on |
| ``node_role`` | this node's role (ADR-164), UNKNOWN when it has none |
| ``environment`` | the deployment environment |
| ``locale`` | the caller's locale when the surface knows it (the browser's language); else UNKNOWN |
| ``timezone``, ``country``, ``region`` | the principal's, when the surface knows them (a web session's OIDC ``zoneinfo`` and ``address`` claims; a CLI or MCP caller states them); else UNKNOWN (LOOP-F15) |
| ``site_timezone``, ``site_country``, ``site_region`` | the site's configuration: ``$AXIOM_SITE_TZ`` or the ``site.timezone`` setting, and the ``site.country`` / ``site.region`` settings |
| ``node_timezone``, ``node_country``, ``node_region`` | this node's file, ``[settings]`` ``node.timezone`` / ``node.country`` / ``node.region``; UNKNOWN for another node |
| ``local_timezone`` | the subject's zone: the principal's, else the node's, else the site's |
| ``local_hour`` | the time of day in that zone as decimal hours (09:30 is 9.5) |
| ``day_of_week`` | ``mon`` to ``sun`` in that zone |

A time zone is read through the tz database (``zoneinfo``): a name it does not know is
UNKNOWN, never UTC. Nothing here invents a value: an unset key is UNKNOWN.

An extension adds attributes in its manifest::

    [[flag_attributes]]
    name = "plan_tier"
    description = "The account's plan"
    resolver = "my_ext.attributes:plan_tier"   # callable(subject) -> value | None

A declared name that a platform attribute already has is skipped (the platform's wins),
as is a malformed declaration; a resolver that fails reads UNKNOWN.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.governance.roles import with_aliases

from .targeting import Attr, zone

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MANIFEST_KEY = "flag_attributes"


@dataclass(frozen=True)
class Subject:
    """Who a flag is resolved for. ``None`` means "not known here", never "empty"."""

    principal: str | None = None
    account: str | None = None
    node: str | None = None
    roles: tuple[str, ...] | None = None
    locale: str | None = None
    principal_kind: str | None = None
    #: The principal's IANA zone, ISO country and region, when the surface knows them (LOOP-F15).
    timezone: str | None = None
    country: str | None = None
    region: str | None = None
    #: The instant the flag is resolved for; local time of day is read at it.
    at: datetime | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "account": self.account,
            "node": self.node,
            "roles": list(self.roles) if self.roles is not None else None,
            "locale": self.locale,
            "principal_kind": self.principal_kind,
            "timezone": self.timezone,
            "country": self.country,
            "region": self.region,
            "local_timezone": _local_timezone(self),
            "site_time": _site_time(self),
            "site_weekday": _site_weekday(self),
            "my_time": _my_time(self),
            "my_weekday": _my_weekday(self),
            # Deprecated names, read as site time (Q13).
            "local_hour": _local_hour(self),
            "day_of_week": _day_of_week(self),
        }


Resolver = Callable[[Subject], Any]


@dataclass(frozen=True)
class AttributeDef:
    name: str
    description: str = ""
    #: ``None``: declared, but nothing can resolve it here (always UNKNOWN).
    resolver: Resolver | str | None = None
    #: The extension that declared it; ``""`` for a platform attribute.
    owner: str = ""
    #: The values a builder picks from (owner decision Q12): a list, or ``fn(query, limit)`` giving
    #: ``[value | {value, label}]``. ``None``: a platform attribute names its source in
    #: ``vocabulary.SOURCES``; an extension attribute without one is typed.
    values: tuple[Any, ...] | Callable[[str, int], Any] | None = None
    #: A retired name: the attribute to use instead (Q13). It still resolves, and is not offered.
    deprecated: str = ""

    def resolve(self, subject: Subject) -> Attr:
        fn = self.resolver
        if fn is None:
            return Attr(available=False)
        try:
            if isinstance(fn, str):
                fn = _load(fn)
            return Attr(available=True, value=fn(subject))
        except Exception as exc:  # noqa: BLE001 - a failing resolver is UNKNOWN, never a crash
            log.warning("attribute %s could not be resolved: %s", self.name, exc)
            return Attr(available=False)


def _load(entry: str) -> Resolver:
    module, _, attr = entry.partition(":")
    if not attr:
        raise ValueError(f"resolver {entry!r} is not 'module:function'")
    return getattr(importlib.import_module(module), attr)


def _node_role(_: Subject) -> str | None:
    from axiom.infra import node_functions as nf

    return nf.load().role or None


def _environment(_: Subject) -> str:
    from .flag_registry import get_environment

    return get_environment()


# ------------------------------------------------------------------ geography and time (LOOP-F15)

#: The settings a site declares its place in (``axi settings set site.country DE``).
SITE_SETTINGS: tuple[str, ...] = ("site.timezone", "site.country", "site.region")
#: The keys a node declares its own place under, in its node file's ``[settings]``.
NODE_SETTINGS: tuple[str, ...] = ("node.timezone", "node.country", "node.region")
SITE_TZ_ENV = "AXIOM_SITE_TZ"
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_site_settings: Mapping[str, Any] | None = None


def set_site_settings(values: Mapping[str, Any] | None) -> None:
    """Pin the site settings this module reads (tests), or ``None`` to read the settings store."""
    global _site_settings
    _site_settings = None if values is None else dict(values)


def _site_setting(key: str) -> Any:
    if _site_settings is not None:
        return _site_settings.get(key)
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return SettingsStore().get(key)
    except Exception:  # noqa: BLE001 - no settings store is "not configured", which is UNKNOWN
        return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _zone_name(value: Any) -> str | None:
    name = _text(value)
    return name if name and zone(name) is not None else None


def _site_timezone(_: Subject) -> str | None:
    return _zone_name(os.environ.get(SITE_TZ_ENV) or _site_setting("site.timezone"))


def _site_place(key: str) -> Resolver:
    return lambda _: _text(_site_setting(key))


def _this_node() -> str | None:
    try:
        from axiom.infra.topology import this_node

        return this_node()["node"]
    except Exception:  # noqa: BLE001
        return None


def _node_setting(subject: Subject, key: str) -> Any:
    """``key`` from THIS node's file; a subject on another node reads UNKNOWN."""
    if subject.node and subject.node != _this_node():
        return None
    from axiom.infra import node_functions as nf

    return (nf.load().settings or {}).get(key)


def _node_timezone(s: Subject) -> str | None:
    return _zone_name(_node_setting(s, "node.timezone"))


def _node_place(key: str) -> Resolver:
    return lambda s: _text(_node_setting(s, key))


def _safely(fn: Resolver, s: Subject) -> Any:
    try:
        return fn(s)
    except Exception:  # noqa: BLE001 - one source failing leaves the next to answer
        return None


def _local_timezone(s: Subject) -> str | None:
    """The subject's zone: the principal's, else this node's, else the site's."""
    return (_zone_name(s.timezone) or _safely(_node_timezone, s) or _safely(_site_timezone, s))


def _now_in(s: Subject, zone_name: str | None) -> datetime | None:
    tz = zone(zone_name)
    if tz is None:
        return None
    return (s.at or datetime.now(UTC)).astimezone(tz)


def _hours(now: datetime | None) -> float | None:
    return None if now is None else round(now.hour + now.minute / 60 + now.second / 3600, 4)


def _weekday(now: datetime | None) -> str | None:
    return None if now is None else _DAYS[now.weekday()]


def _site_time(s: Subject) -> float | None:
    """The time of day at the site, in decimal hours: the site's zone, nothing else (Q13)."""
    return _hours(_now_in(s, _safely(_site_timezone, s)))


def _site_weekday(s: Subject) -> str | None:
    return _weekday(_now_in(s, _safely(_site_timezone, s)))


def _my_time(s: Subject) -> float | None:
    """The time of day on the person's own clock: their zone, nothing else (Q13)."""
    return _hours(_now_in(s, _zone_name(s.timezone)))


def _my_weekday(s: Subject) -> str | None:
    return _weekday(_now_in(s, _zone_name(s.timezone)))


#: The retired time names read the site's clock (Q13); the fallback chain no longer decides.
_local_hour = _site_time
_day_of_week = _site_weekday

#: Attributes that read the site's clock, and those that read the person's.
SITE_CLOCK = frozenset({"site_time", "site_weekday", "local_hour", "day_of_week", "local_time", "weekday"})
PERSON_CLOCK = frozenset({"my_time", "my_weekday"})
NO_SITE_ZONE = ("This rule reads the site's clock, and this site has no time zone: set site.timezone (or "
                "$AXIOM_SITE_TZ), or use my_time / my_weekday for the person's own clock.")


def rule_attrs(rule: Any) -> set[str]:
    """Every attribute a rule (or a list of rollout rules) names, at any depth."""
    out: set[str] = set()
    if isinstance(rule, dict):
        if isinstance(rule.get("attr"), str):
            out.add(rule["attr"])
        for v in rule.values():
            out |= rule_attrs(v)
    elif isinstance(rule, list):
        for v in rule:
            out |= rule_attrs(v)
    return out


def check_site_clock(rules: Any) -> None:
    """Refuse a rule that reads the site's clock while the site has no time zone (Q13)."""
    from .targeting import RuleError

    if rule_attrs(rules) & SITE_CLOCK and _safely(_site_timezone, Subject()) is None:
        raise RuleError(NO_SITE_ZONE)


def clocks(rules: Any, cohorts: Mapping[str, Mapping[str, Any]], subject: Subject) -> list[dict[str, Any]]:
    """Which clock each time attribute a flag's rules read (directly or through a cohort) uses, and
    its zone, for explain (Q13)."""
    from .targeting import cohort_refs

    names, seen, todo = set(rule_attrs(rules)), set(), set(cohort_refs_of(rules, cohort_refs))
    while todo:
        key = todo.pop()
        if key in seen or key not in cohorts:
            continue
        seen.add(key)
        names |= rule_attrs(cohorts[key].get("rules"))
        todo |= cohort_refs(cohorts[key].get("rules"))
    out = []
    for name in sorted(names & (SITE_CLOCK | PERSON_CLOCK)):
        person = name in PERSON_CLOCK
        row = {"attribute": name, "clock": "person" if person else "site",
               "zone": _zone_name(subject.timezone) if person else _safely(_site_timezone, subject)}
        target = _RETIRED.get(name)
        if target:
            row["note"] = f"{name} is deprecated: it reads the site's clock; use {target}"
        out.append(row)
    return out


def cohort_refs_of(rules: Any, refs: Callable[[Any], set[str]]) -> set[str]:
    out: set[str] = set()
    for r in rules if isinstance(rules, list) else [rules]:
        if isinstance(r, dict):
            if isinstance(r.get("cohort"), str):
                out.add(r["cohort"])
            out |= refs(r.get("match") if "match" in r else r)
    return out


_RETIRED = {"local_hour": "site_time", "local_time": "site_time", "day_of_week": "site_weekday",
            "weekday": "site_weekday"}


PLATFORM_ATTRIBUTES: tuple[AttributeDef, ...] = (
    AttributeDef("principal", "The person or agent a flag is resolved for", lambda s: s.principal),
    AttributeDef("principal_kind", "person or agent, when the calling surface knows it",
                 lambda s: s.principal_kind),
    # With retired ids beside current ones, so a stored rule naming ``operator`` and one
    # naming ``site-admin`` both match the same holder (ADR-261).
    AttributeDef("role", "The caller's roles, when the calling surface knows them",
                 lambda s: list(with_aliases(s.roles)) if s.roles is not None else None),
    AttributeDef("account", "The account the caller acts in", lambda s: s.account),
    AttributeDef("account_kind", "The kind of account; no platform resolver, so an extension declares one"),
    AttributeDef("site", "The site this node serves ($AXIOM_SITE)",
                 lambda s: os.environ.get("AXIOM_SITE") or None),
    AttributeDef("node", "The node the flag is resolved on", lambda s: s.node),
    AttributeDef("node_role", "This node's role (ADR-164)", _node_role),
    AttributeDef("environment", "The deployment environment", _environment),
    AttributeDef("locale", "The caller's locale, when the calling surface knows it", lambda s: s.locale),
    AttributeDef("timezone", "The principal's IANA time zone, when the calling surface knows it",
                 lambda s: _zone_name(s.timezone)),
    AttributeDef("country", "The principal's country (ISO 3166-1), when the calling surface knows it",
                 lambda s: _text(s.country)),
    AttributeDef("region", "The principal's region (ISO 3166-2), when the calling surface knows it",
                 lambda s: _text(s.region)),
    AttributeDef("site_timezone", "The site's IANA time zone ($AXIOM_SITE_TZ or site.timezone)", _site_timezone),
    AttributeDef("site_country", "The site's country (the site.country setting)", _site_place("site.country")),
    AttributeDef("site_region", "The site's region (the site.region setting)", _site_place("site.region")),
    AttributeDef("node_timezone", "This node's IANA time zone (node.timezone in its node file)", _node_timezone),
    AttributeDef("node_country", "This node's country (node.country in its node file)",
                 _node_place("node.country")),
    AttributeDef("node_region", "This node's region (node.region in its node file)", _node_place("node.region")),
    AttributeDef("local_timezone", "The subject's zone: the principal's, else the node's, else the site's",
                 _local_timezone),
    AttributeDef("site_time", "The time of day at the site, in decimal hours (09:30 is 9.5); the site's time zone",
                 _site_time),
    AttributeDef("site_weekday", "The day of the week at the site, mon to sun; the site's time zone", _site_weekday),
    AttributeDef("my_time", "The time of day on the person's own clock, in decimal hours; their time zone",
                 _my_time),
    AttributeDef("my_weekday", "The day of the week on the person's own clock, mon to sun", _my_weekday),
    # Retired names (Q13): they still resolve, as site time, and are not offered.
    *(AttributeDef(old, f"Deprecated: use {new} (it reads the site's clock)",
                   _site_time if new == "site_time" else _site_weekday, deprecated=new)
      for old, new in (("local_hour", "site_time"), ("local_time", "site_time"),
                       ("day_of_week", "site_weekday"), ("weekday", "site_weekday"))),
)


def parse_manifest_attributes(manifest: Mapping[str, Any], *, owner: str) -> list[AttributeDef]:
    """The ``[[flag_attributes]]`` of a parsed manifest. Malformed entries are skipped and logged;
    a platform attribute's name is never taken over."""
    platform = {a.name for a in PLATFORM_ATTRIBUTES}
    out: list[AttributeDef] = []
    for raw in manifest.get(MANIFEST_KEY) or ():
        try:
            name = str(raw.get("name") or "").strip()
            resolver = str(raw.get("resolver") or "").strip()
        except AttributeError:
            continue
        if not NAME_RE.match(name):
            log.warning("skipping flag attribute %r from %s: not a lowercase identifier", name, owner)
            continue
        if not resolver or ":" not in resolver:
            log.warning("skipping flag attribute %s from %s: resolver must be 'module:function'", name, owner)
            continue
        if name in platform:
            log.warning("flag attribute %s is a platform attribute; %s's declaration is ignored", name, owner)
        listed = raw.get("values")
        values: Any = None
        if isinstance(listed, list | tuple):
            values = tuple(str(v) for v in listed)
        elif isinstance(listed, str) and ":" in listed:
            values = _lazy(listed)
        out.append(AttributeDef(name=name, description=str(raw.get("description") or ""),
                                resolver=resolver, owner=owner, values=values))
    return out


def _lazy(entry: str) -> Callable[[str, int], Any]:
    """A manifest's ``values = "module:function"``, loaded when it is first asked."""
    return lambda query, limit: _load(entry)(query, limit)


def _discover(search_dirs: Iterable[Path] = ()) -> list[AttributeDef]:
    from axiom.extensions.discovery import discover_extensions

    out: list[AttributeDef] = []
    for ext in discover_extensions(*search_dirs):
        try:
            manifest = tomllib.loads(ext.manifest_path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        out.extend(parse_manifest_attributes(manifest, owner=ext.name))
    return out


_extension_attrs: list[AttributeDef] | None = None
_pinned: dict[str, AttributeDef] | None = None


def set_extension_attributes(defs: Iterable[AttributeDef] | None) -> None:
    """Pin the extension-declared attributes (tests), or ``None`` to discover again."""
    global _extension_attrs
    _extension_attrs = None if defs is None else list(defs)


def set_catalogue(defs: Iterable[AttributeDef] | None) -> None:
    """Pin the whole catalogue (tests), or ``None`` for platform plus extensions."""
    global _pinned
    _pinned = None if defs is None else {d.name: d for d in defs}


def catalogue() -> dict[str, AttributeDef]:
    """Every attribute a rule may name: the platform's, then each extension's (first wins)."""
    if _pinned is not None:
        return dict(_pinned)
    global _extension_attrs
    if _extension_attrs is None:
        try:
            _extension_attrs = _discover()
        except Exception as exc:  # noqa: BLE001 - discovery failing leaves the platform attributes
            log.warning("flag attributes could not be discovered: %s", exc)
            _extension_attrs = []
    out = {a.name: a for a in PLATFORM_ATTRIBUTES}
    for a in _extension_attrs:
        out.setdefault(a.name, a)
    return out


def lookup_for(subject: Subject, cat: Mapping[str, AttributeDef] | None = None) -> Callable[[str], Attr]:
    """A memoised ``name -> Attr`` lookup for one subject (one request)."""
    cat = catalogue() if cat is None else cat
    memo: dict[str, Attr] = {}

    def look(name: str) -> Attr:
        if name not in memo:
            d = cat.get(name)
            memo[name] = d.resolve(subject) if d is not None else Attr(available=False)
        return memo[name]

    return look


__all__ = [
    "NODE_SETTINGS",
    "SITE_SETTINGS",
    "set_site_settings",
    "MANIFEST_KEY",
    "PLATFORM_ATTRIBUTES",
    "AttributeDef",
    "Subject",
    "catalogue",
    "lookup_for",
    "parse_manifest_attributes",
    "set_catalogue",
    "set_extension_attributes",
]
