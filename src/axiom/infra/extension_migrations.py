# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Migrations Axiom did not ship: trusted, pinned, planned, confined, audited (#2006).

An installed extension's manifest may declare ``[database]`` migrations (ADR-052). The
built-ins and portfolio packages ship with Axiom and run as they always have
(:func:`axiom.infra.db.extensions_with_migrations`). Everything else is *third-party*, and
its revisions are the first database changes Axiom runs that it did not ship, so:

1. **Opt-in trust.** Nothing runs until the node's operator trusts the extension
   (``db migrate upgrade --trust <ext>``), recorded in node settings
   (``db.trusted_migrations.<schema>.*``, global scope of the settings store).
2. **Pinned to content.** The record holds a hash of the migrations folder, the extension's
   version and the revisions it covered. A changed folder (an upgrade adds a revision) is
   ``stale``: the new revisions are named and nothing runs until it is trusted again.
3. **Plan before apply.** :func:`plan` reads only: pending revisions and, where Alembic can
   render it offline, the SQL. It creates nothing, not even the schema.
4. **Confinement.** The extension's own ``env.py`` is never executed: it would own the
   connection and the transaction. Axiom drives the revision scripts itself, in ONE
   transaction on its own connection with ``search_path`` set to the extension's schema,
   and compares the catalog (every schema but the extension's, roles, extensions, ...) and
   the per-table write counters before and after. Anything created, altered, dropped or
   written outside the schema rolls the whole transaction back, naming the objects.
   ADR-052 D6 (a per-extension role owning only its schema) is still deferred, so this is
   a check, not a privilege boundary: a revision script that opens a connection of its own
   is not seen by it.
5. **Audit.** Each applied revision is one record in the action audit chain
   (``skill = db.migrate.extension``) carrying extension, version, content hash, revision,
   and who trusted it when.
6. **Never automatic.** Only :func:`apply_trusted`, which only ``db migrate upgrade`` calls,
   applies them. ``provision_extension`` (start-up, serve, a store's first use) reports a
   third-party extension's pending revisions and points at the command instead.

Listing and status read revision ids with :mod:`ast` and never import the scripts; only the
plan's SQL rendering and an apply load them, and both are explicit operator commands.
"""

from __future__ import annotations

import ast
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger(__name__)

#: Where a trust record lives in node settings.
SETTINGS_PREFIX = "db.trusted_migrations"
#: The audit chain's skill name for one applied third-party revision.
AUDIT_SKILL = "db.migrate.extension"

# Values that go into the settings file. The store's fallback TOML writer does not escape
# strings, so anything an extension author controls is checked before it is written.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.+@:\-]{1,128}$")
_RESERVED_SCHEMAS = {"public", "information_schema"}


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


# ---------------------------------------------------------------- discovery


@dataclass(frozen=True)
class ThirdParty:
    """An installed extension, not shipped with Axiom, that declares migrations."""

    extension: str
    schema: str
    version: str
    directory: Path
    content_hash: str
    #: revision id -> its down revisions (from the scripts, read without importing them)
    graph: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Why its revisions cannot be read; empty when they can.
    error: str = ""

    @property
    def revisions(self) -> list[str]:
        """Every revision, base first."""
        return _ordered(self.graph, set(self.graph))


def content_hash(directory: Path) -> str:
    """SHA-256 over every file in the migrations folder (relative path and bytes)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = path.relative_to(directory)
        if "__pycache__" in rel.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(rel.as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _literal_ids(node: ast.expr) -> tuple[str, ...] | None:
    value = ast.literal_eval(node)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


def read_graph(directory: Path) -> dict[str, tuple[str, ...]]:
    """``{revision: down revisions}`` from ``versions/*.py``, parsed, never imported.

    Raises ``ValueError`` for a script whose ids are not plain literals or not safe ids.
    """
    graph: dict[str, tuple[str, ...]] = {}
    for script in sorted((directory / "versions").glob("*.py")):
        found: dict[str, tuple[str, ...] | None] = {}
        for node in ast.parse(script.read_text(encoding="utf-8")).body:
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                try:
                    found[target.id] = _literal_ids(value)
                except ValueError:
                    found[target.id] = None
        rev, down = found.get("revision"), found.get("down_revision", ())
        if not rev or len(rev) != 1 or down is None:
            raise ValueError(f"{script.name}: revision and down_revision must be plain literals")
        for rid in (*rev, *down):
            if not _SAFE_VALUE.match(rid):
                raise ValueError(f"{script.name}: revision id {rid!r} is not a plain identifier")
        graph[rev[0]] = down
    return graph


def _ordered(graph: dict[str, tuple[str, ...]], wanted: set[str]) -> list[str]:
    """``wanted`` in dependency order, base first (ties broken by id)."""
    out: list[str] = []
    seen: set[str] = set()

    def visit(rev: str) -> None:
        if rev in seen or rev not in graph:
            return
        seen.add(rev)
        for down in sorted(graph[rev]):
            visit(down)
        if rev in wanted:
            out.append(rev)

    for rev in sorted(wanted):
        visit(rev)
    return out


def _ancestors(graph: dict[str, tuple[str, ...]], heads: set[str]) -> set[str]:
    out: set[str] = set()
    stack = list(heads)
    while stack:
        rev = stack.pop()
        if rev in out:
            continue
        out.add(rev)
        stack.extend(graph.get(rev, ()))
    return out


def _shipped_roots() -> list[Path]:
    """The package folders whose migrations ship with Axiom (the tree scan's roots)."""
    import importlib.util

    from axiom.infra.branding import discover_portfolio_members

    packages = ["axiom"] + [m.package_name.replace("-", "_") for m in discover_portfolio_members()]
    roots: list[Path] = []
    for package in dict.fromkeys(packages):
        spec = importlib.util.find_spec(package)
        if spec is not None and spec.submodule_search_locations:
            roots.extend(Path(r).resolve() for r in spec.submodule_search_locations)
    return roots


def third_party() -> list[ThirdParty]:
    """Every installed extension declaring migrations that Axiom did not ship."""
    from axiom.extensions.discovery import discover_extensions
    from axiom.infra.db import declared_migration_dirs, extensions_with_migrations, normalize_extension_name

    builtin = extensions_with_migrations()
    taken = {normalize_extension_name(name) for name, _ in builtin}
    shipped = _shipped_roots()
    try:
        discovered = discover_extensions()
    except Exception as exc:  # noqa: BLE001 - discovery failing must not stop the built-ins migrating
        log.warning("extension discovery failed; no third-party migrations are visible: %s", exc)
        return []
    versions = {e.name: (e.version or "") for e in discovered}

    found: list[ThirdParty] = []
    by_schema: dict[str, list[str]] = {}
    for name, directory in declared_migration_dirs(skip={n for n, _ in builtin}, extensions=discovered):
        resolved = directory.resolve()
        if any(resolved.is_relative_to(root) for root in shipped):
            continue  # ships with Axiom; not a third-party extension
        schema = normalize_extension_name(name)
        if schema in _RESERVED_SCHEMAS or schema.startswith("pg_") or schema in taken:
            log.warning("%s: its schema %r is reserved or a built-in's; its migrations are ignored", name, schema)
            continue
        by_schema.setdefault(schema, []).append(name)
        version = versions.get(name, "")
        if not _SAFE_VALUE.match(version or "-"):
            version = "unreadable:" + hashlib.sha256(version.encode()).hexdigest()[:12]
        try:
            graph, error = read_graph(directory), ""
        except (OSError, SyntaxError, ValueError) as exc:
            graph, error = {}, f"its revisions cannot be read: {exc}"
        found.append(ThirdParty(
            extension=name, schema=schema, version=version, directory=directory,
            content_hash=content_hash(directory), graph=graph, error=error,
        ))
    clashes = {s for s, names in by_schema.items() if len(names) > 1}
    for schema in clashes:
        log.warning("extensions %s all map to schema %r; none of them is migrated", by_schema[schema], schema)
    return sorted((t for t in found if t.schema not in clashes), key=lambda t: t.extension)


def _find(extension: str) -> ThirdParty:
    from axiom.infra.db import normalize_extension_name

    schema = normalize_extension_name(extension)
    for tp in third_party():
        if tp.extension == extension or tp.schema == schema:
            return tp
    raise LookupError(
        f"{extension!r} is not an installed third-party extension with migrations "
        "(built-in and portfolio migrations need no trust)"
    )


# ---------------------------------------------------------------- trust (node settings)


@dataclass(frozen=True)
class TrustRecord:
    extension: str
    schema: str
    version: str
    content_hash: str
    revisions: list[str]
    trusted_by: str
    trusted_at: str


def _settings():
    from axiom.extensions.builtins.settings.store import SettingsStore

    return SettingsStore()


_FIELDS = ("extension", "version", "content_hash", "revisions", "trusted_by", "trusted_at")


def trust_record(tp: ThirdParty) -> TrustRecord | None:
    store = _settings()
    key = f"{SETTINGS_PREFIX}.{tp.schema}"
    digest = store.get(f"{key}.content_hash")
    if not digest:
        return None
    revisions = store.get(f"{key}.revisions") or []
    return TrustRecord(
        extension=str(store.get(f"{key}.extension") or tp.extension),
        schema=tp.schema,
        version=str(store.get(f"{key}.version") or ""),
        content_hash=str(digest),
        revisions=[str(r) for r in revisions] if isinstance(revisions, list) else [],
        trusted_by=str(store.get(f"{key}.trusted_by") or ""),
        trusted_at=str(store.get(f"{key}.trusted_at") or ""),
    )


def _principal() -> str:
    try:
        from axiom.infra.principal import resolve_principal

        handle = resolve_principal().handle
    except Exception:  # noqa: BLE001
        import getpass

        handle = f"@{getpass.getuser()}:local"
    return handle if _SAFE_VALUE.match(handle) else "@unknown:local"


def trust(extension: str) -> TrustRecord:
    """Trust *extension*'s migrations as they are now. Re-running re-pins to new content."""
    tp = _find(extension)
    if tp.error:
        raise ValueError(f"{tp.extension}: {tp.error}")
    for value in (tp.extension, tp.version):
        if not _SAFE_VALUE.match(value):
            raise ValueError(f"{tp.extension}: {value!r} cannot be recorded safely")
    record = TrustRecord(
        extension=tp.extension, schema=tp.schema, version=tp.version, content_hash=tp.content_hash,
        revisions=tp.revisions, trusted_by=_principal(),
        trusted_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    store = _settings()
    for name in _FIELDS:
        store.set(f"{SETTINGS_PREFIX}.{tp.schema}.{name}", getattr(record, name), scope="global")
    return record


def untrust(extension: str) -> bool:
    """Withdraw trust. What was applied stays applied; nothing more runs."""
    from axiom.infra.db import normalize_extension_name

    store = _settings()
    schema = normalize_extension_name(extension)
    removed = False
    for name in _FIELDS:
        removed = store.reset(f"{SETTINGS_PREFIX}.{schema}.{name}", scope="global") or removed
    return removed


# ---------------------------------------------------------------- reading state (read-only)


def _current_heads(schema: str) -> set[str]:
    """The revisions stamped in *schema*, read without creating anything."""
    from axiom.infra.db import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = 'alembic_version'"),
            {"s": schema},
        ).scalar()
        if not exists:
            return set()
        # nosemgrep: avoid-sqlalchemy-text -- schema is normalize_extension_name output; a schema cannot be bound
        rows = conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version')).scalars().all()
        conn.rollback()
    return {str(r) for r in rows}


def _pending(tp: ThirdParty, heads: set[str]) -> list[str]:
    done = _ancestors(tp.graph, heads)
    return _ordered(tp.graph, set(tp.graph) - done)


@dataclass
class ExtensionPlan:
    extension: str
    schema: str
    version: str
    #: untrusted | stale | trusted | up_to_date | unreadable
    state: str
    current: list[str]
    pending: list[str]
    content_hash: str
    new_since_trust: list[str] = field(default_factory=list)
    trusted_by: str = ""
    trusted_at: str = ""
    sql: str = ""
    sql_note: str = ""
    error: str = ""


def _state(tp: ThirdParty, record: TrustRecord | None, pending: list[str]) -> tuple[str, list[str]]:
    if tp.error:
        return "unreadable", []
    if not pending:
        return "up_to_date", []
    if record is None:
        return "untrusted", []
    if record.content_hash != tp.content_hash:
        return "stale", [r for r in pending if r not in record.revisions]
    return "trusted", []


def _alembic(tp: ThirdParty):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(tp.directory))
    return config, ScriptDirectory.from_config(config)


def _offline_sql(tp: ThirdParty, heads: set[str]) -> str:
    """The SQL Alembic renders for the pending revisions, without a connection."""
    from alembic.runtime.environment import EnvironmentContext

    config, script = _alembic(tp)
    buffer = io.StringIO()

    def upgrade(rev, _context):
        return script._upgrade_revs("heads", rev)

    starting = ":".join(sorted(heads)) or None
    with EnvironmentContext(config, script, fn=upgrade, as_sql=True, starting_rev=starting, destination_rev="heads") as env:
        env.configure(
            dialect_name="postgresql", output_buffer=buffer, version_table_schema=tp.schema,
            transactional_ddl=True,
        )
        env.run_migrations()
    return f'SET search_path TO "{tp.schema}";\n' + buffer.getvalue()


def plan(extensions: list[str] | None = None, *, sql: bool = True) -> list[ExtensionPlan]:
    """What ``db migrate upgrade`` would do for each third-party extension. Reads only."""
    plans: list[ExtensionPlan] = []
    for tp in third_party():
        if extensions and tp.extension not in extensions:
            continue
        record = trust_record(tp)
        try:
            heads = _current_heads(tp.schema)
        except Exception as exc:  # noqa: BLE001
            plans.append(ExtensionPlan(tp.extension, tp.schema, tp.version, "unreadable", [], [],
                                       tp.content_hash, error=f"database not readable: {exc}"))
            continue
        pending = _pending(tp, heads)
        state, new = _state(tp, record, pending)
        item = ExtensionPlan(
            extension=tp.extension, schema=tp.schema, version=tp.version, state=state,
            current=sorted(heads), pending=pending, content_hash=tp.content_hash, new_since_trust=new,
            trusted_by=record.trusted_by if record else "", trusted_at=record.trusted_at if record else "",
            error=tp.error,
        )
        if sql and pending and not tp.error:
            try:
                item.sql = _offline_sql(tp, heads)
            except Exception as exc:  # noqa: BLE001 - a revision that needs a live connection
                item.sql_note = f"offline SQL not available ({type(exc).__name__}: {exc})"
        plans.append(item)
    return plans


def builtin_plan() -> list[tuple[str, str, str, list[str]]]:
    """``(extension, current, head, pending)`` for each built-in, read without creating anything.

    signals keeps its version elsewhere and is reported by ``db migrate check``.
    """
    from alembic.script import ScriptDirectory

    from axiom.infra.db import extensions_with_migrations, normalize_extension_name

    rows: list[tuple[str, str, str, list[str]]] = []
    for name, directory in extensions_with_migrations():
        if name == "signals":
            continue
        try:
            script = ScriptDirectory(str(directory))
            heads = _current_heads(normalize_extension_name(name))
            lower = ":".join(sorted(heads)) or "base"
            pending = [r.revision for r in script.iterate_revisions("heads", lower) if r.revision not in heads]
            rows.append((name, ":".join(sorted(heads)), script.get_current_head() or "", list(reversed(pending))))
        except Exception as exc:  # noqa: BLE001
            rows.append((name, "", f"unreadable ({type(exc).__name__})", []))
    return rows


def held_back(extension: str):
    """For :func:`axiom.infra.db.provision_extension`: a third-party extension is never
    migrated there. At head it is fine; otherwise it says which command to run. ``None``
    when *extension* is not third-party."""
    from axiom.infra.db import ProvisionResult

    try:
        tp = _find(extension)
    except LookupError:
        return None
    heads = _current_heads(tp.schema)
    pending = _pending(tp, heads)
    current = ":".join(sorted(heads))
    if not pending and not tp.error:
        return ProvisionResult(extension=extension, schema=tp.schema, before=current, after=current, ok=True)
    return ProvisionResult(
        extension=extension, schema=tp.schema, before=current, after=current, ok=False,
        error=(
            f"{len(pending)} pending revision(s) from a third-party extension; they run only through "
            f"`{_brand_cli()} db migrate upgrade` once trusted (review: `{_brand_cli()} db migrate upgrade "
            f"--dry-run`, then `--trust {tp.extension}`)"
        ),
    )


# ---------------------------------------------------------------- confinement


_RELKINDS = {
    "r": "table", "p": "table", "i": "index", "I": "index", "S": "sequence", "v": "view",
    "m": "materialized view", "c": "type", "f": "foreign table",
}

# Each query yields (kind, name, signature). Rows of the extension's own schema are left out
# (:schema), as are toast and temp schemas.
_OUTSIDE = "n.nspname <> :schema AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"
_CATALOG = [
    f"""SELECT 'schema', n.nspname, concat_ws('|', n.nspowner::regrole::text, n.nspacl::text)
        FROM pg_namespace n WHERE {_OUTSIDE}""",
    f"""SELECT c.relkind::text, n.nspname || '.' || c.relname, concat_ws('|',
            c.relowner::regrole::text, c.relacl::text, array_to_string(c.reloptions, ','),
            c.relrowsecurity::text, c.relpersistence::text,
            (SELECT string_agg(concat_ws(':', a.attname, format_type(a.atttypid, a.atttypmod),
                                         a.attnotnull::text, a.attisdropped::text, a.attacl::text,
                                         pg_get_expr(d.adbin, d.adrelid)), ',' ORDER BY a.attnum)
               FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
              WHERE a.attrelid = c.oid AND a.attnum > 0),
            (SELECT string_agg(conname || '=' || pg_get_constraintdef(o.oid), ',' ORDER BY conname)
               FROM pg_constraint o WHERE o.conrelid = c.oid),
            (SELECT string_agg(tgname || '=' || pg_get_triggerdef(t.oid), ',' ORDER BY tgname)
               FROM pg_trigger t WHERE t.tgrelid = c.oid AND NOT t.tgisinternal),
            (SELECT string_agg(polname || '=' || coalesce(pg_get_expr(polqual, polrelid), ''), ',' ORDER BY polname)
               FROM pg_policy p WHERE p.polrelid = c.oid),
            (SELECT string_agg(rulename || '=' || pg_get_ruledef(r.oid), ',' ORDER BY rulename)
               FROM pg_rewrite r WHERE r.ev_class = c.oid),
            CASE WHEN c.relkind IN ('i', 'I') THEN pg_get_indexdef(c.oid) END)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_OUTSIDE}""",
    f"""SELECT 'function', n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
            concat_ws('|', md5(coalesce(p.prosrc, '')), p.prokind::text, p.proowner::regrole::text,
                      p.proacl::text, p.prosecdef::text, array_to_string(p.proconfig, ','))
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE {_OUTSIDE}""",
    f"""SELECT 'type', n.nspname || '.' || t.typname, concat_ws('|', t.typtype::text, t.typowner::regrole::text,
            (SELECT string_agg(enumlabel, ',' ORDER BY enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid),
            (SELECT string_agg(pg_get_constraintdef(o.oid), ',') FROM pg_constraint o WHERE o.contypid = t.oid))
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE {_OUTSIDE} AND t.typtype IN ('e', 'd', 'r', 'm', 'b', 'p')""",
    """SELECT 'extension', extname, concat_ws('|', extversion, extnamespace::regnamespace::text) FROM pg_extension""",
    """SELECT 'role', r.rolname, concat_ws('|', r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
            r.rolcanlogin, r.rolreplication, r.rolbypassrls,
            (SELECT string_agg(m.roleid::regrole::text, ',' ORDER BY 1) FROM pg_auth_members m WHERE m.member = r.oid))
        FROM pg_roles r""",
    """SELECT 'event trigger', evtname, concat_ws('|', evtevent, evtenabled::text, evtfoid::regproc::text)
        FROM pg_event_trigger""",
    """SELECT 'default privileges', concat_ws('.', defaclrole::regrole::text, defaclnamespace::text, defaclobjtype::text),
            defaclacl::text FROM pg_default_acl""",
    """SELECT 'database setting', concat_ws('.', setdatabase::text, setrole::text), array_to_string(setconfig, ',')
        FROM pg_db_role_setting""",
    """SELECT 'publication', pubname, concat_ws('|', puballtables::text, pubowner::regrole::text) FROM pg_publication""",
]
# A plain literal (no interpolation) so the statement is visibly constant; it repeats _OUTSIDE.
_WRITES = """SELECT n.nspname || '.' || s.relname, s.n_tup_ins + s.n_tup_upd + s.n_tup_del
    FROM pg_stat_xact_user_tables s JOIN pg_namespace n ON n.nspname = s.schemaname
    WHERE n.nspname <> :schema AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"""


def _snapshot(conn, schema: str) -> tuple[dict[tuple[str, str], str], dict[str, int]]:
    catalog: dict[tuple[str, str], str] = {}
    for query in _CATALOG:
        for kind, name, signature in conn.execute(text(query), {"schema": schema}):
            catalog[(_RELKINDS.get(kind, kind), str(name))] = str(signature or "")
    writes = {str(name): int(count or 0) for name, count in conn.execute(text(_WRITES), {"schema": schema})}
    return catalog, writes


def _outside_changes(before, after) -> list[str]:
    (cat0, w0), (cat1, w1) = before, after
    changes = [f"created {k} {n}" for (k, n) in sorted(cat1.keys() - cat0.keys())]
    changes += [f"dropped {k} {n}" for (k, n) in sorted(cat0.keys() - cat1.keys())]
    changes += [f"altered {k} {n}" for (k, n) in sorted(cat0.keys() & cat1.keys()) if cat0[(k, n)] != cat1[(k, n)]]
    changes += [f"wrote rows in {n}" for n in sorted(w1) if w1[n] > w0.get(n, 0)]
    return changes


# ---------------------------------------------------------------- apply


@dataclass
class ApplyResult:
    extension: str
    #: untrusted | stale | up_to_date | applied | rolled_back | failed | unreadable
    state: str
    pending: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    new_since_trust: list[str] = field(default_factory=list)
    before: str = ""
    after: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state not in ("rolled_back", "failed")

    @property
    def summary(self) -> str:
        cli = _brand_cli()
        if self.state == "untrusted":
            return (f"{self.extension}: pending-untrusted, applied nothing ({', '.join(self.pending)}); "
                    f"review `{cli} db migrate upgrade --dry-run`, then `--trust {self.extension}`")
        if self.state == "stale":
            return (f"{self.extension}: migrations changed since they were trusted, applied nothing; new: "
                    f"{', '.join(self.new_since_trust) or '(existing revisions edited)'}; review and re-run "
                    f"with `--trust {self.extension}`")
        if self.state == "up_to_date":
            return f"{self.extension}: up to date ({self.after or 'base'}) [third-party]"
        if self.state == "applied":
            return f"{self.extension}: {self.before or 'base'} → {self.after} [third-party, confined]"
        return f"{self.extension}: {self.state.replace('_', ' ')} — {self.error}"


class _Escaped(RuntimeError):
    pass


def _audit(tp: ThirdParty, record: TrustRecord, revision: str, previous: str) -> None:
    import os

    from axiom.infra import audit_trail
    from axiom.infra.paths import get_user_state_dir

    detail = {
        "extension": tp.extension, "version": tp.version, "content_hash": tp.content_hash,
        "revision": revision, "trusted_by": record.trusted_by, "trusted_at": record.trusted_at,
    }
    try:
        audit_trail.record_invocation(
            state_dir=get_user_state_dir(), skill=AUDIT_SKILL, params=detail, principal=_principal(),
            site=os.environ.get("AXIOM_SITE") or None, outcome="ok", errors_count=0,
            change={"kind": "migration", "subject": tp.schema, "old": previous or "base", "new": revision, **detail},
        )
    except Exception as exc:  # noqa: BLE001 - the schema change is committed; say the record is missing
        log.error("audit record for %s %s could not be written: %s", tp.extension, revision, exc)


def _apply(tp: ThirdParty, record: TrustRecord, heads: set[str]) -> ApplyResult:
    from alembic.runtime.environment import EnvironmentContext

    from axiom.infra.change_intent import record_change
    from axiom.infra.db import get_engine

    before = ":".join(sorted(heads))
    pending = _pending(tp, heads)
    config, script = _alembic(tp)
    applied: list[str] = []

    def upgrade(rev, _context):
        return script._upgrade_revs("heads", rev)

    def on_apply(*, step, **_kw):
        applied.extend(step.up_revision_ids)

    engine = get_engine()
    try:
        with record_change("migration", tp.schema, detail=f"{before or 'base'} -> {pending[-1]} (third-party)"):
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"axiom.migrate.{tp.schema}"})
                    # nosemgrep: avoid-sqlalchemy-text -- schema is normalize_extension_name output; a schema cannot be bound
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{tp.schema}"'))
                    # nosemgrep: avoid-sqlalchemy-text -- as above
                    conn.execute(text(f'SET LOCAL search_path TO "{tp.schema}"'))
                    xid = conn.execute(text("SELECT txid_current()")).scalar()
                    snap0 = _snapshot(conn, tp.schema)
                    with EnvironmentContext(config, script, fn=upgrade, destination_rev="heads") as env:
                        env.configure(
                            connection=conn, version_table_schema=tp.schema, transactional_ddl=True,
                            on_version_apply=on_apply,
                        )
                        env.run_migrations()
                    if conn.execute(text("SELECT txid_current()")).scalar() != xid:
                        raise _Escaped(
                            "a revision ended the transaction itself (COMMIT/ROLLBACK), so its changes could "
                            "not be confined; inspect the database before trusting this extension again"
                        )
                    escaped = _outside_changes(snap0, _snapshot(conn, tp.schema))
                    if escaped:
                        raise _Escaped(
                            f"its migrations changed objects outside schema {tp.schema!r}: "
                            + "; ".join(escaped) + " — rolled back, nothing applied"
                        )
                    trans.commit()
                except BaseException:
                    if trans.is_active:
                        trans.rollback()
                    raise
    except _Escaped as exc:
        return ApplyResult(tp.extension, "rolled_back", pending=pending, before=before, after=before, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(tp.extension, "failed", pending=pending, before=before, after=before,
                           error=f"{type(exc).__name__}: {exc} — rolled back, nothing applied")

    previous = before
    for revision in applied:
        _audit(tp, record, revision, previous)
        previous = revision
    after = ":".join(sorted(_current_heads(tp.schema)))
    return ApplyResult(tp.extension, "applied", pending=pending, applied=applied, before=before, after=after)


def apply_trusted() -> list[ApplyResult]:
    """Apply every trusted, unchanged third-party extension's pending revisions.

    Only ``db migrate upgrade`` calls this. Untrusted and stale extensions are reported
    with their pending revisions and get nothing, not even their schema.
    """
    results: list[ApplyResult] = []
    for tp in third_party():
        record = trust_record(tp)
        try:
            heads = _current_heads(tp.schema)
        except Exception as exc:  # noqa: BLE001
            results.append(ApplyResult(tp.extension, "failed", error=f"database not readable: {exc}"))
            continue
        pending = _pending(tp, heads)
        current = ":".join(sorted(heads))
        state, new = _state(tp, record, pending)
        if state != "trusted" or record is None:
            results.append(ApplyResult(tp.extension, state, pending=pending, new_since_trust=new,
                                       before=current, after=current, error=tp.error))
            continue
        results.append(_apply(tp, record, heads))
    return results
