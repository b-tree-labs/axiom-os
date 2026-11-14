# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Periods the site's clock defines (#1806): a shift that starts at set times, not at an entry.

A ``[period.<name>]`` lists start times (``HH:MM``); a period runs from one start to the next.
Times are read in the SITE's time zone, the ``site.timezone`` setting (or ``$AXIOM_SITE_TZ``),
never the browser's or the server's; a site that names none cannot seal a clock period. A seal
by a period covers the last period that ended. Once an ended period is past its grace window
with nothing sealed in it, it is "not closed" until a person who may seal closes it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import site_settings
from .logbooks import PeriodDecl

SITE_TZ_ENV = "AXIOM_SITE_TZ"


class PeriodError(ValueError):
    """The site's clock cannot place the period; the message says why."""


def site_zone(site_id: str) -> tzinfo:
    name = site_settings.value(site_id, "site.timezone", None) or os.environ.get(SITE_TZ_ENV)
    if not name:
        raise PeriodError(
            f"site {site_id} names no time zone (the site.timezone setting), so its clock "
            "periods have no start times"
        )
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise PeriodError(f"site {site_id}'s time zone {name!r} is not a known zone") from None


def _starts(site_id: str, decl: PeriodDecl) -> list[tuple[int, int]]:
    raw = decl.starts
    if decl.starts_key:
        value = site_settings.value(site_id, decl.starts_key, None)
        if value is not None:
            raw = tuple(value) if isinstance(value, list | tuple) else (str(value),)
    out = []
    for t in raw:
        try:
            hh, mm = (int(x) for x in str(t).split(":"))
        except ValueError:
            raise PeriodError(f"start time {t!r} is not HH:MM") from None
        out.append((hh, mm))
    return sorted(set(out))


def grace(site_id: str, decl: PeriodDecl) -> timedelta:
    minutes = decl.grace_minutes
    if decl.grace_key:
        minutes = int(site_settings.value(site_id, decl.grace_key, minutes))
    return timedelta(minutes=minutes)


def last_ended(site_id: str, decl: PeriodDecl, now: datetime) -> tuple[datetime, datetime]:
    """``(start, end)`` of the last period that ended at or before ``now``, in the site's zone."""
    zone = site_zone(site_id)
    local = now.astimezone(zone)
    starts = _starts(site_id, decl)
    bounds = [
        datetime(d.year, d.month, d.day, hh, mm, tzinfo=zone)
        for d in (local.date() - timedelta(days=k) for k in (2, 1, 0))
        for hh, mm in starts
    ]
    past = [b for b in sorted(bounds) if b <= local]
    if len(past) < 2:
        raise PeriodError(f"no {decl.name} period has ended yet")
    return past[-2], past[-1]


def describe(decl: PeriodDecl, start: datetime, end: datetime) -> dict[str, Any]:
    return {"name": decl.name, "start": start.isoformat(), "end": end.isoformat()}


__all__ = ["PeriodError", "describe", "grace", "last_ended", "site_zone"]
