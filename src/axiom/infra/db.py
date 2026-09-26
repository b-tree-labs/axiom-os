# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Schema-per-extension database primitive — ADR-052.

Extensions never see a DSN, the connection pool, or schema-creation DDL.
They consume a single scoped session:

    from axiom.infra.db import session_for

    with session_for("expman") as s:
        s.add(sample)
        s.commit()

The provider owns the shared Engine/pool, computes a safe Postgres schema
name from the extension name, ensures the schema exists (idempotent), and
sets ``search_path`` so unqualified table names resolve to the extension's
own schema.

Per ADR-050 the platform vocabulary is ``tenant`` (data-owner / partition
within an extension's data) and ``site`` (physical install). This module
delivers extension-level isolation; the within-extension tenancy menu
(single / row-level ``tenant_id`` / schema-per-tenant — ADR-052 §D4)
sits on top.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

#: The driver is named EXPLICITLY. A bare ``postgresql://`` asks
#: SQLAlchemy 2 for the psycopg (v3) dialect, which this project does not
#: depend on — pyproject declares ``psycopg2-binary`` and nothing else.
#: It worked on machines where psycopg3 happened to arrive as somebody
#: else's transitive dependency, and raised ``ModuleNotFoundError: No
#: module named 'psycopg'`` in CI, where it did not. A default that works
#: by luck is not a default.
DEFAULT_DB_URL = "postgresql+psycopg2://axiom:axiom@localhost:5432/axiom_db"

#: The scheme a driver-less Postgres URL is rewritten to.
_NAMED_PG_SCHEME = "postgresql+psycopg2://"


def require_named_driver(url: str) -> str:
    """Return ``url`` with its Postgres driver named explicitly.

    Naming :data:`DEFAULT_DB_URL` fixed the *default* and only the default.
    A URL that arrives from the **environment** never passes through that
    constant, and CI supplies exactly that — the workflow sets
    ``AXIOM_DB_URL`` to a bare ``postgresql://``. So ``main`` went on
    failing with ``ModuleNotFoundError: No module named 'psycopg'`` after
    the default was fixed, in the one job that installs no incidental
    psycopg3: ``ensure_pgvector_extension`` died, ``CREATE EXTENSION
    vector`` never ran, and every vector column in the integration suite
    then failed with ``type "vector" does not exist``. Two red jobs, six
    consecutive red mains, one cause.

    A URL that already names a driver is returned untouched, including a
    deliberate ``postgresql+psycopg://`` from somebody who wants v3.
    Non-Postgres schemes are not our business. ``postgres://`` is rewritten
    too, because SQLAlchemy has rejected that spelling outright since 1.4.
    """
    if not url:
        return url
    for bare in ("postgresql://", "postgres://"):
        if url.startswith(bare):
            return _NAMED_PG_SCHEME + url[len(bare) :]
    return url


_engine: Engine | None = None
_session_factory: sessionmaker | None = None
# The URL the cached Engine was built from. The cache is keyed on this, not on
# `_engine is not None`: a bare None-check means the first caller in a process
# fixes the database for every caller after it, and anything that repoints
# AXIOM_DB_URL leaves a stale Engine behind that still looks healthy.
_engine_url: str | None = None
_engine_lock = threading.Lock()

# Postgres unquoted identifiers must match [a-z_][a-z0-9_]*. We normalize
# anything outside that set to underscore so we never need to quote at
# call sites; see normalize_extension_name.
_UNSAFE = re.compile(r"[^a-z0-9_]")
# NAMEDATALEN - 1; Postgres truncates anything longer.
_MAX_IDENT = 63


log = logging.getLogger(__name__)


def normalize_extension_name(name: str) -> str:
    """Coerce an extension name into a safe Postgres schema identifier.

    Lowercase, hyphens → underscores, anything else → underscores, leading
    underscores stripped, length-capped at 63 chars. All-unsafe input falls
    back to ``"ext"`` so the result is always a valid identifier; empty or
    whitespace-only input raises.

    Test isolation hook: when ``AXIOM_TEST_SCHEMA_SUFFIX`` is set in env,
    its value is appended to the normalized name (and re-normalized so a
    malformed worker id stays a valid identifier). The conftest sets this
    per pytest-xdist worker so persisted-state tests get worker-scoped
    schemas (``vault_gw0``, ``vault_gw1``, …) and parallel teardowns
    can't TRUNCATE each other's rows. The var is unset in production —
    behavior is unchanged when absent.
    """
    if not name or not name.strip():
        raise ValueError("extension name cannot be empty")
    s = name.strip().lower().replace("-", "_")
    s = _UNSAFE.sub("_", s)
    s = s.lstrip("_") or "ext"

    suffix = os.environ.get("AXIOM_TEST_SCHEMA_SUFFIX", "")
    if suffix:
        # Run the suffix through the same coercion so a worker id like
        # "gw-3" becomes "gw_3" rather than producing an unsafe ident.
        suffix_norm = _UNSAFE.sub("_", suffix.lower().replace("-", "_"))
        s = s + suffix_norm

    return s[:_MAX_IDENT]


def _positive_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unusable.

    A malformed pool setting must not take the database down — a node that
    refuses to start because someone typo'd a number is worse than a node
    running on the documented default.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %d", name, raw, default)
        return default
    if value < 0:
        log.warning("%s=%d is negative; using %d", name, value, default)
        return default
    return value


def pool_settings(url: str = "") -> dict:
    """Connection-pool configuration, measured rather than guessed.

    Returns ``{}`` for SQLite. SQLite uses ``SingletonThreadPool``, which
    accepts neither ``max_overflow`` nor ``pool_timeout`` — passing them
    raises at engine construction, which would break every local-testing path.
    Pool sizing is a server-database concern.

    Defaults come from a scale probe against a local PostgreSQL:

    - SQLAlchemy's stock pool is size 5 with overflow 10, so a process can hold
      15 connections. Past that, callers queue: at concurrency 32 the median
      wait doubled (64 ms → 111 ms) with no errors, which is the pool
      queueing, not the database struggling.
    - PostgreSQL's own `max_connections` was 100. Six such processes
      (6 x 15 = 90) exhaust it, so pool size is what decides how many workers
      a node can run — it is a deployment parameter, not an implementation
      detail. Hence the environment variables.
    - `pool_recycle` defaulted to -1: connections were NEVER recycled. A
      long-lived worker behind a proxy or firewall that drops idle connections
      would keep handing out dead ones. `pool_pre_ping` catches that on
      checkout, but recycling avoids the round trip and the proxy's own
      timeout is usually the shorter deadline. 30 minutes sits under the
      common defaults.
    """
    if url.startswith("sqlite"):
        return {}
    return {
        "pool_size": _positive_int("AXIOM_DB_POOL_SIZE", 5),
        "max_overflow": _positive_int("AXIOM_DB_MAX_OVERFLOW", 10),
        "pool_timeout": _positive_int("AXIOM_DB_POOL_TIMEOUT", 30),
        "pool_recycle": _positive_int("AXIOM_DB_POOL_RECYCLE", 1800),
    }


def platform_db_url() -> str:
    """The database URL the platform itself uses.

    One definition, because there were two. ``get_engine`` (and therefore every
    ``session_for`` / ``engine_for`` / Alembic ``env.py``) reads ``AXIOM_DB_URL``
    and falls back to :data:`DEFAULT_DB_URL`. ``data.ensure_schema`` read three
    env names and *failed* when none was set — so on a node where the env is
    empty, ``axi db migrate upgrade head`` reached the database and
    ``axi data ensure-schema`` reported "no DSN", in the same deploy step, two
    lines apart.

    A skill that cannot find the database the rest of the process is already
    talking to is not a configuration problem; it is two answers to one
    question. Callers that need a raw DSN (DDL over psycopg2, say) resolve it
    here rather than inventing their own precedence.

    Returned **as configured**, deliberately not driver-qualified. This string
    is consumed both by SQLAlchemy and by raw psycopg2, and the two want
    different spellings: ``psycopg2.connect`` rejects
    ``postgresql+psycopg2://`` outright with ``invalid dsn: missing "="``.
    Naming the driver is therefore the job of whoever builds an Engine — see
    :func:`require_named_driver`, applied in :func:`get_engine`.
    """
    return os.environ.get("AXIOM_DB_URL", DEFAULT_DB_URL)


def get_engine() -> Engine:
    """Return the process-wide shared SQLAlchemy Engine.

    Lazy-built from ``AXIOM_DB_URL``; every extension's ``session_for(...)``
    rides the same Engine + pool for as long as the URL is unchanged.

    The cache is **keyed on the URL**. A changed ``AXIOM_DB_URL`` rebuilds
    rather than returning the Engine some earlier caller happened to build,
    which is what kept a test that repointed the URL poisoning every later
    consumer in the same process.
    """
    global _engine, _session_factory, _engine_url
    url = platform_db_url()
    if _engine is None or _engine_url != url:
        with _engine_lock:
            if _engine is None or _engine_url != url:
                stale = _engine
                # pool_pre_ping: a lightweight liveness check on checkout so a
                # long-running service (a serve app, an always-on agent) that
                # holds pooled connections doesn't 500 on the first request
                # after Postgres has dropped an idle connection — the dead
                # connection is transparently discarded and replaced instead.
                # require_named_driver, not `url`: the cache key stays the URL
                # as configured, while the Engine is built from a spelling that
                # names its driver. Letting SQLAlchemy pick is what broke CI.
                _engine = create_engine(
                    require_named_driver(url), future=True, pool_pre_ping=True, **pool_settings(url)
                )
                _session_factory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
                _engine_url = url
                if stale is not None:
                    # Return the old pool's connections rather than leaking them
                    # for the life of the process.
                    try:
                        stale.dispose()
                    except Exception:  # pragma: no cover - best effort
                        pass
    return _engine


def ensure_schema(engine: Engine, extension_name: str) -> str:
    """Idempotently ``CREATE SCHEMA IF NOT EXISTS`` for the extension.

    Returns the normalized schema name. Safe to call on every session —
    the second call is a no-op at the DB layer.
    """
    schema = normalize_extension_name(extension_name)
    # normalize_extension_name guarantees [a-z0-9_], so direct
    # interpolation is safe; param binding doesn't work for identifiers.
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    return schema


@dataclass(frozen=True)
class ProvisionResult:
    """What a provisioning pass did to one extension's storage."""

    extension: str
    schema: str
    before: str
    after: str
    ok: bool
    error: str = ""
    #: Why this extension was not provisioned at all. Empty means it was.
    #:
    #: A third outcome, distinct from both success and failure: the extension's
    #: code is not installed here. An install that omits an optional extra has
    #: nothing to migrate, and calling that a failure means a deploy can never
    #: complete on a node that deliberately does not ship every extension.
    #:
    #: Kept separate from ``error`` on purpose. "Not installed" and "broken"
    #: want different reactions, and one field for both is how an operator
    #: learns to ignore the field.
    skipped: str = ""

    @property
    def migrated(self) -> bool:
        """Whether this pass actually moved the schema."""
        return self.ok and not self.skipped and self.before != self.after

    @property
    def summary(self) -> str:
        if self.skipped:
            return f"{self.extension}: skipped — {self.skipped}"
        if not self.ok:
            return f"{self.extension}: failed — {self.error}"
        if self.migrated:
            start = self.before or "base"
            return f"{self.extension}: {start} → {self.after}"
        return f"{self.extension}: up to date ({self.after or 'base'})"


def provision_extension(extension_name: str, migrations_dir: Path | None = None) -> ProvisionResult:
    """Bring an extension's schema up to head. Idempotent.

    ADR-052 gives every extension a schema and ``session_for`` creates the
    SCHEMA on first use — but nothing created the TABLES. Each extension
    solved that itself: `axi db migrate` and `axi db bootstrap` both import
    from one specific extension, four more ship migrations that nothing runs,
    and some code calls `create_all` inline. A fresh node had no single path
    that stood its storage up.

    This runs ALEMBIC, not ``create_all``. `create_all` makes tables and
    stamps no version, so the first column change afterwards has no upgrade
    path and `db migrate` reports "up to date" over tables Alembic never made
    — the same false green as issue #826. Every extension here already ships
    a revision history; this drives it.

    Reports rather than raises. One extension's broken migration must not stop
    a node provisioning the rest, and a silent provisioning step is how the
    whole thing becomes a false green in the first place.
    """
    schema = normalize_extension_name(extension_name)
    try:
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        directory = migrations_dir or _migrations_dir_for(extension_name)
        if directory is None:
            return ProvisionResult(
                extension=extension_name,
                schema=schema,
                before="",
                after="",
                ok=False,
                error="ships no migrations",
            )

        engine, _ = engine_for(extension_name)
        config = Config()
        config.set_main_option("script_location", str(directory))

        before = current_revision(extension_name)
        command.upgrade(config, "head")
        after = current_revision(extension_name)
        head = ScriptDirectory(str(directory)).get_current_head() or ""
        if after != head:
            # `command.upgrade` can return without error AND without moving
            # the schema — the exact shape of the false green in #826. Not
            # reaching head is a failure even though nothing raised.
            #
            # The usual cause is an env.py that stamps outside the extension's
            # own schema, so name it: "stopped at base" sends someone hunting
            # a migration bug when the migration ran fine and the version
            # landed in the wrong place.
            detail = f"stopped at {after or 'base'!r}, head is {head!r}"
            stray = _stray_version_schema(engine, head)
            if stray:
                detail += (
                    f" — its version is stamped in {stray!r}, not {schema!r};"
                    " env.py must pass version_table_schema (ADR-052)"
                )
            return ProvisionResult(
                extension=extension_name,
                schema=schema,
                before=before,
                after=after,
                ok=False,
                error=detail,
            )
        return ProvisionResult(
            extension=extension_name,
            schema=schema,
            before=before,
            after=after,
            ok=True,
        )
    except ModuleNotFoundError as exc:
        # The extension's own dependencies are absent, so there is no code here
        # to migrate. `signals` needs pgvector, which lives in the `signal`
        # extra; a node installing axiom-os-lm without it has nothing to build
        # and nothing is wrong. Reported as a skip so a deploy is not blocked
        # by an extension the install deliberately does not carry.
        missing = getattr(exc, "name", None) or str(exc)
        log.info("provision_extension(%s) skipped: %r is not installed", extension_name, missing)
        return ProvisionResult(
            extension=extension_name,
            schema=schema,
            before="",
            after="",
            ok=True,
            skipped=(
                f"{missing!r} is not installed — this extension's dependencies "
                "are not part of this install (check its optional extra)"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("provision_extension(%s) failed: %s", extension_name, exc)
        return ProvisionResult(
            extension=extension_name,
            schema=schema,
            before="",
            after="",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def current_revision(extension_name: str) -> str:
    """The revision stamped in *extension_name*'s own schema, or ``""``.

    Read from the schema the extension owns, so an extension stamping
    elsewhere reads as unstamped here rather than borrowing another's version.
    """
    from alembic.runtime.migration import MigrationContext

    schema = normalize_extension_name(extension_name)
    try:
        engine, _ = engine_for(extension_name)
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"version_table_schema": schema})
            return context.get_current_revision() or ""
    except Exception:  # noqa: BLE001
        return ""


def _stray_version_schema(engine: Engine, head: str) -> str:
    """Schema holding *head* in an ``alembic_version`` that is not the
    extension's own — usually ``public``, which ADR-052 forbids writing to."""
    query = text(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = 'alembic_version'"
    )
    try:
        with engine.connect() as connection:
            for (candidate,) in connection.execute(query):
                stamped = connection.execute(
                    text(f'SELECT version_num FROM "{candidate}".alembic_version')
                ).scalar()
                if stamped == head:
                    return str(candidate)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _migrations_dir_for(extension_name: str) -> Path | None:
    for name, directory in extensions_with_migrations():
        if name == extension_name:
            return directory
    return None


def extensions_with_migrations() -> list[tuple[str, Path]]:
    """``(extension, migrations directory)`` for everything shipping revisions.

    Discovered from the tree, because a registry listing them is a second
    place to update and the first thing to drift.

    The marker is ``migrations/env.py``. An earlier version of this discovery
    globbed ``extensions/builtins/*/alembic.ini`` — one directory short of
    where that file lives, and keyed on a file only ONE extension ships. It
    returned an empty list under every condition while four extensions shipped
    migrations nothing ran: a check incapable of failing.
    """
    import importlib.util

    from axiom.infra.branding import discover_portfolio_members

    packages = ["axiom"]
    for member in discover_portfolio_members():
        packages.append(member.package_name.replace("-", "_"))

    roots: list[Path] = []
    for package in dict.fromkeys(packages):
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            continue
        roots.extend(Path(root) for root in spec.submodule_search_locations)
    return scan_migration_roots(roots)


def scan_migration_roots(roots) -> list[tuple[str, Path]]:
    """The scan itself, over explicit roots.

    Separated so it can be tested against a synthetic tree. The version of
    this test that named a real consumer extension passed locally, where that
    package happens to be installed, and failed in CI, where it is not — a
    platform test must not depend on a consumer being present, and Axiom's
    tests must not name domain consumers at all.
    """
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for root in roots:
        for env in Path(root).glob("extensions/builtins/*/migrations/env.py"):
            name = env.parent.parent.name
            if name in seen:
                continue
            seen.add(name)
            found.append((name, env.parent))
    return sorted(found)


@contextmanager
def session_for(extension_name: str) -> Iterator[Session]:
    """Yield a Session scoped to *extension_name*'s schema.

    On enter: ensures the schema exists and sets ``search_path`` to
    ``"<schema>, public"`` on this session's connection so unqualified
    table names resolve to the extension's own schema.

    Commit/rollback is the caller's call. We don't auto-commit on exit —
    that would mask transactional intent. Typical use::

        with session_for("expman") as s:
            do_work(s)
            s.commit()
    """
    get_engine()  # ensures _session_factory is built
    schema = ensure_schema(_engine, extension_name)  # type: ignore[arg-type]
    assert _session_factory is not None  # narrowed by get_engine()
    session = _session_factory()
    try:
        session.execute(text(f'SET search_path TO "{schema}", public'))
        yield session
    finally:
        session.close()


def engine_for(extension_name: str) -> tuple[Engine, str]:
    """Return ``(shared engine, schema)`` for an extension.

    Intended for an extension's Alembic ``env.py``::

        from axiom.infra.db import engine_for

        connectable, schema = engine_for("expman")
        with connectable.connect() as conn:
            context.configure(
                connection=conn,
                target_metadata=target_metadata,
                version_table_schema=schema,
                include_schemas=True,
            )
            with context.begin_transaction():
                context.run_migrations()

    Ensures the schema exists before returning so the migration env has a
    home to write its ``alembic_version`` table into.
    """
    engine = get_engine()
    schema = ensure_schema(engine, extension_name)
    return engine, schema


__all__ = [
    "DEFAULT_DB_URL",
    "ensure_schema",
    "engine_for",
    "get_engine",
    "platform_db_url",
    "normalize_extension_name",
    "require_named_driver",
    "session_for",
]
