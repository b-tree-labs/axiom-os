# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``ScheduledItem``: the read-only agenda over sources that already exist (MVP-24).

The agenda is a READ across features that own their own time-bound records. It
adds no event table and writes nothing to any source (CAL-A1): every provider
here only selects. Three generic providers ship with the platform:

- :class:`SchedulerProvider`: registered schedules, expanded to their fires in
  the window, plus the fire log for the part of the window already past.
- :class:`AttestObligationProvider`: attest obligations measured in a day or
  longer. A minute-scale obligation (a check every half hour) is a countdown,
  not a calendar item, and is left out (CAL-A5).
- :class:`VaultExpiryProvider`: credential ``expires_at`` and ``review_by``
  from the vault's metadata index. Metadata only; never a secret value.

Providers are a registry list (:func:`set_providers`) so a test or a product can
supply its own; a product adds providers, the platform never names it.

Every time is in SITE time with its zone labelled (CAL-A2). A date-only item
(an expiry recorded as ``YYYY-MM-DD``) is carried as that bare date and never
converted through any zone, so it reads the same for every viewer.

Field names follow ``calendar.protocol.EventSpec`` where they fit (``start``,
``end``, ``timezone``, ``status_line``, ``metadata``) with the PRD §22.3
additions (``id``, ``source``, ``kind``, ``owner``, ``status``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

#: The env var a deployment sets for its site zone (IANA name).
SITE_TZ_ENV = "AXIOM_SITE_TZ"
#: The setting the site zone is read from when the env var is unset.
SITE_TZ_SETTING = "site.timezone"

#: An obligation or recurrence shorter than this is minute-scale (CAL-A5).
ONE_DAY = timedelta(days=1)

#: Most occurrences one source may contribute to one window (spec-site-view).
MAX_PER_SOURCE = 500

KINDS = ("run", "obligation", "expiry")

DEFAULT_DAYS = 7
DUE_SOON_DAYS = 14


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - a missing brand is the default name
        return "axi"


# --------------------------------------------------------------------------- site time


def site_timezone() -> str:
    """The site's IANA zone: ``AXIOM_SITE_TZ``, else the ``site.timezone``
    setting, else ``UTC``. An unknown name falls back to UTC (and is logged)."""
    name = (os.environ.get(SITE_TZ_ENV) or "").strip()
    if not name:
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            name = str(SettingsStore().get(SITE_TZ_SETTING) or "").strip()
        except Exception:  # noqa: BLE001 - no settings store is "not configured"
            name = ""
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("site time zone %r is not an IANA zone; using UTC", name)
        return "UTC"
    return name


#: The generic name for a zone whose abbreviation alternates with daylight time.
_GENERIC = {
    "CST": "CT", "CDT": "CT", "EST": "ET", "EDT": "ET",
    "MST": "MT", "MDT": "MT", "PST": "PT", "PDT": "PT",
    "AKST": "AKT", "AKDT": "AKT", "HST": "HT", "HDT": "HT",
}


def tz_label(tz: str, at: datetime | None = None) -> str:
    """``"CT (UTC-5)"``: the zone's short name and its offset at ``at``."""
    zone = ZoneInfo(tz)
    when = (at or datetime.now(UTC)).astimezone(zone)
    offset = when.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    off = f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")
    if tz == "UTC":
        return "UTC"
    abbrev = when.tzname() or tz
    short = _GENERIC.get(abbrev, abbrev)
    if short.startswith(("+", "-")):  # zones without a letter abbreviation
        short = tz
    return f"{short} ({off})"


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored instant is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_site_iso(dt: datetime, tz: str) -> str:
    return _aware(dt).astimezone(ZoneInfo(tz)).isoformat()


# --------------------------------------------------------------------------- the item


@dataclass(frozen=True)
class ScheduledItem:
    """One read-only agenda item. ``start``/``end`` are ISO strings in site time
    (with offset), or, when ``date_only``, a bare ``YYYY-MM-DD``."""

    id: str
    title: str
    kind: str  # run | obligation | expiry
    source: str  # the extension that owns the record
    start: str
    end: str | None = None
    date_only: bool = False
    timezone: str = "UTC"
    status: str = "planned"  # planned | in_progress | done | missed | cancelled
    status_line: str | None = None  # one line of state in the source's own words
    owner: str | None = None
    owner_link: dict[str, str | None] = field(default_factory=dict)  # {"cli", "web"}
    addressed_to: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["addressed_to"] = list(self.addressed_to)
        return d


@dataclass(frozen=True)
class Window:
    """``[start, end)`` as aware instants, plus the site zone they are read in."""

    start: datetime
    end: datetime
    tz: str

    def contains(self, at: datetime) -> bool:
        return self.start <= _aware(at) < self.end

    def contains_date(self, d: date) -> bool:
        """A site-local date is in the window when its site-local day overlaps it."""
        zone = ZoneInfo(self.tz)
        day_start = datetime.combine(d, time.min, tzinfo=zone)
        return day_start < self.end and day_start + ONE_DAY > self.start


class Provider(Protocol):
    """One source of items. ``list`` must only read."""

    name: str

    def list(self, window: Window, *, now: datetime) -> list[ScheduledItem]: ...


# --------------------------------------------------------------------------- scheduler


def _duration(seconds: float) -> str:
    s = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size and s % size == 0:
            return f"{s // size}{unit}"
    return f"{s}s"


_OUTCOME_STATUS = {
    "success": "done", "succeeded": "done", "ok": "done",
    "pending": "in_progress", "running": "in_progress",
    "failed": "missed", "dead_letter": "missed", "skipped": "cancelled",
}


def _actor_of(envelope: Any) -> str | None:
    if not isinstance(envelope, dict):
        return None
    actor = envelope.get("actor")
    if isinstance(actor, str):
        return actor or None
    if isinstance(actor, dict):
        for key in ("principal", "id", "principal_id", "name"):
            if actor.get(key):
                return str(actor[key])
    return None


class SchedulerProvider:
    """Registered schedules expanded into the window, and their fire log."""

    name = "schedule"

    def __init__(self, *, max_per_definition: int = MAX_PER_SOURCE) -> None:
        self.max_per_definition = max_per_definition

    def list(self, window: Window, *, now: datetime) -> list[ScheduledItem]:
        from axiom.extensions.builtins.schedule import store
        from axiom.extensions.builtins.schedule.api import cadence_from_payload
        from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
        from axiom.extensions.builtins.schedule.db_models import (
            ScheduleDefinition,
            ScheduleFireLog,
        )

        items: list[ScheduledItem] = []
        with store.session_scope() as s:
            defs = (
                s.query(ScheduleDefinition)
                .filter(ScheduleDefinition.state == "active")
                .order_by(ScheduleDefinition.created_at)
                .all()
            )
            fires = (
                s.query(ScheduleFireLog)
                .filter(ScheduleFireLog.intended_fire_at >= window.start.astimezone(UTC))
                .filter(ScheduleFireLog.intended_fire_at < window.end.astimezone(UTC))
                .order_by(ScheduleFireLog.intended_fire_at)
                .all()
            ) if window.start <= now else []
            rows = [
                {
                    "id": d.id, "name": d.name, "action": d.action, "extension": d.extension,
                    "cadence_kind": d.cadence_kind, "cadence_payload": dict(d.cadence_payload or {}),
                    "next_fire_at": d.next_fire_at, "envelope": d.capability_envelope,
                }
                for d in defs
            ]
            fire_rows = [
                {"schedule_id": f.schedule_id, "at": f.intended_fire_at, "outcome": f.outcome,
                 "finished_at": f.finished_at}
                for f in fires
            ]
            # Read-only: nothing was added or changed, so leave the session clean.
            s.rollback()

        by_id = {r["id"]: r for r in rows}
        sub_day: set[str] = set()
        for r in rows:
            if r["next_fire_at"] is None:
                continue
            cadence = cadence_from_payload(r["cadence_kind"], r["cadence_payload"])
            fires_at: list[datetime] = []
            t: datetime | None = _aware(r["next_fire_at"])
            while t is not None and t < window.end and len(fires_at) <= self.max_per_definition:
                if t >= window.start:
                    fires_at.append(t)
                nxt = compute_next_fire_at(cadence, last_fire=t, now=t)
                if nxt is None or _aware(nxt) <= t:
                    break
                period = _aware(nxt) - t
                if period < ONE_DAY:
                    # A sub-day recurrence drawn as events is noise: one item
                    # stands for the series (its next fire in the window).
                    sub_day.add(r["id"])
                    if t < window.start:
                        t = t + period * -(-(window.start - t) // period)
                    if t < window.end:
                        fires_at = [t]
                    break
                t = _aware(nxt)
            for at in fires_at:
                items.append(self._item(r, at, window.tz, every=(r["id"] in sub_day), cadence=cadence))
        for f in fire_rows:
            r = by_id.get(f["schedule_id"])
            if r is None or r["id"] in sub_day:
                continue
            at = _aware(f["at"])
            if at >= now:
                continue
            status = _OUTCOME_STATUS.get(str(f["outcome"]), "done")
            items.append(self._item(r, at, window.tz, status=status, outcome=str(f["outcome"])))
        # A fire the log already holds is not also shown as planned.
        seen: dict[str, ScheduledItem] = {}
        for it in items:
            if it.id not in seen or it.status != "planned":
                seen[it.id] = it
        return list(seen.values())

    def _item(self, r: dict, at: datetime, tz: str, *, status: str = "planned",
              outcome: str | None = None, every: bool = False, cadence: Any = None) -> ScheduledItem:
        cli = _cli()
        line = f"last outcome: {outcome}" if outcome else "next fire"
        title = r["name"]
        meta: dict[str, Any] = {"schedule_id": r["id"], "action": r["action"],
                                "cadence_kind": r["cadence_kind"], "registered_by": r["extension"]}
        if every and cadence is not None and cadence.interval is not None:
            title = f"{title} (every {_duration(cadence.interval.total_seconds())})"
            meta["collapsed"] = True
        elif every:
            title = f"{title} (recurs more than once a day)"
            meta["collapsed"] = True
        return ScheduledItem(
            id=f"schedule:run:{r['id']}@{at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            title=title,
            kind="run",
            source=self.name,
            start=to_site_iso(at, tz),
            timezone=tz,
            status=status,
            status_line=line,
            owner=_actor_of(r["envelope"]),
            owner_link={"cli": f"{cli} schedule status {r['id']}", "web": None},
            metadata=meta,
        )


# --------------------------------------------------------------------------- attest


ObligationFetch = Callable[[datetime], Iterable[dict[str, Any]]]


def _log_handle(logbook_id: str) -> str:
    """The word a person types after ``log`` for this log (ADR-243); the id if it is unknown."""
    try:
        from axiom.extensions.builtins.attest import registry

        book = registry.get(logbook_id)
    except Exception:  # noqa: BLE001 - an undeclared log is named by its id
        return logbook_id
    return book.handle or book.id


def _attest_fetch(now: datetime) -> list[dict[str, Any]]:
    """Every open obligation at every site, each with its cadence in minutes.

    The attest engine's own ``evaluate`` (the read behind ``attest.obligations``)
    does the work; this only adds ``every_minutes`` so the agenda can drop the
    minute-scale ones. It reads; it records nothing (``tick`` is what records).
    """
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import obligations, registry, site_settings, store
    from axiom.extensions.builtins.attest.logbooks import LogbookError

    with store.session_scope() as s:
        open_ = s.execute(
            text("SELECT DISTINCT site_id, logbook FROM attest_intervals WHERE closed_at IS NULL")
        ).all()
    out: list[dict[str, Any]] = []
    for row in open_:
        try:
            decls = {ob.id: ob for ob in registry.get(row.logbook).obligations}
        except LogbookError:
            continue
        if not decls:
            continue
        every = {
            oid: int(site_settings.value(row.site_id, ob.every_key, ob.every_default))
            for oid, ob in decls.items()
        }
        if all(m < ONE_DAY.total_seconds() / 60 for m in every.values()):
            continue  # nothing here can reach the agenda; skip the evaluation
        for st in obligations.evaluate(row.site_id, row.logbook, now=now):
            out.append({**st, "every_minutes": every.get(st["obligation"], 0)})
    return out


_OBLIGATION_STATUS = {"ok": "planned", "warn": "planned", "missed": "missed"}


class AttestObligationProvider:
    """Attest obligations of a day or longer. Minute-scale ones are excluded."""

    name = "attest"

    def __init__(self, fetch: ObligationFetch | None = None) -> None:
        self._fetch = fetch or _attest_fetch

    def list(self, window: Window, *, now: datetime) -> list[ScheduledItem]:
        cli = _cli()
        items: list[ScheduledItem] = []
        for ob in self._fetch(now):
            if int(ob.get("every_minutes") or 0) < ONE_DAY.total_seconds() / 60:
                continue  # CAL-A5: a countdown, not a calendar item
            due = ob.get("due_at")
            if isinstance(due, str):
                due = datetime.fromisoformat(due)
            if not isinstance(due, datetime) or not window.contains(due):
                continue
            interval = ob.get("interval") or {}
            site, book, oid = ob.get("site_id"), ob.get("logbook"), ob.get("obligation")
            word = {"warn": "due soon", "missed": "missed", "ok": "due"}.get(ob.get("state"), "due")
            notify = tuple(str(r) for r in ob.get("notify") or ())
            items.append(
                ScheduledItem(
                    id=f"attest:obligation:{site}/{book}/{oid}/{interval.get('number', '')}",
                    title=f"{ob.get('type', oid)} ({book})",
                    kind="obligation",
                    source=self.name,
                    start=to_site_iso(due, window.tz),
                    timezone=window.tz,
                    status=_OBLIGATION_STATUS.get(str(ob.get("state")), "planned"),
                    status_line=f"{word}; every {_duration(int(ob['every_minutes']) * 60)}",
                    owner=None,
                    owner_link={"cli": f"{cli} log {_log_handle(book)} due --site {site}",
                                "web": None},
                    addressed_to=notify,
                    metadata={"site_id": site, "logbook": book, "obligation": oid,
                              "severity": ob.get("severity"), "interval": dict(interval)},
                )
            )
        return items


# --------------------------------------------------------------------------- vault


VaultFetch = Callable[[], Iterable[dict[str, Any]]]

#: The only credential fields the agenda ever reads. Values never; the
#: fingerprint never either.
_VAULT_FIELDS = ("name", "expires_at", "review_by", "owner", "provider")


def _vault_fetch(state_dir: Any = None) -> list[dict[str, Any]]:
    """The credential metadata index, metadata only. Opens no keychain."""
    from axiom.extensions.builtins.vault.index import CredentialIndex

    return [{k: r.get(k) for k in _VAULT_FIELDS} for r in CredentialIndex(state_dir=state_dir).rows()]


def _date_or_instant(raw: Any) -> tuple[date | None, datetime | None]:
    text = str(raw or "").strip()
    if not text:
        return None, None
    if len(text) == 10:
        try:
            return date.fromisoformat(text), None
        except ValueError:
            return None, None
    try:
        return None, _aware(datetime.fromisoformat(text))
    except ValueError:
        return None, None


class VaultExpiryProvider:
    """Credential expiry and review dates, from the vault's metadata index."""

    name = "vault"

    def __init__(self, fetch: VaultFetch | None = None, *, state_dir: Any = None) -> None:
        self._fetch = fetch or (lambda: _vault_fetch(state_dir))

    def list(self, window: Window, *, now: datetime) -> list[ScheduledItem]:
        cli = _cli()
        items: list[ScheduledItem] = []
        for raw in self._fetch():
            row = {k: raw.get(k) for k in _VAULT_FIELDS}
            name = str(row.get("name") or "")
            if not name:
                continue
            for fld, suffix, word in (("expires_at", "", "expires"), ("review_by", "#review", "review by")):
                d, at = _date_or_instant(row.get(fld))
                if d is not None:
                    if not window.contains_date(d):
                        continue
                    start, date_only = d.isoformat(), True
                    past = d < now.astimezone(ZoneInfo(window.tz)).date()
                elif at is not None:
                    if not window.contains(at):
                        continue
                    start, date_only = to_site_iso(at, window.tz), False
                    past = at <= now
                else:
                    continue
                expired = past and fld == "expires_at"
                owner = str(row["owner"]) if row.get("owner") else None
                items.append(
                    ScheduledItem(
                        id=f"vault:expiry:{name}{suffix}",
                        title=f"{name} {word}",
                        kind="expiry",
                        source=self.name,
                        start=start,
                        date_only=date_only,
                        timezone=window.tz,
                        status="missed" if expired else "planned",
                        status_line="expired" if expired else word,
                        owner=owner,
                        owner_link={"cli": f"{cli} vault list --json", "web": None},
                        addressed_to=(owner,) if owner else (),
                        metadata={"credential": name, "field": fld},
                    )
                )
        return items


# --------------------------------------------------------------------------- registry


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _system_clock


def set_clock(fn: Callable[[], datetime]) -> None:
    """Seam: what "now" is for the read (tests pin it)."""
    global _clock
    _clock = fn


def reset_clock() -> None:
    global _clock
    _clock = _system_clock


_providers: list[Provider] | None = None


def default_providers() -> list[Provider]:
    return [SchedulerProvider(), AttestObligationProvider(), VaultExpiryProvider()]


def providers() -> list[Provider]:
    return list(_providers) if _providers is not None else default_providers()


def set_providers(items: Sequence[Provider]) -> None:
    """Bind the provider list (tests and products). :func:`reset_providers` undoes it."""
    global _providers
    _providers = list(items)


def reset_providers() -> None:
    global _providers
    _providers = None


# --------------------------------------------------------------------------- the read


def _parse_bound(raw: Any, tz: str, *, end: bool = False) -> datetime | None:
    """A date (site-local midnight; an END date is inclusive) or an ISO instant."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return _aware(raw)
    text = str(raw).strip()
    if len(text) == 10:
        d = date.fromisoformat(text)
        if end:
            d = d + ONE_DAY
        return datetime.combine(d, time.min, tzinfo=ZoneInfo(tz))
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))


def window_for(*, start: Any = None, end: Any = None, days: int | None = None,
               tz: str | None = None, now: datetime | None = None) -> Window:
    """The window: ``start`` (default today, site time) to ``end`` or ``days`` later."""
    tz = tz or site_timezone()
    now = _aware(now or _clock())
    lo = _parse_bound(start, tz) or datetime.combine(
        now.astimezone(ZoneInfo(tz)).date(), time.min, tzinfo=ZoneInfo(tz)
    )
    hi = _parse_bound(end, tz, end=True)
    if hi is None:
        hi = lo + timedelta(days=int(days or DEFAULT_DAYS))
    if hi <= lo:
        raise ValueError("the window ends before it starts")
    return Window(start=lo, end=hi, tz=tz)


def _who(p: str) -> str:
    return p.strip().lstrip("@").lower()


def _for_person(item: dict[str, Any], person: str | None) -> bool:
    if not person:
        return True
    want = _who(person)
    names = [item.get("owner") or "", *item.get("addressed_to", [])]
    for n in names:
        got = _who(str(n))
        if got and (got == want or got.split(":", 1)[0] == want.split(":", 1)[0]):
            return True
    return False


def _sort_key(item: dict[str, Any]) -> tuple[str, str]:
    if item["date_only"]:
        return (item["start"] + "T00:00:00", item["id"])
    utc = datetime.fromisoformat(item["start"]).astimezone(UTC).isoformat()
    return (utc[:19], item["id"])


def collect(window: Window, *, source: str | None = None, person: str | None = None,
            now: datetime | None = None,
            using: Sequence[Provider] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(items, sources)``. A provider that fails is reported in ``sources``
    as unavailable; it never empties the rest of the agenda."""
    now = _aware(now or _clock())
    out: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for p in using if using is not None else providers():
        if source and p.name != source:
            continue
        try:
            got = p.list(window, now=now)[:MAX_PER_SOURCE]
        except Exception as exc:  # noqa: BLE001 - one source down is not an empty agenda
            log.debug("agenda source %s unavailable: %s", p.name, exc)
            report.append({"name": p.name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        report.append({"name": p.name, "ok": True, "count": len(got)})
        out.extend(it.to_dict() for it in got)
    out = [it for it in out if _for_person(it, person)]
    out.sort(key=_sort_key)
    return out, report


def items_list(params: dict[str, Any] | None = None, *, now: datetime | None = None,
               using: Sequence[Provider] | None = None) -> dict[str, Any]:
    """The agenda: ``{tz, tz_label, window: {from, to}, count, items, sources}``.

    ``params``: ``from``, ``to`` (dates or ISO instants, site time; ``to`` as a
    date is inclusive), or ``days`` (default 7); ``source``; ``person``.
    """
    params = params or {}
    now = _aware(now or _clock())
    window = window_for(start=params.get("from"), end=params.get("to"),
                        days=params.get("days"), now=now)
    items, report = collect(window, source=params.get("source") or None,
                            person=params.get("person") or None, now=now, using=using)
    return {
        "tz": window.tz,
        "tz_label": tz_label(window.tz, window.start),
        "window": {"from": window.start.isoformat(), "to": window.end.isoformat()},
        "count": len(items),
        "items": items,
        "sources": report,
    }


def items_show(item_id: str, *, now: datetime | None = None,
               using: Sequence[Provider] | None = None) -> dict[str, Any] | None:
    """One item by id, or ``None``. A run id carries its instant, so its own
    window is searched; anything else is searched a year either side of now."""
    now = _aware(now or _clock())
    tz = site_timezone()
    if "@" in item_id and item_id.startswith("schedule:run:"):
        stamp = item_id.rsplit("@", 1)[1]
        try:
            at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            window = Window(start=at - timedelta(seconds=1), end=at + timedelta(seconds=1), tz=tz)
        except ValueError:
            return None
    else:
        window = Window(start=now - timedelta(days=366), end=now + timedelta(days=366), tz=tz)
    source = item_id.split(":", 1)[0]
    items, _ = collect(window, source=source, now=now, using=using)
    for it in items:
        if it["id"] == item_id:
            return it
    return None


def due_soon(person: str | None, days: int = DUE_SOON_DAYS, *, now: datetime | None = None,
             using: Sequence[Provider] | None = None) -> list[dict[str, Any]]:
    """Items in the next ``days`` owned by, or addressed to, ``person`` (everyone
    when ``None``): the "Needs you" Due section. Same dict shape as
    ``items_list()['items']``. Runs are the scheduler's, not a person's, so
    only obligations and expiries are due."""
    now = _aware(now or _clock())
    window = Window(start=now, end=now + timedelta(days=int(days)), tz=site_timezone())
    items, _ = collect(window, person=person, now=now, using=using)
    return [it for it in items if it["kind"] in ("obligation", "expiry")]


# --------------------------------------------------------------------------- ICS export


def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\n", "\\n"))


def to_ics(agenda: dict[str, Any]) -> str:
    """The agenda as an iCalendar document: a format of the same read, not a
    subscription. Timed items are UTC instants; a date-only item stays a DATE."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Axiom//schedule agenda//EN",
             "CALSCALE:GREGORIAN", f"X-WR-TIMEZONE:{agenda.get('tz', 'UTC')}"]
    for it in agenda.get("items", []):
        lines += ["BEGIN:VEVENT", f"UID:{_ics_escape(it['id'])}", f"DTSTAMP:{stamp}"]
        if it["date_only"]:
            d = date.fromisoformat(it["start"])
            lines.append(f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(d + ONE_DAY).strftime('%Y%m%d')}")
        else:
            start = datetime.fromisoformat(it["start"]).astimezone(UTC)
            lines.append(f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}")
            if it.get("end"):
                end = datetime.fromisoformat(it["end"]).astimezone(UTC)
                lines.append(f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}")
        lines.append(f"SUMMARY:{_ics_escape(it['title'])}")
        desc = " | ".join(x for x in (it.get("status_line"), it.get("source"),
                                      (it.get("owner_link") or {}).get("cli")) if x)
        if desc:
            lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
        lines += [f"CATEGORIES:{it['kind']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


__all__ = [
    "AttestObligationProvider",
    "Provider",
    "SITE_TZ_ENV",
    "ScheduledItem",
    "SchedulerProvider",
    "VaultExpiryProvider",
    "Window",
    "collect",
    "default_providers",
    "due_soon",
    "items_list",
    "items_show",
    "providers",
    "reset_clock",
    "reset_providers",
    "set_clock",
    "set_providers",
    "site_timezone",
    "to_ics",
    "tz_label",
    "window_for",
]
