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

Nothing on the submit or serve path runs DDL. The registry, the serving role's
grants, the reader policies and row-level security on ``silver.signals`` are
made once by the operator (:func:`setup`, ``data kit-host-setup``) with the
database's writer role. The serving app's login may be read-only and holds only
what :func:`setup` grants it; a node that was never set up answers a submit with
:class:`RegistryNotReady` (HTTP 503 at the door), naming that command.
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
#: How many times an apply waits for its locks on a busy node before it gives
#: up and leaves the bundle ``checked`` (with the error recorded) to try again.
LOCK_ATTEMPTS = 4
LOCK_BACKOFF_S = 2.0
SETUP_COMMAND = "data kit-host-setup --serve-role <the serving app's database login>"
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
    # Expand only: a failed approve that is worth retrying records why here and
    # leaves the bundle checked.
    f"ALTER TABLE {SCHEMA}.bundles ADD COLUMN IF NOT EXISTS last_error text",
    f"ALTER TABLE {SCHEMA}.bundles ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz",
    # Who owns each object a bundle created at a site (ownership within a site):
    # a bundle replaces or drops only its own submitter's objects unless the
    # operator approves with an override.
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.objects (
        site text NOT NULL,
        kind text NOT NULL CHECK (kind IN ('gold','verb','chart','automation')),
        name text NOT NULL,
        principal text NOT NULL,
        bundle_id bigint NOT NULL,
        PRIMARY KEY (site, kind, name))""",
    # A contributor asks; the operator applies it with kit-retire --principal.
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.withdrawals (
        id bigserial PRIMARY KEY,
        site text NOT NULL,
        principal text NOT NULL,
        reason text NOT NULL DEFAULT '',
        requested_at timestamptz NOT NULL DEFAULT now(),
        state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','applied')),
        applied_by text,
        applied_at timestamptz)""",
    # The audit record: what each retire, rollback and override did. Never deleted.
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.events (
        id bigserial PRIMARY KEY,
        site text NOT NULL,
        at timestamptz NOT NULL DEFAULT now(),
        by text NOT NULL,
        action text NOT NULL,
        detail jsonb NOT NULL DEFAULT '{{}}'::jsonb)""",
    # What setup was told: the serving login every approve hands a site's role to.
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.settings (
        key text PRIMARY KEY,
        value text NOT NULL)""",
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


class RegistryNotReady(Exception):  # noqa: N818 - reads as the state it reports
    """The node's kit registry was never set up, or the serving login lacks its grants.

    Not a :class:`KitError`: the contributor did nothing wrong, and the door
    answers it as 503 (the node is not ready), never 500.
    """


def _not_ready(detail: str) -> RegistryNotReady:
    return RegistryNotReady(
        f"this node is not set up to take kit bundles ({detail}). An operator runs, once, "
        f"with the database's writer role: {SETUP_COMMAND}"
    )


def _require(cur) -> None:
    """Refuse, without DDL, when the registry does not exist."""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{SCHEMA}.bundles",))
    if not cur.fetchone()[0]:
        raise _not_ready(f"there is no {SCHEMA} schema")


def require_registry(dsn: str) -> None:
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)


def setup(dsn: str, *, serve_role: str | None = None) -> dict[str, Any]:
    """The operator's one-time step (``kit-host-setup``): everything the hot paths never do.

    Run with the database's writer role. Idempotent. It

    - creates the registry (schema, tables);
    - grants ``serve_role`` (the serving app's login, which may be read-only)
      exactly what the served verbs need: USAGE on the schema, INSERT on
      ``bundles`` with SELECT on its ``id`` (``RETURNING``) and its sequence, and
      SELECT on ``verbs``, ``charts`` and ``automations``; it is recorded so each
      approve hands the new site's role to it (``kit-verb`` runs as that role);
    - gives every current reader of ``silver.signals`` its policy and turns
      row-level security on, so an approve on a live node does not take the
      hot table's ACCESS EXCLUSIVE lock to do it.
    """
    from psycopg import sql as _sql

    done: list[str] = []
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        for stmt in REGISTRY_DDL:
            cur.execute(stmt)
        done.append(f"registry {SCHEMA}")
        if serve_role:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (serve_role,))
            if not cur.fetchone():
                raise KitError(f"there is no database role {serve_role!r} to grant the serving rights to")
            r, sch = _sql.Identifier(serve_role), _sql.Identifier(SCHEMA)
            cur.execute(_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, r))
            cur.execute(_sql.SQL("GRANT INSERT, SELECT (id) ON {}.bundles TO {}").format(sch, r))
            cur.execute(_sql.SQL("GRANT USAGE ON SEQUENCE {}.bundles_id_seq TO {}").format(sch, r))
            cur.execute(_sql.SQL("GRANT SELECT ON {}.verbs, {}.charts, {}.automations TO {}").format(
                sch, sch, sch, r))
            # kit-withdraw, served: a request is a row, never a drop.
            cur.execute(_sql.SQL("GRANT INSERT, SELECT (id, site, principal, state) ON {}.withdrawals TO {}")
                        .format(sch, r))
            cur.execute(_sql.SQL("GRANT USAGE ON SEQUENCE {}.withdrawals_id_seq TO {}").format(sch, r))
            cur.execute(
                f"INSERT INTO {SCHEMA}.settings (key, value) VALUES ('serve_role', %s) "
                f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (serve_role,),
            )
            cur.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'kit\\_%' AND NOT rolcanlogin")
            for (site_role,) in cur.fetchall():
                cur.execute(_sql.SQL("GRANT {} TO {}").format(_sql.Identifier(site_role), r))
            done.append(f"serving grants for {serve_role}")
        cur.execute("SELECT to_regclass('silver.signals') IS NOT NULL")
        if cur.fetchone()[0]:
            readers = _reader_policies(cur)
            cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.signals'::regclass")
            if not cur.fetchone()[0]:
                cur.execute("ALTER TABLE silver.signals ENABLE ROW LEVEL SECURITY")
                done.append("row-level security on silver.signals")
            done += [f"reader policy for {r}" for r in readers]
    return {"done": done, "serve_role": serve_role}


def ensure_registry(dsn: str) -> None:
    """The registry's DDL alone. Operator and test use only: nothing served calls it."""
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
    """Receive a bundle. A site's credential submits for its own site only. Runs no DDL."""
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
    import psycopg

    try:
        with _connect(dsn) as conn, conn.cursor() as cur:
            _require(cur)
            cur.execute(
                f"""INSERT INTO {SCHEMA}.bundles (site, sha256, submitted_by, state, problems, manifest, bundle)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING id""",
                (project.tenant, sha, principal, state, json.dumps(problems), json.dumps(manifest), data),
            )
            (bundle_id,) = cur.fetchone()
    except psycopg.errors.InsufficientPrivilege as exc:
        raise _not_ready(f"this login may not write the registry: {str(exc).splitlines()[0]}") from None
    return {"id": bundle_id, "site": project.tenant, "state": state, "problems": problems, "sha256": sha}


def review(dsn: str, *, site: str | None = None, state: str | None = None) -> list[dict]:
    where, args = [], []
    if site:
        where.append("site = %s")
        args.append(site)
    if state:
        where.append("state = %s")
        args.append(state)
    sql = (
        f"SELECT id, site, sha256, submitted_by, submitted_at, state, problems, manifest, "
        f"approved_by, applied_at, last_error, last_attempt_at FROM {SCHEMA}.bundles"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY id DESC LIMIT 100"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
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
    """The site's role, schema and row policy. Each step runs only when it is missing.

    ``silver.signals`` is the node's hot table, and every policy or security change
    on it takes an ACCESS EXCLUSIVE lock that waits behind (and then blocks) every
    reader and writer. Setup turns security on once; here the site's own policy is
    created only on the site's first apply, or when its condition changed.
    """
    from psycopg import sql as _sql

    role, schema, site = project.role, project.gold_schema, project.tenant
    cur.execute(
        """SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind = 'v' AND n.nspname = 'gold'
             AND NOT coalesce('security_invoker=true' = ANY(c.reloptions), false)"""
    )
    for (view,) in cur.fetchall():
        cur.execute(f"ALTER VIEW {view} SET (security_invoker = true)")
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if not cur.fetchone():
        cur.execute(_sql.SQL("CREATE ROLE {} NOLOGIN").format(_sql.Identifier(role)))
    cur.execute(_sql.SQL("GRANT {} TO CURRENT_USER").format(_sql.Identifier(role)))
    cur.execute(f"SELECT value FROM {SCHEMA}.settings WHERE key = 'serve_role'")
    serve = cur.fetchone()
    if serve:
        # kit-verb, served, runs as the site's role from the serving login.
        cur.execute(_sql.SQL("GRANT {} TO {}").format(_sql.Identifier(role), _sql.Identifier(serve[0])))
    cur.execute(_sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_sql.Identifier(schema)))
    cur.execute(
        _sql.SQL("GRANT USAGE ON SCHEMA silver, gold, {} TO {}").format(
            _sql.Identifier(schema), _sql.Identifier(role)
        )
    )
    cur.execute("SELECT has_table_privilege(%s, 'silver.signals', 'SELECT')", (role,))
    if not cur.fetchone()[0]:
        cur.execute(_sql.SQL("GRANT SELECT ON silver.signals TO {}").format(_sql.Identifier(role)))
    cur.execute(_sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA gold TO {}").format(_sql.Identifier(role)))
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.signals'::regclass")
    if not cur.fetchone()[0]:  # setup was skipped; still correct, just not lock-free
        cur.execute("ALTER TABLE silver.signals ENABLE ROW LEVEL SECURITY")
    policy = f"{role}_rows"
    cur.execute(
        "SELECT qual FROM pg_policies WHERE schemaname='silver' AND tablename='signals' AND policyname=%s",
        (policy,),
    )
    have = cur.fetchone()
    if have is None or site not in (have[0] or ""):
        cur.execute(_sql.SQL("DROP POLICY IF EXISTS {} ON silver.signals").format(_sql.Identifier(policy)))
        cur.execute(
            _sql.SQL("CREATE POLICY {} ON silver.signals FOR SELECT TO {} USING (site = {})").format(
                _sql.Identifier(policy), _sql.Identifier(role), _sql.Literal(site)
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


class OwnershipConflict(KitError):
    """A bundle would replace or drop another contributor's objects at its site."""


#: (kind, name) -> principal for one site.
Owners = dict[tuple[str, str], str]


def _owners(cur, site: str, schema: str) -> Owners:
    """Who owns each of the site's objects, adopting any created before ownership was recorded.

    An unrecorded verb, chart or automation belongs to whoever submitted the bundle
    that registered it; an unrecorded view to the submitter of the site's latest
    applied bundle. What cannot be traced is owned by nobody (``""``).
    """
    cur.execute(f"SELECT kind, name, principal FROM {SCHEMA}.objects WHERE site = %s", (site,))
    owners: Owners = {(k, n): p for k, n, p in cur.fetchall()}
    for kind, table in (("verb", "verbs"), ("chart", "charts"), ("automation", "automations")):
        cur.execute(
            f"SELECT t.name, coalesce(b.submitted_by, '') FROM {SCHEMA}.{table} t "
            f"LEFT JOIN {SCHEMA}.bundles b ON b.id = t.bundle_id WHERE t.site = %s",
            (site,),
        )
        for name, who in cur.fetchall():
            owners.setdefault((kind, name), who)
    cur.execute(
        f"SELECT submitted_by FROM {SCHEMA}.bundles WHERE site = %s AND state = 'applied' "
        f"ORDER BY applied_at DESC NULLS LAST, id DESC LIMIT 1",
        (site,),
    )
    latest = cur.fetchone()
    cur.execute("SELECT viewname FROM pg_views WHERE schemaname = %s", (schema,))
    for (view,) in cur.fetchall():
        owners.setdefault(("gold", view), latest[0] if latest else "")
    return owners


def _declared(ds: decl.Declarations) -> set[tuple[str, str]]:
    return ({("gold", g.name) for g in ds.gold} | {("verb", v.name) for v in ds.verbs}
            | {("chart", c.name) for c in ds.charts} | {("automation", a.name) for a in ds.automations})


def dependents(cur, views: list[str], *, inside: set[str]) -> list[str]:
    """Every relation that depends, directly or through others, on ``views`` (``schema.name``)
    and is not itself in ``inside``. What a CASCADE would take with it."""
    if not views:
        return []
    cur.execute(
        """WITH RECURSIVE dep(oid) AS (
               SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE format('%%s.%%s', n.nspname, c.relname) = ANY(%s)
             UNION
               SELECT r.ev_class FROM pg_depend d
               JOIN pg_rewrite r ON r.oid = d.objid
               JOIN dep ON d.refobjid = dep.oid
               WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
                 AND r.ev_class <> dep.oid)
           SELECT DISTINCT format('%%s.%%s', n.nspname, c.relname)
           FROM dep JOIN pg_class c ON c.oid = dep.oid JOIN pg_namespace n ON n.oid = c.relnamespace
           ORDER BY 1""",
        (list(views),),
    )
    return [r for (r,) in cur.fetchall() if r not in inside]


def _plan_replace(cur, project: KitProject, ds: decl.Declarations, principal: str,
                  override: bool) -> tuple[set[tuple[str, str]], int]:
    """What this bundle removes before it registers its own: its submitter's objects
    (and untraceable ones). Raises :class:`OwnershipConflict` when it would replace
    or drop another contributor's, unless the operator overrides."""
    site, schema = project.tenant, project.gold_schema
    owners = _owners(cur, site, schema)
    declared = _declared(ds)
    theirs = sorted(f"{k} {n} (owned by {owners[(k, n)]})" for k, n in declared
                    if owners.get((k, n)) not in (None, "", principal))
    mine = {key for key, who in owners.items() if who in ("", principal)}
    drop = mine | {key for key in declared if key in owners}
    views = [f"{schema}.{n}" for k, n in drop if k == "gold"]
    inside = {f"{schema}.{n}" for k, n in declared if k == "gold"} | set(views)
    outside = dependents(cur, views, inside=inside)
    if (theirs or outside) and not override:
        raise OwnershipConflict(
            "this bundle would replace another contributor's objects at "
            f"{site}: {'; '.join(theirs + [f'{o} depends on a view it replaces' for o in outside])}. "
            "An operator can approve it anyway with --override"
        )
    had = sum(1 for k, _ in drop if k == "automation")
    return drop, had


def _apply_views(cur, project: KitProject, ds: decl.Declarations,
                 drop: set[tuple[str, str]] | None = None) -> list[str]:
    from psycopg import sql as _sql

    schema, role = project.gold_schema, project.role
    cur.execute("SELECT viewname FROM pg_views WHERE schemaname = %s", (schema,))
    existing = {v for (v,) in cur.fetchall()}
    going = existing if drop is None else {n for k, n in drop if k == "gold"} & existing
    for old in sorted(going):
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


_TABLES = {"verb": "verbs", "chart": "charts", "automation": "automations"}


def _register(cur, project: KitProject, ds: decl.Declarations, bundle_id: int, submitted_by: str,
              drop: set[tuple[str, str]] | None = None) -> int:
    """Replace the submitter's registered verbs, charts and automations (every one at the
    site when ``drop`` is None) and record who owns what the bundle declares. Returns how
    many automations went, so the caller knows whether the scheduler needs to hear."""
    site = project.tenant
    had = 0
    if drop is None:
        for table in _TABLES.values():
            cur.execute(f"DELETE FROM {SCHEMA}.{table} WHERE site = %s", (site,))
            if table == "automations":
                had = cur.rowcount or 0
        cur.execute(f"DELETE FROM {SCHEMA}.objects WHERE site = %s", (site,))
    else:
        for kind, name in drop:
            if kind in _TABLES:
                cur.execute(f"DELETE FROM {SCHEMA}.{_TABLES[kind]} WHERE site = %s AND name = %s", (site, name))
                had += (cur.rowcount or 0) if kind == "automation" else 0
            cur.execute(f"DELETE FROM {SCHEMA}.objects WHERE site = %s AND kind = %s AND name = %s",
                        (site, kind, name))
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
    for kind, name in sorted(_declared(ds)):
        cur.execute(
            f"INSERT INTO {SCHEMA}.objects (site, kind, name, principal, bundle_id) VALUES (%s,%s,%s,%s,%s) "
            f"ON CONFLICT (site, kind, name) DO UPDATE SET principal = EXCLUDED.principal, "
            f"bundle_id = EXCLUDED.bundle_id",
            (site, kind, name, submitted_by, bundle_id),
        )
    return had


def _event(cur, site: str, by: str, action: str, detail: dict[str, Any]) -> None:
    cur.execute(
        f"INSERT INTO {SCHEMA}.events (site, by, action, detail) VALUES (%s,%s,%s,%s::jsonb)",
        (site, by, action, json.dumps(detail, default=str)),
    )


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


def _transient(exc: BaseException) -> bool:
    """A failure of the moment (a lock not granted in time, a deadlock, a dropped
    connection), not of the bundle. Retried, and never a reason to reject."""
    import psycopg

    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def _apply_once(dsn: str, project: KitProject, ds: decl.Declarations, bundle_id: int,
                site: str, submitted_by: str, approver: str, *, override: bool,
                action: str) -> tuple[list[str], list[str], int]:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        readers = _reader_policies(cur)
        _prepare_site(cur, project)
        drop, had_automations = _plan_replace(cur, project, ds, submitted_by, override)
        views = _apply_views(cur, project, ds, drop)
        _register(cur, project, ds, bundle_id, submitted_by, drop)
        problems = structural_proof(cur, project, ds)
        if problems:
            raise KitError("; ".join(problems))
        # The submitter's bundle in force is replaced; other contributors' stay applied.
        cur.execute(
            f"UPDATE {SCHEMA}.bundles SET state='superseded' "
            f"WHERE site=%s AND submitted_by=%s AND state='applied' AND id<>%s",
            (site, submitted_by, bundle_id),
        )
        cur.execute(
            f"UPDATE {SCHEMA}.bundles SET state='applied', approved_by=%s, applied_at=now(), "
            f"last_error=NULL, last_attempt_at=now() WHERE id=%s",
            (approver, bundle_id),
        )
        if override or action != "approve":
            _event(cur, site, approver, action, {"bundle": bundle_id, "override": override,
                                                 "replaced": sorted(f"{k}:{n}" for k, n in drop)})
    return readers, views, had_automations


def _record(dsn: str, bundle_id: int, *, reason: str, reject: bool) -> None:
    with _connect(dsn) as conn, conn.cursor() as cur:
        if reject:
            cur.execute(
                f"UPDATE {SCHEMA}.bundles SET state='rejected', problems=%s::jsonb, last_error=%s, "
                f"last_attempt_at=now() WHERE id=%s",
                (json.dumps([reason]), reason, bundle_id),
            )
        else:
            cur.execute(
                f"UPDATE {SCHEMA}.bundles SET last_error=%s, last_attempt_at=now() WHERE id=%s",
                (reason, bundle_id),
            )


def _load_bundle(dsn: str, bundle_id: int) -> tuple[str, str, bytes, str]:
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        # Applying is the operator's act with the writer role, so a registry set up
        # by an older release is brought forward here (expand-only DDL, off the hot table).
        for stmt in REGISTRY_DDL:
            cur.execute(stmt)
        cur.execute(
            f"SELECT site, state, bundle, submitted_by FROM {SCHEMA}.bundles WHERE id = %s",
            (bundle_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise KitError(f"no bundle {bundle_id}")
    site, state, data, submitted_by = row
    return site, state, bytes(data), submitted_by


def _apply(dsn: str, bundle_id: int, site: str, data: bytes, submitted_by: str, *, approver: str,
           override: bool, attempts: int, backoff_s: float, action: str, may_reject: bool) -> dict:
    import time

    import psycopg

    keep = "checked" if action == "approve" else "as it was"
    again = f"kit-approve {bundle_id}" if action == "approve" else f"kit-rollback {bundle_id} --site {site}"
    with tempfile.TemporaryDirectory(prefix="axiom-kit-") as tmp:
        project = load(unpack(data, Path(tmp)))
        ds = decl.load(project)
        if ds.problems:
            raise KitError("the bundle no longer loads cleanly: " + "; ".join(ds.problems))
        for attempt in range(1, max(1, attempts) + 1):
            try:
                readers, views, had_automations = _apply_once(
                    dsn, project, ds, bundle_id, site, submitted_by, approver,
                    override=override, action=action)
                break
            except (KitError, psycopg.Error) as exc:
                reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                if _transient(exc) and attempt < attempts:
                    time.sleep(backoff_s * (2 ** (attempt - 1)))
                    continue
                if isinstance(exc, OwnershipConflict):
                    _record(dsn, bundle_id, reason=reason, reject=False)
                    raise KitError(f"bundle {bundle_id} was not applied and is still {keep}: {reason}") from None
                if not may_reject or _transient(exc) or isinstance(exc, psycopg.errors.InsufficientPrivilege):
                    _record(dsn, bundle_id, reason=reason, reject=False)
                    raise KitError(
                        f"bundle {bundle_id} was not applied ({reason}) after {attempt} attempt(s). "
                        f"Nothing changed and it is still {keep}; run {again} again"
                        + (", at a quieter moment if the node is busy" if _transient(exc) else "")
                    ) from None
                _record(dsn, bundle_id, reason=reason, reject=True)
                raise KitError(f"bundle {bundle_id} was not applied: {reason}") from None
    from . import automations

    # After the commit: the registry is the record and PULSE follows it. A kit
    # that never had an automation leaves the scheduler alone.
    scheduled = automations.sync_quietly(dsn) if (had_automations or ds.automations) else {}
    return {
        "id": bundle_id, "site": site, "state": "applied", "views": views,
        "verbs": [v.name for v in ds.verbs], "charts": [c.name for c in ds.charts],
        "automations": [a.name for a in ds.automations], "scheduled": scheduled,
        "reader_policies_added": readers, "attempts": attempt, "principal": submitted_by,
    }


def approve(dsn: str, bundle_id: int, *, approver: str, override: bool = False,
            attempts: int = LOCK_ATTEMPTS, backoff_s: float = LOCK_BACKOFF_S) -> dict:
    """Apply a checked bundle in one transaction, or apply nothing.

    A lock that is not granted in time, or any other failure of the moment, is
    retried with backoff; if it still fails the bundle stays ``checked`` with the
    error recorded (``kit-review`` shows it) and can be approved again. A bundle
    that would replace another contributor's objects at its site also stays
    ``checked`` until an operator approves it with ``override``. Only a failure of
    the bundle itself (its isolation proof, SQL the database refuses) marks it
    ``rejected``.
    """
    site, state, data, submitted_by = _load_bundle(dsn, bundle_id)
    if state != "checked":
        raise KitError(f"bundle {bundle_id} is {state!r}; only a checked bundle can be approved")
    return _apply(dsn, bundle_id, site, data, submitted_by, approver=approver, override=override,
                  attempts=attempts, backoff_s=backoff_s, action="approve", may_reject=True)


def rollback(dsn: str, bundle_id: int, *, site: str, approver: str, override: bool = False,
             attempts: int = LOCK_ATTEMPTS, backoff_s: float = LOCK_BACKOFF_S) -> dict:
    """Re-apply an earlier bundle (``applied`` or ``superseded``) from the registry.

    The same apply and the same isolation proof as an approve. The submitter's
    bundle in force becomes ``superseded``. A failure never marks the earlier
    bundle ``rejected``: it was good once, and the reason is recorded instead.
    """
    found, state, data, submitted_by = _load_bundle(dsn, bundle_id)
    if found != site:
        raise KitError(f"bundle {bundle_id} is for site {found!r}, not {site!r}")
    if state not in ("applied", "superseded"):
        raise KitError(f"bundle {bundle_id} is {state!r}; only an applied or superseded bundle can be "
                       f"rolled back to")
    return _apply(dsn, bundle_id, site, data, submitted_by, approver=approver, override=override,
                  attempts=attempts, backoff_s=backoff_s, action="rollback", may_reject=False)


def _retire_plan(cur, site: str, principal: str | None) -> dict[str, Any]:
    schema = f"gold_{site.replace('-', '_')}"
    owners = _owners(cur, site, schema)
    keys = {k for k, who in owners.items() if principal is None or who == principal}
    views = sorted(f"{schema}.{n}" for k, n in keys if k == "gold")
    if principal is None:
        cur.execute(
            "SELECT format('%%s.%%s', n.nspname, c.relname) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind IN ('r','v','m')",
            (schema,),
        )
        inside = {r for (r,) in cur.fetchall()}
        relations = sorted(inside)
    else:
        inside, relations = set(views), views
    outside = dependents(cur, relations, inside=inside)
    if principal is not None:
        gone_views = {n for k, n in keys if k == "gold"}
        gone_verbs = {n for k, n in keys if k == "verb"}
        cur.execute(f"SELECT name, object FROM {SCHEMA}.verbs WHERE site = %s", (site,))
        outside += [f"verb {n} (owned by {owners.get(('verb', n)) or 'nobody'}) runs over {o}"
                    for n, o in cur.fetchall() if o in gone_views and ("verb", n) not in keys]
        cur.execute(f"SELECT name, spec FROM {SCHEMA}.automations WHERE site = %s", (site,))
        for n, spec in cur.fetchall():
            verb = (spec if isinstance(spec, dict) else json.loads(spec)).get("verb")
            if verb in gone_verbs and ("automation", n) not in keys:
                outside.append(f"automation {n} (owned by {owners.get(('automation', n)) or 'nobody'}) runs {verb}")
    would = (relations if principal is None else views) + sorted(
        f"{k} {n}" for k, n in keys if k != "gold")
    return {"schema": schema, "keys": keys, "would_drop": would, "dependents": outside}


def retire_plan(dsn: str, site: str, *, principal: str | None = None) -> dict:
    """What :func:`retire` would drop, and every dependent outside it. Changes nothing.

    ``kit-retire`` without ``--apply`` or ``--force`` shows this, so an operator sees the
    whole list before anything is dropped.
    """
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        for stmt in REGISTRY_DDL:  # as retire() does; rolled back below with everything else
            cur.execute(stmt)
        plan = _retire_plan(cur, site, principal)
        conn.rollback()
    return {"site": site, "principal": principal, "would_drop": plan["would_drop"],
            "dependents": plan["dependents"], "state": "planned"}


def retire(dsn: str, site: str, *, by: str, principal: str | None = None, force: bool = False) -> dict:
    """Remove a site's promoted objects: all of them, or one contributor's (``principal``).

    RESTRICT by default: it lists every object it would drop and every dependent
    outside them (a view in another schema, another contributor's verb), and when
    there is one it drops nothing. ``force`` drops them too (CASCADE), after the
    same list. The bundles stay in the registry as the audit record, and the drop
    itself is written to ``events``. A pending withdrawal from ``principal`` is
    marked applied.
    """
    from psycopg import sql as _sql

    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        for stmt in REGISTRY_DDL:
            cur.execute(stmt)
        plan = _retire_plan(cur, site, principal)
        result = {"site": site, "principal": principal, "would_drop": plan["would_drop"],
                  "dependents": plan["dependents"], "forced": bool(force)}
        if plan["dependents"] and not force:
            conn.rollback()
            raise RetireRefused(result)
        if principal is None:
            cur.execute(_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(_sql.Identifier(plan["schema"])))
            removed = 0
            for table in _TABLES.values():
                cur.execute(f"DELETE FROM {SCHEMA}.{table} WHERE site = %s", (site,))
                if table == "automations":
                    removed = cur.rowcount or 0
            cur.execute(f"DELETE FROM {SCHEMA}.objects WHERE site = %s", (site,))
            cur.execute(
                f"UPDATE {SCHEMA}.bundles SET state='retired', approved_by=coalesce(approved_by,%s) "
                f"WHERE site=%s AND state='applied'",
                (by, site),
            )
        else:
            removed = 0
            for kind, name in sorted(plan["keys"]):
                if kind == "gold":
                    cur.execute(_sql.SQL("DROP VIEW IF EXISTS {}.{} CASCADE").format(
                        _sql.Identifier(plan["schema"]), _sql.Identifier(name)))
                else:
                    cur.execute(f"DELETE FROM {SCHEMA}.{_TABLES[kind]} WHERE site = %s AND name = %s",
                                (site, name))
                    removed += (cur.rowcount or 0) if kind == "automation" else 0
                cur.execute(f"DELETE FROM {SCHEMA}.objects WHERE site=%s AND kind=%s AND name=%s",
                            (site, kind, name))
            cur.execute(
                f"UPDATE {SCHEMA}.bundles SET state='retired', approved_by=coalesce(approved_by,%s) "
                f"WHERE site=%s AND submitted_by=%s AND state='applied'",
                (by, site, principal),
            )
            cur.execute(
                f"UPDATE {SCHEMA}.withdrawals SET state='applied', applied_by=%s, applied_at=now() "
                f"WHERE site=%s AND principal=%s AND state='pending'",
                (by, site, principal),
            )
            result["withdrawals_applied"] = cur.rowcount or 0
        _event(cur, site, by, "retire", result)
    from . import automations

    return {**result, "state": "retired",
            "unscheduled": automations.sync_quietly(dsn) if removed else {}}


class RetireRefused(KitError):
    """A retire that would take objects outside the kit (or another contributor's) with it."""

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        super().__init__(
            f"refusing to retire {plan['site']}"
            + (f" objects of {plan['principal']}" if plan.get("principal") else "")
            + ": these depend on what it would drop: " + "; ".join(plan["dependents"])
            + ". Nothing was dropped. kit-retire --force also drops the dependents outside the kit (CASCADE)"
        )


def request_withdraw(dsn: str, *, site: str, principal: str, caller_site: str | None, home: bool,
                     reason: str = "") -> dict:
    """A contributor's request to have their objects at ``site`` removed. Runs no DDL and
    drops nothing: it is one row the operator sees in kit-review and applies with
    ``kit-retire --site S --principal P``."""
    import psycopg

    if not home and site != (caller_site or ""):
        raise KitError(f"this credential is bound to site {caller_site!r}; it may only withdraw from that site")
    try:
        with _connect(dsn) as conn, conn.cursor() as cur:
            _require(cur)
            cur.execute(
                f"SELECT id FROM {SCHEMA}.withdrawals WHERE site=%s AND principal=%s AND state='pending'",
                (site, principal),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"INSERT INTO {SCHEMA}.withdrawals (site, principal, reason) VALUES (%s,%s,%s) RETURNING id",
                    (site, principal, reason),
                )
                row = cur.fetchone()
    except psycopg.errors.InsufficientPrivilege as exc:
        raise _not_ready(f"this login may not write the registry: {str(exc).splitlines()[0]}") from None
    return {"id": row[0], "site": site, "principal": principal, "state": "pending"}


def withdrawals(dsn: str, *, site: str | None = None, state: str | None = "pending") -> list[dict]:
    where, args = [], []
    if site:
        where.append("site = %s")
        args.append(site)
    if state:
        where.append("state = %s")
        args.append(state)
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{SCHEMA}.withdrawals",))
        if not cur.fetchone()[0]:
            return []
        cur.execute(
            f"SELECT id, site, principal, reason, requested_at, state, applied_by, applied_at "
            f"FROM {SCHEMA}.withdrawals" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY id DESC LIMIT 100",
            args,
        )
        return _rows(cur)


def owners(dsn: str, *, site: str) -> list[dict]:
    """Each promoted object at ``site`` and the principal that owns it."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        for stmt in REGISTRY_DDL:
            cur.execute(stmt)
        found = _owners(cur, site, f"gold_{site.replace('-', '_')}")
    return [{"kind": k, "name": n, "principal": p} for (k, n), p in sorted(found.items())]


# -- serve ----------------------------------------------------------------------


def list_verbs(dsn: str, *, site: str | None) -> list[dict]:
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        if site:
            cur.execute(f"SELECT site, name, description, object, params FROM {SCHEMA}.verbs WHERE site=%s ORDER BY name", (site,))
        else:
            cur.execute(f"SELECT site, name, description, object, params FROM {SCHEMA}.verbs ORDER BY site, name")
        return _rows(cur)


def run_registered_verb(dsn: str, *, site: str, name: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Run one promoted verb under its site's role, with only its declared parameters.

    Bounded by ``VERB_TIMEOUT_MS``; a verb that runs past it is a :class:`KitError` naming the
    bound, never an unhandled ``QueryCanceled``."""
    import psycopg

    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
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
        try:
            cur.execute(sql.replace("{object}", f'"{schema}"."{obj}"'), values)
        except psycopg.errors.QueryCanceled:
            # The node's statement timeout, not a crash: a refusal the caller can act on.
            # Uncaught, the door answered it as HTTP 500.
            conn.rollback()
            raise KitError(
                f"the verb {name!r} did not finish within {VERB_TIMEOUT_MS / 1000:g}s; narrow its window "
                f"(the time bound in gold/{obj}.sql, or the verb's own parameters) and promote again"
            ) from None
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
    with _connect(dsn) as conn, conn.cursor() as cur:
        _require(cur)
        if site:
            cur.execute(f"SELECT site, name, spec FROM {SCHEMA}.charts WHERE site=%s ORDER BY name", (site,))
        else:
            cur.execute(f"SELECT site, name, spec FROM {SCHEMA}.charts ORDER BY site, name")
        return _rows(cur)


__all__ = [
    "OwnershipConflict", "RegistryNotReady", "RetireRefused", "approve", "dependents", "ensure_registry",
    "owners", "request_withdraw", "require_registry", "rollback", "setup", "withdrawals", "list_automations", "list_charts", "list_verbs", "retire", "retire_plan", "review",
    "run_registered_verb", "static_problems", "structural_proof", "submit", "Bundle",
]
