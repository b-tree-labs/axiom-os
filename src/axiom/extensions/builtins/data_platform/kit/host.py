# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The node's side of promotion (ADR-203): receive, check, approve, apply, serve.

A promoted kit is data on the node: a row in the kit registry holding the
bundle, and, once an operator approves it, a per-site schema of views, verbs,
charts and automations in registry tables. Nothing here imports or runs
contributor code. An automation's cadence is a PULSE schedule projected from the
registry after the apply commits (``automations.sync_cadences``).

Safety lives in the database, not in review:

- each site's objects run as that site's role (``kit_<site>``), which can read
  ``silver.signals`` (row-level security admits only its site), the shared gold
  views (made to run with the reader's rights, so the policy reaches through
  them), and its own schema;
- every existing reader of silver keeps its access through an explicit policy,
  added in the same transaction that enables row-level security, because a
  table with security enabled and no policy for a role shows that role nothing;
- the proof is read from the catalog inside the apply transaction, and a failure
  rolls the whole apply back. It never writes probe rows into the node's data.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from . import declarations as decl
from .bundle import Bundle, unpack
from .project import KitError, KitProject, load

SCHEMA = "kit_registry"
LOCK_TIMEOUT = "5s"
VERB_TIMEOUT_MS = 15_000
_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

REGISTRY_DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.bundles (
        id bigserial PRIMARY KEY,
        site text NOT NULL,
        sha256 text NOT NULL,
        submitted_by text NOT NULL,
        submitted_at timestamptz NOT NULL DEFAULT now(),
        state text NOT NULL CHECK (state IN ('checked','rejected','applied','superseded','retired')),
        problems jsonb NOT NULL DEFAULT '[]'::jsonb,
        manifest jsonb NOT NULL,
        bundle bytea NOT NULL,
        approved_by text,
        applied_at timestamptz)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.verbs (
        site text NOT NULL,
        name text NOT NULL,
        description text NOT NULL DEFAULT '',
        object text NOT NULL,
        sql text NOT NULL,
        params jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        bundle_id bigint NOT NULL,
        PRIMARY KEY (site, name))""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.charts (
        site text NOT NULL,
        name text NOT NULL,
        spec jsonb NOT NULL,
        bundle_id bigint NOT NULL,
        PRIMARY KEY (site, name))""",
    # One row per promoted automation: the declaration, who to tell, and how
    # its last run went. The cadence lives in PULSE, keyed by (site, name).
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.automations (
        site text NOT NULL,
        name text NOT NULL,
        description text NOT NULL DEFAULT '',
        spec jsonb NOT NULL,
        submitted_by text NOT NULL,
        bundle_id bigint NOT NULL,
        last_run_at timestamptz,
        last_outcome text,
        last_rows_matched integer,
        last_error text,
        PRIMARY KEY (site, name))""",
)


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn)  # one transaction until commit/rollback


def ensure_registry(dsn: str) -> None:
    with _connect(dsn) as conn, conn.cursor() as cur:
        for stmt in REGISTRY_DDL:
            cur.execute(stmt)


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c.name for c in cur.description or ()]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# -- submit -------------------------------------------------------------------


def static_problems(project: KitProject, bundle_has_code: bool) -> list[str]:
    """What the node refuses before anyone looks: bad declarations, or code."""
    ds = decl.load(project)
    problems = list(ds.problems)
    if bundle_has_code:
        problems.append(
            "the bundle carries conform code (conform/*.py). The node does not run contributor "
            "code yet (ADR-203 decision 6): promote the declarations, and keep the normalizer "
            "in your site's reviewed package until the isolated conform worker exists"
        )
    if not (ds.gold or ds.verbs or ds.charts or ds.automations):
        problems.append("the bundle declares no gold object, verb or chart; there is nothing to promote")
    return problems


def submit(dsn: str, data: bytes, *, caller_site: str | None, principal: str, home: bool) -> dict:
    """Receive a bundle. A site's credential submits for its own site only."""
    ensure_registry(dsn)
    with tempfile.TemporaryDirectory(prefix="axiom-kit-") as tmp:
        project = load(unpack(data, Path(tmp)))
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        if not home and project.tenant != (caller_site or ""):
            raise KitError(
                f"this credential is bound to site {caller_site!r} and may only promote a kit for "
                f"that site; the bundle is for {project.tenant!r}"
            )
        names = sorted(p.relative_to(project.root).as_posix() for p in project.root.rglob("*") if p.is_file())
        has_code = any(n.startswith("conform/") for n in names)
        problems = static_problems(project, has_code)
    state = "rejected" if problems else "checked"
    manifest = {"tenant": project.tenant, "sha256": sha, "files": names, "has_code": has_code}
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {SCHEMA}.bundles (site, sha256, submitted_by, state, problems, manifest, bundle)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING id""",
            (project.tenant, sha, principal, state, json.dumps(problems), json.dumps(manifest), data),
        )
        (bundle_id,) = cur.fetchone()
    return {"id": bundle_id, "site": project.tenant, "state": state, "problems": problems, "sha256": sha}


def review(dsn: str, *, site: str | None = None, state: str | None = None) -> list[dict]:
    ensure_registry(dsn)
    where, args = [], []
    if site:
        where.append("site = %s")
        args.append(site)
    if state:
        where.append("state = %s")
        args.append(state)
    sql = (
        f"SELECT id, site, sha256, submitted_by, submitted_at, state, problems, manifest, "
        f"approved_by, applied_at FROM {SCHEMA}.bundles"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY id DESC LIMIT 100"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return _rows(cur)


# -- apply ----------------------------------------------------------------------


def _reader_policies(cur) -> list[str]:
    """Give every role that reads silver today a policy, before security is enabled.

    A role that owns the table is not subject to row-level security and needs
    nothing. Every other role holding SELECT on ``silver.signals`` (the gold door's
    role, a dashboard, conform) would otherwise see no rows the moment the first
    kit is applied.
    """
    cur.execute(
        """SELECT DISTINCT grantee FROM information_schema.role_table_grants
           WHERE table_schema = 'silver' AND table_name = 'signals' AND privilege_type = 'SELECT'
             AND grantee NOT LIKE 'kit\\_%' AND grantee <> 'PUBLIC'"""
    )
    readers = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT current_user, tableowner FROM pg_tables WHERE schemaname='silver' AND tablename='signals'")
    me, owner = cur.fetchone()
    if me != owner and me not in readers:
        readers.append(me)
    from psycopg import sql as _sql

    done = []
    for role in readers:
        if role == owner:
            continue
        name = f"reader_{role}"[:63]
        cur.execute(
            "SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='signals' AND policyname=%s",
            (name,),
        )
        if cur.fetchone():
            continue
        cur.execute(
            _sql.SQL("CREATE POLICY {} ON silver.signals FOR SELECT TO {} USING (true)").format(
                _sql.Identifier(name), _sql.Identifier(role)
            )
        )
        done.append(role)
    return done


def _prepare_site(cur, project: KitProject) -> None:
    from psycopg import sql as _sql

    role, schema, site = project.role, project.gold_schema, project.tenant
    cur.execute(
        "SELECT format('%I.%I', schemaname, viewname) FROM pg_views WHERE schemaname = 'gold'"
    )
    for (view,) in cur.fetchall():
        cur.execute(f"ALTER VIEW {view} SET (security_invoker = true)")
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if not cur.fetchone():
        cur.execute(_sql.SQL("CREATE ROLE {} NOLOGIN").format(_sql.Identifier(role)))
    cur.execute(_sql.SQL("GRANT {} TO CURRENT_USER").format(_sql.Identifier(role)))
    cur.execute(_sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_sql.Identifier(schema)))
    cur.execute(
        _sql.SQL("GRANT USAGE ON SCHEMA silver, gold, {} TO {}").format(
            _sql.Identifier(schema), _sql.Identifier(role)
        )
    )
    cur.execute(_sql.SQL("GRANT SELECT ON silver.signals TO {}").format(_sql.Identifier(role)))
    cur.execute(_sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA gold TO {}").format(_sql.Identifier(role)))
    cur.execute("ALTER TABLE silver.signals ENABLE ROW LEVEL SECURITY")
    cur.execute(
        _sql.SQL("DROP POLICY IF EXISTS {} ON silver.signals").format(_sql.Identifier(f"{role}_rows"))
    )
    cur.execute(
        _sql.SQL("CREATE POLICY {} ON silver.signals FOR SELECT TO {} USING (site = {})").format(
            _sql.Identifier(f"{role}_rows"), _sql.Identifier(role), _sql.Literal(site)
        )
    )
    # Where the site's automations write. Owned by the site's role, so a fire
    # running as that role can write it, a verb can read it back, and no other
    # site's role can do either.
    give_annotations(cur, schema, role)


def give_annotations(cur, schema: str, role: str) -> None:
    """Create ``<schema>.annotations`` and hand it to ``role``.

    A new owner needs CREATE on the schema, which the site's role otherwise
    never holds, so it is granted for the hand-over and revoked in the same
    transaction: the role owns its table and still cannot create anything.
    """
    from psycopg import sql as _sql

    s, r = _sql.Identifier(schema), _sql.Identifier(role)
    cur.execute(ANNOTATIONS_DDL.format(schema=schema))
    cur.execute(_sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(s, r))
    cur.execute(_sql.SQL("ALTER TABLE {}.annotations OWNER TO {}").format(s, r))
    cur.execute(_sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(s, r))


#: A site's annotations: rows an automation marked, readable by its verbs.
ANNOTATIONS_DDL = """CREATE TABLE IF NOT EXISTS "{schema}".annotations (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL,
    channel text,
    label text NOT NULL,
    note text NOT NULL DEFAULT '',
    automation text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now())"""


def _apply_views(cur, project: KitProject, ds: decl.Declarations) -> list[str]:
    from psycopg import sql as _sql

    schema, role = project.gold_schema, project.role
    cur.execute(
        "SELECT viewname FROM pg_views WHERE schemaname = %s", (schema,)
    )
    for (old,) in cur.fetchall():
        cur.execute(_sql.SQL("DROP VIEW IF EXISTS {}.{} CASCADE").format(_sql.Identifier(schema), _sql.Identifier(old)))
    done = []
    for g in ds.gold:
        if not _IDENT.match(g.name):
            raise KitError(f"gold object name {g.name!r} must be lowercase letters, digits and _")
        cur.execute(f'CREATE VIEW "{schema}"."{g.name}" WITH (security_invoker = true) AS {g.sql}')
        cur.execute(_sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
            _sql.Identifier(schema), _sql.Identifier(g.name), _sql.Identifier(role)))
        done.append(f"{schema}.{g.name}")
    return done


def _register(cur, project: KitProject, ds: decl.Declarations, bundle_id: int, submitted_by: str) -> int:
    """Replace the site's registered verbs, charts and automations. Returns how
    many automations the site had before, so the caller knows whether the
    scheduler needs to hear about it."""
    site = project.tenant
    cur.execute(f"DELETE FROM {SCHEMA}.verbs WHERE site = %s", (site,))
    cur.execute(f"DELETE FROM {SCHEMA}.charts WHERE site = %s", (site,))
    cur.execute(f"DELETE FROM {SCHEMA}.automations WHERE site = %s", (site,))
    had = cur.rowcount or 0
    for a in ds.automations:
        cur.execute(
            f"INSERT INTO {SCHEMA}.automations (site, name, description, spec, submitted_by, bundle_id) "
            f"VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
            (site, a.name, a.description, json.dumps(a.to_record(), default=str), submitted_by, bundle_id),
        )
    for v in ds.verbs:
        cur.execute(
            f"INSERT INTO {SCHEMA}.verbs (site, name, description, object, sql, params, bundle_id) "
            f"VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (site, v.name, v.description, v.obj, v.sql, json.dumps(v.params, default=str), bundle_id),
        )
    for c in ds.charts:
        spec = json.loads(c.source.read_text(encoding="utf-8"))
        cur.execute(
            f"INSERT INTO {SCHEMA}.charts (site, name, spec, bundle_id) VALUES (%s,%s,%s::jsonb,%s)",
            (site, c.name, json.dumps(spec), bundle_id),
        )
    return had


def structural_proof(cur, project: KitProject, ds: decl.Declarations) -> list[str]:
    """Why this site's role could see another site's rows, from the catalog. ``[]`` = isolated."""
    role, schema, site = project.role, project.gold_schema, project.tenant
    out: list[str] = []
    cur.execute(
        """SELECT table_schema, table_name FROM information_schema.role_table_grants
           WHERE grantee = %s AND privilege_type = 'SELECT'
             AND NOT (table_schema = 'silver' AND table_name = 'signals')
             AND table_schema NOT IN ('gold', %s)""",
        (role, schema),
    )
    out += [f"{role} can read {s}.{t}, which is outside its site's objects" for s, t in cur.fetchall()]
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.signals'::regclass")
    if not cur.fetchone()[0]:
        out.append("row-level security is not enabled on silver.signals")
    cur.execute(
        "SELECT qual FROM pg_policies WHERE schemaname='silver' AND tablename='signals' AND %s = ANY(roles)",
        (role,),
    )
    quals = [q for (q,) in cur.fetchall()]
    if not quals:
        out.append(f"no policy admits {role} to silver.signals")
    elif any(site not in (q or "") for q in quals):
        out.append(f"a policy for {role} is not limited to site {site!r}: {quals}")
    cur.execute(
        """SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind = 'v' AND n.nspname IN ('gold', %s)
             AND NOT coalesce('security_invoker=true' = ANY(c.reloptions), false)""",
        (schema,),
    )
    out += [f"view {v} runs with its owner's rights, so row-level security does not reach through it"
            for (v,) in cur.fetchall()]
    cur.execute(
        """SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')
             AND n.nspname NOT LIKE 'pg\\_%%' AND n.nspname NOT LIKE '\\_timescaledb%%'
             AND has_function_privilege(%s, p.oid, 'EXECUTE')""",
        (role,),
    )
    out += [f"{role} can execute {f}, a SECURITY DEFINER function, which runs with its owner's "
            f"rights and so walks past row-level security" for (f,) in cur.fetchall()]
    cur.execute("SAVEPOINT verbs_as_role")
    try:
        cur.execute(f'SET LOCAL ROLE "{role}"')
        for v in ds.verbs:
            values = {k: spec.get("default") for k, spec in v.params.items()}
            cur.execute(v.sql.replace("{object}", f'"{schema}"."{v.obj}"'), values)
            cur.fetchall()
        cur.execute("RESET ROLE")
    except Exception as exc:  # noqa: BLE001 - reported as a proof failure
        cur.execute("ROLLBACK TO SAVEPOINT verbs_as_role")
        out.append(f"a verb does not run as {role}: {str(exc).splitlines()[0]}")
    return out


def approve(dsn: str, bundle_id: int, *, approver: str) -> dict:
    """Apply a checked bundle in one transaction, or apply nothing."""
    ensure_registry(dsn)
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT site, state, bundle, submitted_by FROM {SCHEMA}.bundles WHERE id = %s FOR UPDATE",
            (bundle_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KitError(f"no bundle {bundle_id}")
        site, state, data, submitted_by = row
        if state != "checked":
            raise KitError(f"bundle {bundle_id} is {state!r}; only a checked bundle can be approved")
    with tempfile.TemporaryDirectory(prefix="axiom-kit-") as tmp:
        project = load(unpack(bytes(data), Path(tmp)))
        ds = decl.load(project)
        if ds.problems:
            raise KitError("the bundle no longer loads cleanly: " + "; ".join(ds.problems))
        import psycopg

        try:
            with _connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                readers = _reader_policies(cur)
                _prepare_site(cur, project)
                views = _apply_views(cur, project, ds)
                had_automations = _register(cur, project, ds, bundle_id, submitted_by)
                problems = structural_proof(cur, project, ds)
                if problems:
                    raise KitError("; ".join(problems))
                cur.execute(
                    f"UPDATE {SCHEMA}.bundles SET state='superseded' WHERE site=%s AND state='applied'",
                    (site,),
                )
                cur.execute(
                    f"UPDATE {SCHEMA}.bundles SET state='applied', approved_by=%s, applied_at=now() WHERE id=%s",
                    (approver, bundle_id),
                )
        except (KitError, psycopg.Error) as exc:
            reason = str(exc).splitlines()[0]
            with _connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {SCHEMA}.bundles SET state='rejected', problems=%s::jsonb WHERE id=%s",
                    (json.dumps([reason]), bundle_id),
                )
            raise KitError(f"bundle {bundle_id} was not applied: {reason}") from None
    from . import automations

    # After the commit: the registry is the record and PULSE follows it. A kit
    # that never had an automation leaves the scheduler alone.
    scheduled = automations.sync_quietly(dsn) if (had_automations or ds.automations) else {}
    return {
        "id": bundle_id, "site": site, "state": "applied", "views": views,
        "verbs": [v.name for v in ds.verbs], "charts": [c.name for c in ds.charts],
        "automations": [a.name for a in ds.automations], "scheduled": scheduled,
        "reader_policies_added": readers,
    }


def retire(dsn: str, site: str, *, by: str) -> dict:
    from psycopg import sql as _sql

    schema = f"gold_{site.replace('-', '_')}"
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(_sql.Identifier(schema)))
        cur.execute(f"DELETE FROM {SCHEMA}.verbs WHERE site = %s", (site,))
        cur.execute(f"DELETE FROM {SCHEMA}.charts WHERE site = %s", (site,))
        cur.execute(f"DELETE FROM {SCHEMA}.automations WHERE site = %s", (site,))
        removed = cur.rowcount or 0
        cur.execute(
            f"UPDATE {SCHEMA}.bundles SET state='retired', approved_by=coalesce(approved_by,%s) "
            f"WHERE site=%s AND state='applied'",
            (by, site),
        )
    from . import automations

    return {"site": site, "state": "retired",
            "unscheduled": automations.sync_quietly(dsn) if removed else {}}


# -- serve ----------------------------------------------------------------------


def list_verbs(dsn: str, *, site: str | None) -> list[dict]:
    ensure_registry(dsn)
    with _connect(dsn) as conn, conn.cursor() as cur:
        if site:
            cur.execute(f"SELECT site, name, description, object, params FROM {SCHEMA}.verbs WHERE site=%s ORDER BY name", (site,))
        else:
            cur.execute(f"SELECT site, name, description, object, params FROM {SCHEMA}.verbs ORDER BY site, name")
        return _rows(cur)


def run_registered_verb(dsn: str, *, site: str, name: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Run one promoted verb under its site's role, with only its declared parameters."""
    ensure_registry(dsn)
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT object, sql, params FROM {SCHEMA}.verbs WHERE site=%s AND name=%s", (site, name))
        row = cur.fetchone()
        if row is None:
            raise KitError(f"site {site!r} has no promoted verb {name!r}")
        obj, sql, declared = row
        unknown = sorted(set(params or {}) - set(declared or {}))
        if unknown:
            raise KitError(f"{name} takes {sorted(declared or {})}; it has no parameter {unknown}")
        values = {k: (spec or {}).get("default") for k, spec in (declared or {}).items()}
        values.update(params or {})
        schema, role = f"gold_{site.replace('-', '_')}", f"kit_{site.replace('-', '_')}"
        cur.execute(f"SET LOCAL statement_timeout = {VERB_TIMEOUT_MS}")
        cur.execute(f'SET LOCAL ROLE "{role}"')
        cur.execute(sql.replace("{object}", f'"{schema}"."{obj}"'), values)
        rows = _rows(cur)
        conn.rollback()  # a verb only reads; nothing it did is kept
        return rows


def list_automations(dsn: str, *, site: str | None) -> list[dict]:
    """The promoted automations, with their schedule and how their last run went."""
    from . import automations

    rows = automations.registered(dsn, site=site)
    pulse = automations.schedule_state(site) if rows else {}
    out = []
    for r in rows:
        a = automations.Automation.from_record(r["name"], r["spec"])
        sched = pulse.get((r["site"], r["name"])) or {}
        out.append({
            "site": r["site"], "name": r["name"], "description": r["description"],
            "verb": a.verb, "schedule": a.schedule_text(), "when": a.condition_text(),
            "then": [x for x, on in (("notify the submitter", a.notify),
                                     (f"annotate {(a.annotate or {}).get('label')!r}", a.annotate)) if on],
            "scheduled": sched.get("state") or "not scheduled", "next_run_at": sched.get("next_fire_at"),
            "last_run_at": r["last_run_at"], "last_outcome": r["last_outcome"],
            "last_rows_matched": r["last_rows_matched"], "last_error": r["last_error"],
        })
    return out


def list_charts(dsn: str, *, site: str | None) -> list[dict]:
    ensure_registry(dsn)
    with _connect(dsn) as conn, conn.cursor() as cur:
        if site:
            cur.execute(f"SELECT site, name, spec FROM {SCHEMA}.charts WHERE site=%s ORDER BY name", (site,))
        else:
            cur.execute(f"SELECT site, name, spec FROM {SCHEMA}.charts ORDER BY site, name")
        return _rows(cur)


__all__ = [
    "approve", "ensure_registry", "list_automations", "list_charts", "list_verbs", "retire", "review",
    "run_registered_verb", "static_problems", "structural_proof", "submit", "Bundle",
]
