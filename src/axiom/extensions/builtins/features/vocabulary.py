# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a rule builder offers for each attribute (owner decision Q12, SW-A26).

Every attribute in the catalogue has a group (Person, Place, Time, Cohort), a label, and a
value source: where the builder's list of values comes from. A value is picked from a known
list and typed only where no list exists (``source`` is ``None``):

* ``roles``: the role table (``axiom.governance.roles``);
* ``sites``: the site configuration (the sites the node knows; its region and country);
* ``tz``: the tz database;
* ``cohorts``: the stored cohorts;
* ``directory``: people and accounts the directory and this node know;
* ``weekdays`` and ``kinds``: fixed lists;
* ``extension``: an extension attribute's own list (``AttributeDef.values``).

Local time has no list. Reads only; nothing here writes.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

#: The pseudo-attribute a rule uses to name another cohort.
IN_COHORT = "in_cohort"

PLACE = frozenset({"site", "node", "node_role", "environment", "site_timezone", "site_country", "site_region",
                   "node_timezone", "node_country", "node_region", "local_timezone"})
TIME = frozenset({"site_time", "site_weekday", "my_time", "my_weekday", "local_hour", "day_of_week",
                  "local_time", "weekday"})

#: The platform attributes' value sources.
SOURCES: dict[str, str] = {
    "role": "roles",
    "site": "sites", "site_country": "sites", "site_region": "sites", "country": "sites", "region": "sites",
    "node_country": "sites", "node_region": "sites",
    "timezone": "tz", "site_timezone": "tz", "node_timezone": "tz", "local_timezone": "tz",
    IN_COHORT: "cohorts",
    "principal": "directory", "account": "directory",
    "site_weekday": "weekdays", "my_weekday": "weekdays", "day_of_week": "weekdays", "weekday": "weekdays",
    "principal_kind": "kinds",
}

_LABELS = {"principal": "Person", "principal_kind": "Person or agent", "node_role": "Node role",
           "site_time": "Site time", "site_weekday": "Site weekday", "my_time": "Your time",
           "my_weekday": "Your weekday", IN_COHORT: "In cohort",
           "timezone": "Time zone", "site_timezone": "Site time zone", "node_timezone": "Node time zone",
           "local_timezone": "Local time zone"}


def group_of(name: str) -> str:
    if name == IN_COHORT:
        return "Cohort"
    if name in TIME:
        return "Time"
    if name in PLACE:
        return "Place"
    return "Person"


def label_of(name: str) -> str:
    return _LABELS.get(name) or name.replace("_", " ").capitalize()


def source_of(name: str, attr: Any = None) -> str | None:
    if attr is not None and getattr(attr, "values", None) is not None:
        return "extension"
    return SOURCES.get(name)


def rows() -> list[dict[str, Any]]:
    """Every attribute a rule may name, as the builder's menu shows it."""
    from .attributes import catalogue

    out = []
    for name, attr in catalogue().items():
        if attr.deprecated:
            continue  # a retired name still resolves; the builder offers its successor
        out.append({"name": name, "label": label_of(name), "group": group_of(name),
                    "description": attr.description, "available": attr.resolver is not None,
                    "has_values": source_of(name, attr) is not None})
    out.append({"name": IN_COHORT, "label": label_of(IN_COHORT), "group": "Cohort",
                "description": "Members of another cohort", "available": True, "has_values": True})
    return out


def _pairs(values: Any) -> list[dict[str, str]]:
    out = []
    for v in values or ():
        if isinstance(v, dict):
            out.append({"value": str(v.get("value")), "label": str(v.get("label") or v.get("value"))})
        else:
            out.append({"value": str(v), "label": str(v)})
    return out


def _roles(_: str) -> list[dict[str, str]]:
    from axiom.governance.roles import DISPLAY_NAMES

    return [{"value": r, "label": label} for r, label in DISPLAY_NAMES.items()]


def _sites(name: str) -> list[dict[str, str]]:
    from . import attributes

    if name in ("site",):
        known = set()
        try:
            from axiom.infra.site_scope import deployment_sites

            known |= set(deployment_sites() or ())
        except Exception:  # noqa: BLE001
            pass
        if os.environ.get("AXIOM_SITE"):
            known.add(os.environ["AXIOM_SITE"])
        return _pairs(sorted(known))
    aspect = "country" if name.endswith("country") else "region"
    value = attributes._site_setting(f"site.{aspect}")
    return _pairs([value] if value else [])


def _zones(_: str) -> list[dict[str, str]]:
    from zoneinfo import available_timezones

    return _pairs(sorted(z for z in available_timezones() if "/" in z))


def _cohorts(_: str) -> list[dict[str, str]]:
    from .skills._flags import _service

    try:
        stored = _service().targeting.cohorts()
    except Exception:  # noqa: BLE001 - no cohort store lists none
        return []
    return [{"value": k, "label": str(c.get("name") or k)} for k, c in sorted(stored.items())]


def _directory(name: str) -> list[dict[str, str]]:
    from . import asks
    from .skills._flags import _service
    from .skills._loop import _population

    seen: set[str] = set()
    try:
        subjects, _ = _population(_service())
    except Exception:  # noqa: BLE001
        subjects = []
    if name == "account":
        seen |= {a for _, a in subjects if a}
    else:
        seen |= {p for p, _ in subjects if p}
        seen |= {a["principal"] for a in asks.site_admins()}
    return _pairs(sorted(seen))


_FIXED = {"weekdays": ("mon", "tue", "wed", "thu", "fri", "sat", "sun"), "kinds": ("person", "agent")}
_READERS: dict[str, Callable[[str], list[dict[str, str]]]] = {
    "roles": _roles, "sites": _sites, "tz": _zones, "cohorts": _cohorts, "directory": _directory,
}


class UnknownAttribute(LookupError):
    pass


def values(name: str, query: str = "", limit: int = 50) -> dict[str, Any]:
    """``{attribute, source, values: [{value, label}], more}`` for one attribute; ``source`` is
    ``None`` when there is no list, so the value is typed. ``query`` keeps the values whose value
    or label contains it (any case)."""
    from .attributes import catalogue

    cat = catalogue()
    if name != IN_COHORT and name not in cat:
        raise UnknownAttribute(f"no attribute {name!r}; `flags attributes` names them")
    attr = cat.get(name)
    source = source_of(name, attr)
    limit = max(1, min(int(limit or 50), 500))
    note = {"note": f"{name} is deprecated: it reads the site's clock; use {attr.deprecated}"} if (
        attr is not None and attr.deprecated) else {}
    if source is None:
        return {"attribute": name, "source": None, "values": [], "more": False, **note}
    q = str(query or "").strip().lower()
    if source == "extension":
        declared = attr.values
        found = _pairs(declared(q, limit + 1) if callable(declared) else declared)
    elif source in _FIXED:
        found = _pairs(_FIXED[source])
    else:
        found = _READERS[source](name)
    if q:
        found = [v for v in found if q in v["value"].lower() or q in v["label"].lower()]
    return {"attribute": name, "source": source, "values": found[:limit], "more": len(found) > limit, **note}


__all__ = ["IN_COHORT", "SOURCES", "UnknownAttribute", "group_of", "label_of", "rows", "source_of", "values"]
