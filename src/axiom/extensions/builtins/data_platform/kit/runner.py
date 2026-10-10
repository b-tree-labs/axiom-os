# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``kit-try``: run every tier of a kit on local data, and show each one.

bronze (the samples) → silver (the tenant's normalizers, then the silver
declarations) → gold (the tenant's SQL, as views in the tenant's schema) →
verbs and charts (run **as the tenant role**, under the same row-level policy
the host enforces).

The tenant's normalizers are imported from ``conform/`` here, on the
provider's own machine. That is the one place tenant Python runs, by design:
on the host a normalizer reaches the shared conform process only through
review (see the tenant data kit PRD).
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..conformance import NormalizerRegistry, conform_rows, pg_upsert
from . import declarations as decl
from .medallion import connect, dsn, prepare
from .project import KitError, KitProject


def load_normalizers(project: KitProject) -> tuple[NormalizerRegistry, list[str]]:
    """Import ``conform/*.py`` and call each module's ``register_all``."""
    registry = NormalizerRegistry()
    loaded: list[str] = []
    folder = project.folder("conform")
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = f"kit_conform_{project.role}_{path.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise KitError(f"{path}: could not be imported")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — reported with the file named
            raise KitError(f"{path}: importing it failed: {type(exc).__name__}: {exc}") from None
        register = getattr(module, "register_all", None)
        if not callable(register):
            raise KitError(f"{path}: define register_all(registry) that registers your schema_ref")
        register(registry)
        loaded.append(path.name)
    return registry, loaded


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    return value


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c.name for c in cur.description or []]
    return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def conform(project: KitProject, d: str) -> dict[str, Any]:
    registry, loaded = load_normalizers(project)
    bronze = project.folder("samples") / "bronze"
    connectors = sorted(p.name for p in bronze.iterdir() if p.is_dir()) if bronze.is_dir() else []
    site_by_connector = {c: project.tenant for c in connectors}
    with connect(d) as conn, conn.cursor() as cur:
        # ADR-128 E4: the kit's local medallion; this IS the conform pass, on the provider's own data.
        cur.execute("DELETE FROM silver.signals WHERE site = %s", (project.tenant,))
        stats = conform_rows(bronze, registry, site_by_connector, upsert=pg_upsert(cur))
    stats["normalizers"] = loaded
    stats["registered"] = registry.refs()
    return stats


# ADR-128 E4: silver declarations applied by the platform's own code, on the local medallion.
def apply_silver(project: KitProject, d: str, ds: decl.Declarations) -> list[str]:
    """Roles onto existing rows; derived channels as new rows marked ``derived:``."""
    done: list[str] = []
    with connect(d) as conn, conn.cursor() as cur:
        for channel, role in ds.roles.items():
            cur.execute(
                "UPDATE silver.signals SET role = %s WHERE site = %s AND channel = %s",
                (role, project.tenant, channel),
            )
            done.append(f"role {role!r} on {channel} ({cur.rowcount} rows)")
        for dv in ds.derived:

            # ADR-128 E4: the derived channel is silver's own transform (compose),
            # evaluated by platform code from a declaration; nothing here serves.
            def side(operand: str, alias: str) -> tuple[str, str]:
                try:
                    float(operand)
                    return operand, ""
                except ValueError:
                    return f"{alias}.value", (
                        f" JOIN silver.signals {alias} ON {alias}.site = base.site "
                        f"AND {alias}.feed = base.feed AND {alias}.ts = base.ts "
                        f"AND {alias}.channel = %({alias}_ch)s"
                    )

            lhs, ljoin = side(dv.left, "l")
            rhs, rjoin = side(dv.right, "r")
            anchor = dv.left if ljoin else dv.right
            value = (
                f"({lhs} {dv.op} NULLIF({rhs}, 0))" if dv.op == "/" else f"({lhs} {dv.op} {rhs})"
            )
            # ADR-128 E4: writes the derived rows beside the readings they came from.
            cur.execute(
                f"""INSERT INTO silver.signals
                      (site, feed, channel, ts, value, unit, quality, source_class,
                       schema_ref, row_hash, role, derivation)
                    SELECT base.site, base.feed, %(name)s, base.ts, {value}, %(unit)s,
                           base.quality, base.source_class, 'kit:derived',
                           'derived:' || md5(base.row_hash || %(name)s), %(role)s, %(derivation)s
                    FROM silver.signals base{ljoin}{rjoin}
                    WHERE base.site = %(site)s AND base.channel = %(anchor)s
                    ON CONFLICT (row_hash, channel) DO NOTHING""",
                {
                    "name": dv.name,
                    "unit": dv.unit,
                    "role": dv.role,
                    "site": project.tenant,
                    "derivation": f"derived:{dv.expression}",
                    "anchor": anchor,
                    "l_ch": dv.left,
                    "r_ch": dv.right,
                },
            )
            done.append(f"derived {dv.name} = {dv.expression} ({cur.rowcount} rows)")
    return done


def apply_gold(project: KitProject, d: str, ds: decl.Declarations) -> list[str]:
    """Each gold SQL as a view in the tenant schema, invoker's rights (so RLS applies)."""
    done: list[str] = []
    schema = project.gold_schema
    with connect(d) as conn, conn.cursor() as cur:
        for g in ds.gold:
            cur.execute(f'DROP VIEW IF EXISTS "{schema}"."{g.name}" CASCADE')
            try:
                cur.execute(
                    f'CREATE VIEW "{schema}"."{g.name}" WITH (security_invoker = true) AS {g.sql}'
                )
            except Exception as exc:  # noqa: BLE001 — named, then raised as the kit's error
                raise KitError(
                    f"{g.source}: Postgres refused it: {str(exc).splitlines()[0]}"
                ) from None
            if g.description:
                from psycopg import sql as _sql

                cur.execute(
                    _sql.SQL("COMMENT ON VIEW {}.{} IS {}").format(
                        _sql.Identifier(schema),
                        _sql.Identifier(g.name),
                        _sql.Literal(g.description),
                    )
                )
            cur.execute(f'GRANT SELECT ON "{schema}"."{g.name}" TO "{project.role}"')
            done.append(f"{schema}.{g.name}")
    return done


def verb_sql(project: KitProject, v: decl.Verb) -> str:
    return v.sql.replace("{object}", f'"{project.gold_schema}"."{v.obj}"')


def run_verb(
    project: KitProject, d: str, v: decl.Verb, params: dict[str, Any] | None = None
) -> list[dict]:
    values = {k: spec.get("default") for k, spec in v.params.items()}
    values.update(params or {})
    with connect(d, role=project.role) as conn, conn.cursor() as cur:
        cur.execute(verb_sql(project, v), values)
        return _rows(cur)


def query_object(project: KitProject, d: str, name: str, limit: int = 2000) -> list[dict]:
    with connect(d, role=project.role) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{project.gold_schema}"."{name}" LIMIT %s', (limit,))
        return _rows(cur)


def render_chart(project: KitProject, d: str, chart: decl.Chart) -> dict[str, Any]:
    """Terminal sparklines, and an SVG file the notebook and the web app can show."""
    from ...scidisplay.chart_render import render_timeseries
    from ...scidisplay.chart_svg import Series, render_svg

    spec = chart.spec
    rows = query_object(project, d, spec["object"])
    x, y, series_col = spec["x"], spec["y"], spec.get("series")
    only = spec.get("only")
    if only and series_col:
        rows = [r for r in rows if str(r.get(series_col)) in set(map(str, only))]
    unit_col = spec.get("unit", "unit")
    units_drawn = sorted({str(r[unit_col]) for r in rows if r.get(unit_col)})
    if len(units_drawn) > 1:
        # Two units on one axis is a picture of nothing: each line sits at a
        # height that means something only on its own scale.
        raise KitError(
            f"{chart.source}: draws {', '.join(units_drawn)} on one axis. Name the series to draw "
            f'with "only": [...] so they share a unit, or split it into one chart per unit.'
        )
    lines = render_timeseries(
        rows,
        time_column=x,
        value_column=y,
        series_column=series_col,
        unit_column=spec.get("unit", "unit"),
    )
    grouped: dict[str, list] = {}
    units: dict[str, str] = {}
    for r in rows:
        key = str(r.get(series_col)) if series_col else y
        t = r.get(x)
        when = datetime.fromisoformat(t) if isinstance(t, str) else t
        grouped.setdefault(key, []).append((when, r.get(y)))
        if r.get(spec.get("unit", "unit")):
            units[key] = str(r[spec.get("unit", "unit")])
    svg = render_svg(
        [Series(name=k, points=sorted(v), unit=units.get(k, "")) for k, v in grouped.items()],
        title=spec.get("title", chart.name),
        provenance=f"{project.gold_schema}.{spec['object']}",
    )
    project.out_dir.mkdir(parents=True, exist_ok=True)
    out = project.out_dir / f"{chart.name}.svg"
    out.write_text(svg, encoding="utf-8")
    return {"chart": chart.name, "lines": lines, "svg": str(out), "rows": len(rows)}


def _named(source: Path, fn):
    """Run ``fn``; a database refusal comes back naming the file that caused it."""
    try:
        return fn()
    except KitError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised as the kit's error, file named
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        hint = ""
        if "permission denied" in first:
            hint = (
                " — your queries run as your tenant role, which can read silver.signals,"
                " gold.* and your own schema; nothing else"
            )
        raise KitError(f"{source}: {first}{hint}") from None


def try_kit(project: KitProject) -> dict[str, Any]:
    """Every tier, in order. Stops at the first tier that fails, naming the file."""
    ds = decl.load(project)
    if ds.problems:
        raise KitError("fix these declarations first:\n  " + "\n  ".join(ds.problems))
    d = dsn(project)
    prepare(project, d)
    report: dict[str, Any] = {"tenant": project.tenant}
    report["silver"] = conform(project, d)
    report["silver"]["declarations"] = apply_silver(project, d, ds)
    report["gold"] = {"objects": apply_gold(project, d, ds), "samples": {}}
    for g in ds.gold:
        report["gold"]["samples"][g.name] = _named(
            g.source, lambda g=g: query_object(project, d, g.name, limit=5)
        )
    report["verbs"] = {
        v.name: _named(v.source, lambda v=v: run_verb(project, d, v))[:10] for v in ds.verbs
    }
    report["charts"] = [render_chart(project, d, c) for c in ds.charts]
    return report


def row_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


__all__ = [
    "apply_gold",
    "apply_silver",
    "conform",
    "load_normalizers",
    "query_object",
    "render_chart",
    "run_verb",
    "try_kit",
]
