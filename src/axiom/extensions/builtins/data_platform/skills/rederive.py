# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.rederive`` — apply a declaration to the history it should have had.

A site fills in a unit it had never stated, declares the fault word its
hardware emits, corrects a role. Until now none of that could reach a row
already written: the conform insert was ``ON CONFLICT DO NOTHING``, and
``row_hash`` hashes the SOURCE ROW, which does not change when a channel map
does. So a correction applied only to future data, and the bronze we retain
precisely so we can re-derive — 544 GB of it on one node — was unreachable.

That is the difference between an integration that costs a partner a meeting
per gap and one that costs them one declaration. They should not have to
re-send data we already hold in order to benefit from a fact they just told
us.

**Dry run by default.** A re-derive can change a number — a quality verdict
withholds a value (ADR-132 D3) — and it takes row locks for its duration. Both
are reasons to see the count before the write. The dry run is not a prediction:
everything is executed and then rolled back, so the number is what would
actually have happened.

**Scoped by site.** Re-deriving every site because one of them corrected a
unit is how a maintenance operation becomes an outage.
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _resolve_dsn(params: dict[str, Any]) -> str | None:
    from .._dsn import resolve_dsn

    return resolve_dsn(params)


def _sites(params: dict[str, Any]) -> frozenset[str] | None:
    raw = params.get("site") or params.get("sites")
    if not raw:
        return None
    names = [s.strip() for s in raw.split(",")] if isinstance(raw, str) else list(raw)
    kept = frozenset(n for n in names if n)
    return kept or None


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Re-apply the declarations to rows already in silver.

    Params: ``site`` (one, or comma-separated; every site when absent),
    ``apply`` (default false — count only), ``bronze_root``, ``dsn``.
    """
    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(
            ok=False,
            errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL"],
        )

    bronze_root = params.get("bronze_root") or os.path.expanduser("~/.axi/bronze")
    apply = bool(params.get("apply"))
    only_sites = _sites(params)

    from ..conformance.runner import run_conform

    try:
        stats = run_conform(
            bronze_root=bronze_root,
            dsn=dsn,
            state_dir=ctx.state_dir,
            rederive=True,
            apply=apply,
            only_sites=only_sites,
        )
    except Exception as exc:  # noqa: BLE001 — a failed re-derive must say so
        return SkillResult(ok=False, errors=[f"re-derive failed: {exc}"])

    changed = int(stats.get("rows_changed", 0))
    seen = int(stats.get("rows_out", 0))
    unmatched = stats.get("sites_not_matched") or []

    actions = [
        f"read {seen:,} conformed row(s) from bronze at {bronze_root}",
        (
            f"{changed:,} row(s) {'changed' if apply else 'WOULD change'} — the rest "
            "already matched their declarations and were not rewritten"
        ),
    ]
    if unmatched:
        # Silence here would read as "nothing needed changing", which is a
        # different statement from "that site is not mapped to a connector".
        actions.append(
            "no connector maps to: " + ", ".join(unmatched) + " — nothing was "
            "re-derived for them, which is not the same as nothing needing it"
        )
    if not apply:
        actions.append("rolled back — pass apply=true to write")

    errors: list[str] = []
    if unmatched and only_sites and len(unmatched) == len(only_sites):
        errors.append(
            "no named site is mapped to a connector, so this re-derived nothing: "
            + ", ".join(unmatched)
        )

    return SkillResult(
        ok=not errors,
        value={
            "bronze_root": bronze_root,
            "sites": sorted(only_sites) if only_sites else "all",
            "rows_read": seen,
            "rows_changed": changed,
            "applied": apply,
            "sites_not_matched": unmatched,
            "funnel": {
                k: stats.get(k)
                for k in ("rows_in", "rows_out", "errored", "unknown_schema")
            },
        },
        errors=errors,
        actions_taken=actions,
    )


__all__ = ["run"]
