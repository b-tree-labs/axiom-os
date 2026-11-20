# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The values Tab completes from this data platform: sites, roles, channels, feeds, days, kit verbs.

Called only by the background refresh in :mod:`axiom.cli.completion_values`, never
by a TAB press. It asks the same skills a person would (``roles``, ``channels``,
``show``, ``kit-verbs``), so a bound laptop asks its node and a node answers from
its own medallion, through one path and with the caller's own credential.

Every read is bounded: at most ``_MAX_SITES`` sites, one series read for days, and
the provider stops asking when the refresh's deadline is near. A read that fails
names its kinds in ``failed_kinds``, so the cache keeps what it knew before rather
than forgetting it because the node was offline this once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

#: Sites whose channels and days are fetched (a bound laptop has one).
_MAX_SITES = 3
#: Days of history scanned for "days with data", ending at the site's last reading.
_DAY_SPAN = 14
#: Stop starting new reads when fewer seconds than this are left.
_RESERVE_S = 5.0


def _ctx():
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext

    from . import skills as data_skills

    return SkillContext(
        registry=data_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.completion"),
        user_prompt=None,
    )


def _read(ctx, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one read skill directly (no audit row: a completion cache is not an action)."""
    spec = ctx.registry.spec(f"data.{verb}")
    if spec is None:
        raise LookupError(f"no skill data.{verb}")
    res = spec.fn(params, ctx)
    if not res.ok:
        raise RuntimeError("; ".join(res.errors) or f"data.{verb} failed")
    value = res.value if isinstance(res.value, dict) else {}
    data = value.get("data")
    return data if isinstance(data, dict) else value


def _day(value: Any) -> str:
    from .friendly import parse_instant

    dt = parse_instant(value)
    return dt.date().isoformat() if dt else ""


def collect(pctx) -> dict[str, Any]:
    """``{"values", "per_site", "errors", "failed_kinds"}`` for the completion cache."""
    out: dict[str, Any] = {"values": {}, "per_site": {}, "errors": [], "failed_kinds": []}
    values: dict[str, list[str]] = out["values"]
    per_site: dict[str, dict[str, list[str]]] = out["per_site"]
    ctx = _ctx()

    def failed(kinds: list[str], exc: Exception) -> None:
        out["errors"].append(f"{', '.join(kinds)}: {exc}"[:240])
        out["failed_kinds"].extend(kinds)

    # Roles, and which sites answer each: the one fleet-wide read.
    roles: list[dict[str, Any]] = []
    try:
        roles = list(_read(ctx, "roles", {}).get("roles") or [])
    except Exception as exc:  # noqa: BLE001
        failed(["role", "site"], exc)
    values["role"] = sorted({str(r["role"]) for r in roles if r.get("role")})
    sites = sorted({str(s) for r in roles for s in (r.get("sites") or []) if s})
    bound = (pctx.binding or {}).get("site") or ""
    if bound and bound not in sites:
        sites.insert(0, bound)
    elif bound:
        sites.remove(bound)
        sites.insert(0, bound)
    values["site"] = sites
    for site in sites:
        per_site.setdefault(site, {})["role"] = sorted(
            {str(r["role"]) for r in roles if r.get("role") and site in (r.get("sites") or [])}
        )

    # Channels and feeds, per site, for the bound site first.
    for site in sites[:_MAX_SITES]:
        if pctx.time_left() < _RESERVE_S:
            out["errors"].append("channels: stopped at the refresh deadline")
            break
        try:
            rows = list(_read(ctx, "channels", {"site": site}).get("channels") or [])
        except Exception as exc:  # noqa: BLE001
            failed(["channel", "feed"], exc)
            continue
        slot = per_site.setdefault(site, {})
        slot["channel"] = sorted({str(r["channel"]) for r in rows if r.get("channel")})
        slot["feed"] = sorted({str(r["feed"]) for r in rows if r.get("feed")})
        values.setdefault("channel", [])
        values.setdefault("feed", [])
        for c in slot["channel"]:
            if c not in values["channel"]:
                values["channel"].append(c)
        for f in slot["feed"]:
            if f not in values["feed"]:
                values["feed"].append(f)
        # Days with data: one bounded count-per-day read of the site's first role,
        # ending at the last reading the catalogue holds (never "now").
        last = max((str(r.get("last_ts") or "") for r in rows), default="")
        role = (slot.get("role") or [None])[0] or next((r.get("role") for r in rows if r.get("role")), None)
        if not (last and role) or pctx.time_left() < _RESERVE_S:
            continue
        end_day = _day(last)
        if not end_day:
            continue
        end = datetime.fromisoformat(end_day).replace(tzinfo=UTC) + timedelta(days=1)
        start = end - timedelta(days=_DAY_SPAN)
        try:
            shown = _read(ctx, "show", {
                "target": role, "site": site, "every": "1d", "fn": "count",
                "start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d"),
            })
        except Exception as exc:  # noqa: BLE001
            failed(["day"], exc)
            continue
        days = sorted(
            {_day(p.get("t")) for p in shown.get("series") or []
             if isinstance(p.get("value"), (int, float)) and p["value"] > 0} - {""},
            reverse=True,
        )
        slot["day"] = days
        values.setdefault("day", [])
        for d in days:
            if d not in values["day"]:
                values["day"].append(d)

    # Promoted kit verbs the caller may run.
    if pctx.time_left() >= _RESERVE_S:
        try:
            verbs = list(_read(ctx, "kit_verbs", {}).get("verbs") or [])
            values["kit_verb"] = sorted({str(v["name"]) for v in verbs if v.get("name")})
            for v in verbs:
                if v.get("site") and v.get("name"):
                    slot = per_site.setdefault(str(v["site"]), {})
                    slot.setdefault("kit_verb", [])
                    if v["name"] not in slot["kit_verb"]:
                        slot["kit_verb"].append(str(v["name"]))
        except Exception as exc:  # noqa: BLE001
            failed(["kit_verb"], exc)
    return out


__all__ = ["collect"]
