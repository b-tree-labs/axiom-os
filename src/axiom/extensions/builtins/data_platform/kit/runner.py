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

from ..conformance import NormalizerRegistry, conform_rows, pg_upsert, pg_upsert_batched
from ..conformance.deposits import DepositRegistry, conform_deposits
from . import declarations as decl
from .medallion import connect, dsn, prepare, require_stamp
from .project import KitError, KitProject


def load_normalizers(project: KitProject) -> tuple[NormalizerRegistry, DepositRegistry, list[str]]:
    """Import ``conform/*.py``; call ``register_all`` and any ``register_deposits``.

    ``register_all(registry)`` registers row normalizers (one bronze record →
    rows). ``register_deposits(registry)`` registers deposit normalizers (one
    mirrored file → rows) and is required exactly when ``kit.toml`` declares a
    ``kind = "deposit"`` source. A module may define either or both.
    """
    registry = NormalizerRegistry()
    deposits = DepositRegistry()
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
        register_deps = getattr(module, "register_deposits", None)
        if not callable(register) and not callable(register_deps):
            raise KitError(
                f"{path}: define register_all(registry) (rows) or "
                f"register_deposits(registry) (mirrored files) for your schema_ref"
            )
        if callable(register):
            register(registry)
        if callable(register_deps):
            register_deps(deposits)
        loaded.append(path.name)
    missing = {ref for ref in project.deposit_sources.values() if ref and deposits.get(ref) is None}
    if missing:
        raise KitError(
            f"kit.toml declares deposit source(s) with schema_ref "
            f"{', '.join(sorted(missing))} but no conform/*.py registers a deposit "
            f"normalizer for them (register_deposits)"
        )
    return registry, deposits, loaded


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    return value


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c.name for c in cur.description or []]
    return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def conform(project: KitProject, d: str, *, fresh: bool = True) -> dict[str, Any]:
    require_stamp(d, "kit-try")
    registry, deposits, loaded = load_normalizers(project)
    bronze = project.folder("samples") / "bronze"
    connectors = (
        sorted(p.name for p in bronze.iterdir() if p.is_dir() and not p.name.startswith("_"))
        if bronze.is_dir()
        else []
    )
    site_by_connector = {c: project.tenant for c in connectors}
    with connect(d) as conn, conn.cursor() as cur:
        # ADR-128 E4: the kit's local medallion; this IS the conform pass, on the provider's own data.
        if fresh:
            cur.execute("DELETE FROM silver.signals WHERE site = %s", (project.tenant,))
        stats = conform_rows(bronze, registry, site_by_connector, upsert=pg_upsert(cur))
        if project.deposit_sources:
            # One deposit is often millions of readings, so these go batched,
            # flushed per deposit (the checkpoint) and at the end.
            up = pg_upsert_batched(cur, size=5000)
            stats["deposits"] = conform_deposits(
                bronze,
                deposits,
                project.deposit_sources,
                {c: project.tenant for c in project.deposit_sources},
                upsert=up,
                checkpoint=up.flush,
            )
            up.flush()
            stats["rows_in"] += stats["deposits"]["deposits_seen"]
            stats["rows_out"] += stats["deposits"]["rows_out"]
    stats["normalizers"] = loaded
    stats["registered"] = registry.refs()
    stats["deposit_refs"] = deposits.refs()
    return stats


# ADR-128 E4: silver declarations applied by the platform's own code, on the local medallion.
def apply_silver(project: KitProject, d: str, ds: decl.Declarations) -> list[str]:
    """Roles onto existing rows; derived channels as new rows marked ``derived:``."""
    require_stamp(d, "kit-try")
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
    require_stamp(d, "kit-try")
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


#: transform.agg -> SQL aggregate. Kept beside the resolver that runs it; the
#: declaration loader refuses anything not listed (decl.KIT_AGGREGATIONS).
_AGG_SQL = {
    "mean": "avg(value)",
    "min": "min(value)",
    "max": "max(value)",
    "last": "(array_agg(value ORDER BY ts DESC))[1]",
    "count": "count(value)::float8",
}
_BUCKET_UNITS = {"ms": "milliseconds", "s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _bucket_interval(bucket: str) -> str:
    """``'1m'`` -> ``'1 minutes'``: the scidisplay bucket grammar as a SQL interval."""
    import re

    m = re.match(r"^([1-9][0-9]*)(ms|s|m|h|d)$", bucket)
    if not m:  # pragma: no cover - ChartSpec already enforced the grammar
        raise KitError(f"bucket {bucket!r} is not a duration")
    return f"{m.group(1)} {_BUCKET_UNITS[m.group(2)]}"


class KitResolver:
    """The kit's ``ChannelResolver``: what the TENANT ROLE can see, nothing else.

    scidisplay's spec carries ``site`` + ``channels``; deciding which stored
    rows serve them is the resolver's job. Locally that is the shared gold
    surface (``gold.signals``, invoker's rights) read as the tenant role, so a
    chart can only ever resolve channels row-level security lets it see.
    """

    def __init__(self, project: KitProject, d: str) -> None:
        self.project, self.d = project, d
        self._info: dict[str, Any] = {}

    def resolve_channel(self, site: str, channel: str):
        """One channel, looked up on its own.

        Scoped rather than a whole-site ``GROUP BY channel``: at node scale the
        site-wide form was measured at 45 s over 44.9M rows against about 1 ms
        scoped, and a chart only ever asks about the channels it names.
        """
        if site != self.project.tenant:
            return None
        if channel not in self._info:
            from ...scidisplay.channel_catalog import ChannelInfo

            with connect(self.d, role=self.project.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) > 0, max(unit), max(feed) FROM gold.signals "
                    "WHERE site = %s AND channel = %s",
                    (self.project.tenant, channel),
                )
                present, unit, feed = cur.fetchone()
            self._info[channel] = (
                ChannelInfo(name=channel, unit=unit, feed=feed) if present else None
            )
        return self._info[channel]

    def list_channels(self, site: str):
        """Every channel at the site — only reached for near-miss suggestions."""
        if site != self.project.tenant:
            return []
        with connect(self.d, role=self.project.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT channel FROM gold.signals WHERE site = %s ORDER BY 1",
                (self.project.tenant,),
            )
            return [r[0] for r in cur.fetchall()]


def axis_unit(spec: Any, unit: str | None) -> str:
    """The unit a channel is DRAWN in: its display unit, or ``unit not declared``.

    Absence is a unit of its own for the one-axis rule (scidisplay's
    ``UNDECLARED``), so a declared channel and an undeclared one never share an
    axis unremarked, and an all-undeclared chart is labelled, never blank.
    """
    from ...scidisplay.units import UNDECLARED, declared

    if not declared(unit):
        return UNDECLARED
    return dict(spec.display_units or {}).get(unit, unit)


def chart_rows(project: KitProject, d: str, chart: decl.Chart) -> list[dict[str, Any]]:
    """The bucketed series a ChartSpec asks for, read as the tenant role."""
    from ...scidisplay.chart_validation import validate_channels

    spec = chart.spec
    report = validate_channels(spec, KitResolver(project, d))
    if not report.is_valid:  # three-valued; never `if report:`
        try:
            report.require_valid()
        except Exception as exc:  # noqa: BLE001 — re-raised naming the file
            raise KitError(f"{chart.source}: {exc}") from None
    resolved = report.resolved or {}
    units = sorted({axis_unit(spec, info.unit) for info in resolved.values()})
    if len(units) > 1:
        # Two units on one axis is a picture of nothing: each line sits at a
        # height that means something only on its own scale.
        raise KitError(
            f"{chart.source}: draws {', '.join(units)} on one axis. Draw channels that share "
            f"a unit, or split it into one chart per unit. A channel with no declared unit "
            f"counts as its own unit: it can share an axis only with other undeclared channels."
        )
    fields = dict(spec.window.fields or {}) if spec.window is not None else {}
    if spec.window is not None and spec.window.basis != "absolute":
        raise KitError(
            f"{chart.source}: window basis {spec.window.basis!r} needs a run catalogue the "
            f"local medallion does not have yet; use an absolute window"
        )
    transform = spec.transform
    agg = _AGG_SQL[transform.agg] if transform is not None else "avg(value)"
    interval = _bucket_interval(transform.bucket) if transform is not None else "1 minutes"
    sql = (
        # Grouped by (channel, t) with max(unit), never by unit: one channel
        # carrying two unit spellings (or NULL beside a unit) must not draw
        # two points in one bucket.
        f"SELECT channel, max(unit) AS unit, "
        f"date_bin(%(interval)s::interval, ts, %(origin)s::timestamptz) "
        f"AS t, {agg} AS value FROM gold.signals "
        f"WHERE site = %(site)s AND channel = ANY(%(channels)s) "
        f"AND ts >= %(from)s::timestamptz AND ts < %(to)s::timestamptz "
        f"GROUP BY channel, t ORDER BY channel, t"
    )
    params = {
        "interval": interval,
        "origin": fields.get("from", "2000-01-01T00:00:00+00:00"),
        "from": fields.get("from", "-infinity"),
        "to": fields.get("to", "infinity"),
        "site": project.tenant,
        "channels": list(spec.channels or ()),
    }
    with connect(d, role=project.role) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return _rows(cur)


def render_chart(project: KitProject, d: str, chart: decl.Chart) -> dict[str, Any]:
    """Terminal sparklines, and an SVG file the notebook and the web app can show."""
    from ...scidisplay.chart_render import render_timeseries
    from ...scidisplay.chart_svg import Series, render_svg

    spec = chart.spec
    rows = chart_rows(project, d, chart)
    if not rows:
        raise KitError(
            f"{chart.source}: resolved its channels but drew no points in its window; "
            f"check window.from/to against the data's time span"
        )
    lines = render_timeseries(
        rows, time_column="t", value_column="value", series_column="channel", unit_column="unit"
    )
    grouped: dict[str, list] = {}
    units: dict[str, str] = {}
    for r in rows:
        t = r["t"]
        when = datetime.fromisoformat(t) if isinstance(t, str) else t
        grouped.setdefault(str(r["channel"]), []).append((when, r["value"]))
        if r.get("unit"):
            units[str(r["channel"])] = str(r["unit"])
    svg = render_svg(
        [Series(name=k, points=sorted(v), unit=units.get(k, "")) for k, v in grouped.items()],
        title=spec.title or chart.name,
        provenance=f"gold.signals · site {project.tenant}",
        # Everything a spec pins is honoured when it is drawn, or the same
        # document draws a different figure here than on the chart page.
        display_units=spec.display_units,
        series_colours=spec.series_colours,
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
    # Before anything is read or written: kit-try deletes and rewrites this
    # tenant's rows, so it only ever runs on a database kit-up stamped.
    require_stamp(d, "kit-try")
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
    # Each automation once, as a dry run: what it would send or write. Nothing
    # is sent and nothing is written; that only happens on the node.
    from . import automations

    report["automations"] = {
        a.name: _named(a.source, lambda a=a: automations.dry_run(project, d, a, ds.verbs))
        for a in ds.automations
    }
    return report


def row_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


__all__ = [
    "apply_gold",
    "apply_silver",
    "conform",
    "load_normalizers",
    "query_object",
    "KitResolver",
    "axis_unit",
    "chart_rows",
    "render_chart",
    "run_verb",
    "try_kit",
]
