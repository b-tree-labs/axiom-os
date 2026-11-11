# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Publish from gold into the app database: the registry, freshness and invalidation (MVP-11, W13).

The web app keeps its own relational database (one schema per extension,
ADR-052). It holds two kinds of table and only two:

1. **Published from gold**: a projection of a gold object, for what a page
   needs fast. Written by the publish step only.
2. **Application state**: settings, drafts, layouts, saved views, labels a
   person gave a channel. Written by the app only, and never touched by a
   publish or a rebuild.

Domain data reaches people from gold, read directly or published here; **silver
never serves**, directly or through this database. So a publication names
exactly one gold object, and registering one whose source is not in the gold
schema is refused (MVP-11-A4).

Every publish records, in ``publication_state``, the gold object, the gold
version the rows came from, when, and the freshness the publication declares.
That one row per table is what lets a page say "as of" with the lag when the
table is behind (MVP-11-A5), and what makes a refresh cheap: when gold's
revision has not moved, there is nothing to do (invalidation on gold change).
Dropping and rebuilding every publication gives the same rows and leaves
application state alone (MVP-11-A6).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

#: The only schema a publication may read from.
GOLD_SCHEMA = "gold"

_QUALIFIED = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,62})\.([A-Za-z_][A-Za-z0-9_]{0,62})$")

#: Application state in this schema (written by the app, never by a publish).
#: Every table in the schema is either a publication, application state, or
#: the publication ledger itself; a test holds the schema to that.
APP_STATE_TABLES: frozenset[str] = frozenset({"saved_view", "channel_label"})
LEDGER_TABLE = "publication_state"

Invalidation = Literal["gold_change", "schedule", "both"]


class SilverNeverServes(ValueError):
    """A publication named a source outside gold."""


@dataclass(frozen=True)
class Publication:
    """One gold object published into the app database (spec-site-view, addendum 2026-10-30).

    ``gold_version`` is not declared here: it is what each publish *recorded*,
    in ``publication_state``.
    """

    gold_object: str
    app_table: str
    freshness: timedelta
    invalidate_on: Invalidation
    #: ``(session, full)`` -> None: write the projection from ``gold_object``.
    project: Callable[[Any, bool], None]
    #: ``(session)`` -> None: remove every published row (the rebuild's first half).
    clear: Callable[[Any], None]
    #: ``(session)`` -> gold's own revision for this object, or ``None`` when gold keeps none.
    gold_revision: Callable[[Any], str | None] = lambda session: None
    #: ``(session)`` -> the watermark the published rows reached (the version when gold keeps no revision).
    watermark: Callable[[Any], str | None] = lambda session: None


def require_gold(relation: str) -> str:
    """The relation, if it is a gold object; :class:`SilverNeverServes` otherwise."""
    m = _QUALIFIED.match(relation or "")
    if not m or m.group(1).lower() != GOLD_SCHEMA:
        raise SilverNeverServes(
            f"{relation!r} is not a gold object. The app database is published from gold only; "
            "silver never serves. If gold lacks what the page needs, add a gold object."
        )
    return relation


_REGISTRY: dict[str, Publication] = {}


def register(publication: Publication) -> Publication:
    require_gold(publication.gold_object)
    if publication.app_table in APP_STATE_TABLES or publication.app_table == LEDGER_TABLE:
        raise ValueError(f"{publication.app_table!r} is application state; a publish never writes it")
    if publication.freshness <= timedelta(0):
        raise ValueError("a publication declares a positive freshness")
    _REGISTRY[publication.app_table] = publication
    return publication


def publications() -> dict[str, Publication]:
    _ensure_builtin()
    return dict(_REGISTRY)


def get(app_table: str) -> Publication:
    _ensure_builtin()
    try:
        return _REGISTRY[app_table]
    except KeyError:
        raise LookupError(f"no publication into {app_table!r}") from None


# ------------------------------------------------------------------- the ledger


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _record(session, pub: Publication, *, version: str, kind: str, rows: int, now: datetime) -> None:
    from axiom.extensions.builtins.webapp.catalog.models import PublicationState

    row = session.get(PublicationState, pub.app_table)
    if row is None:
        row = PublicationState(app_table=pub.app_table)
        session.add(row)
    row.gold_object = pub.gold_object
    row.gold_version = version
    row.version_kind = kind
    row.rows = rows
    row.freshness_s = int(pub.freshness.total_seconds())
    row.invalidate_on = pub.invalidate_on
    row.published_at = now
    session.commit()


def _count(session, table: str) -> int:
    from sqlalchemy import text

    # nosemgrep: avoid-sqlalchemy-text -- table is a registered app table, never input
    return int(session.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


def publish(session, app_table: str, *, full: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Run one publication and record the gold version its rows came from."""
    pub = get(app_table)
    pub.project(session, full)
    revision = pub.gold_revision(session)
    if revision:
        version, kind = revision, "revision"
    else:
        version, kind = pub.watermark(session) or "", "watermark"
    now = now or _now()
    rows = _count(session, pub.app_table)
    _record(session, pub, version=version, kind=kind, rows=rows, now=now)
    return {"app_table": pub.app_table, "gold_object": pub.gold_object, "gold_version": version,
            "version_kind": kind, "rows": rows, "published_at": now.isoformat(), "full": full}


def refresh(session, app_table: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Publish only when gold moved (or gold keeps no revision to ask): invalidation on gold change."""
    from axiom.extensions.builtins.webapp.catalog.models import PublicationState

    pub = get(app_table)
    state = session.get(PublicationState, pub.app_table)
    revision = pub.gold_revision(session)
    if state is not None and revision and state.gold_version == revision and state.version_kind == "revision":
        # Gold has not moved since the last publish: the rows are current.
        return {"app_table": pub.app_table, "gold_version": revision, "unchanged": True}
    return publish(session, app_table, now=now)


def rebuild(session, app_table: str | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Drop the published rows and publish them again from gold. Application state is not touched."""
    out = []
    for name in ([app_table] if app_table else sorted(publications())):
        pub = get(name)
        pub.clear(session)
        session.commit()
        out.append(publish(session, name, full=True, now=now))
    return out


# ------------------------------------------------------------------- freshness


def _lag_words(lag: timedelta) -> str:
    s = int(lag.total_seconds())
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def freshness(session, app_table: str, *, now: datetime | None = None) -> dict[str, Any]:
    """How current one published table is, and what a page says about it (MVP-11-A5).

    When gold keeps a revision and the table carries it, the table is current
    whatever the clock says. Otherwise the lag is the time since the last
    publish; past the declared freshness the page shows "as of" with the lag.
    A table never published is stale by definition.
    """
    from axiom.extensions.builtins.webapp.catalog.models import PublicationState

    pub = get(app_table)
    now = now or _now()
    state = session.get(PublicationState, pub.app_table)
    declared = int(pub.freshness.total_seconds())
    if state is None or state.published_at is None:
        return {"app_table": pub.app_table, "gold_object": pub.gold_object, "gold_version": None,
                "published_at": None, "freshness_s": declared, "lag_s": None, "stale": True,
                "as_of": None, "note": "not yet published from gold"}
    published = _aware(state.published_at)
    revision = None
    try:
        revision = pub.gold_revision(session)
    except Exception:  # noqa: BLE001 - an unreadable revision falls back to the clock
        revision = None
    if revision and state.version_kind == "revision" and state.gold_version == revision:
        lag = timedelta(0)
    else:
        lag = max(timedelta(0), now - published)
    stale = lag.total_seconds() > declared
    note = f"as of {published.strftime('%Y-%m-%d %H:%M UTC')}, {_lag_words(lag)} behind" if stale else ""
    return {
        "app_table": pub.app_table,
        "gold_object": state.gold_object,
        "gold_version": state.gold_version,
        "version_kind": state.version_kind,
        "published_at": published.isoformat(),
        "freshness_s": declared,
        "lag_s": int(lag.total_seconds()),
        "stale": stale,
        "as_of": published.isoformat(),
        "note": note,
    }


def report(session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    return [freshness(session, name, now=now) for name in sorted(publications())]


# ------------------------------------------------------------------- the built-in publication


def _relation_exists(session, relation: str) -> bool:
    from sqlalchemy import inspect, text

    schema, table = relation.split(".", 1)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return bool(session.execute(text("SELECT to_regclass(:r) IS NOT NULL"), {"r": relation}).scalar())
    try:
        # SQLite (tests): an attached database plays the gold schema. The
        # inspector quotes the identifiers itself (a table or a view counts),
        # so no SQL is built from the relation's name.
        return bool(inspect(session.connection()).has_table(table, schema=schema))
    except Exception:  # noqa: BLE001 - no such schema
        return False


#: Conform keeps this per (site, feed, channel) and stamps every write; its
#: newest stamp is gold's revision (``data_platform.conformance.catalogue``).
GOLD_CATALOGUE = "gold.signal_catalogue"


def _catalogue_revision(session) -> str | None:
    from sqlalchemy import text

    if not _relation_exists(session, GOLD_CATALOGUE):
        return None
    # nosemgrep: avoid-sqlalchemy-text -- a fixed gold relation
    got = session.execute(text(f"SELECT max(computed_at) FROM {GOLD_CATALOGUE}")).scalar()
    if got is None:
        return None
    return got.isoformat() if hasattr(got, "isoformat") else str(got)


def _catalog_watermark(session) -> str | None:
    from sqlalchemy import text

    got = session.execute(text("SELECT max(last_ts) FROM site_catalog_channel")).scalar()
    return str(got) if got else None


def _catalog_project(session, full: bool) -> None:
    from axiom.extensions.builtins.webapp.catalog.sync import project_catalog

    project_catalog(session, source=CATALOG_SOURCE, full=full)


def _catalog_clear(session) -> None:
    from sqlalchemy import text

    session.execute(text("DELETE FROM site_catalog_channel"))


#: The gold object the channel catalog is projected from.
CATALOG_SOURCE = "gold.signals"
#: The catalog has been refreshed at fifteen-minute cadence since it shipped.
CATALOG_FRESHNESS = timedelta(minutes=15)


def _ensure_builtin() -> None:
    if "site_catalog_channel" in _REGISTRY:
        return
    register(Publication(
        gold_object=CATALOG_SOURCE,
        app_table="site_catalog_channel",
        freshness=CATALOG_FRESHNESS,
        invalidate_on="both",
        project=_catalog_project,
        clear=_catalog_clear,
        gold_revision=_catalogue_revision,
        watermark=_catalog_watermark,
    ))


def reset(*, builtin: bool = True) -> None:
    """Forget registered publications (tests); the built-in one comes back on next use."""
    _REGISTRY.clear()
    if builtin:
        _ensure_builtin()


__all__ = [
    "APP_STATE_TABLES",
    "CATALOG_FRESHNESS",
    "CATALOG_SOURCE",
    "GOLD_SCHEMA",
    "LEDGER_TABLE",
    "Publication",
    "SilverNeverServes",
    "freshness",
    "get",
    "publications",
    "publish",
    "rebuild",
    "refresh",
    "register",
    "report",
    "require_gold",
    "reset",
]
