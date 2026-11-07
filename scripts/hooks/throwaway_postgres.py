#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A throwaway Postgres database for the pre-push test run.

The pre-push gate runs the same suite CI runs, and CI gives that suite an EMPTY Postgres. Without one the
database tests fail (the root conftest points an unchosen database at an address that refuses connections,
so a test can never reach a developer's live data), so the hook makes its own:

    throwaway_postgres.py create     print the URL of a fresh, empty database on the reachable server
    throwaway_postgres.py drop URL   drop it (idempotent; refuses any database it did not create)

It creates a NEW database on the server (never touching any existing one), names it
``axiom_hook_<pid>_<time>_<random>``, enables pgvector when the server has it, and reaps stale ``axiom_hook_*``
databases that nothing is connected to, so a killed run does not leave orphans behind.

The server is the first of: ``AXIOM_PG_TEST_URL``, an explicit ``AXIOM_DB_URL``, the platform default
(a developer's local stack). Only its admin connection is used, to CREATE and DROP the throwaway database.

Exit codes: 0 ok; 2 no reachable server (the message says how to fix it); 3 refused.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import time

PREFIX = "axiom_hook_"
# Older names are ``<pid>_<time>``; current names add ``_<8 hex>``. Either
# shape is ours. A quote or other SQL in the third field is not.
_NAME_TAIL = r"[0-9]+_[0-9]+(?:_[0-9a-f]+)?"


def _is_hook_db_name(name: str) -> bool:
    """True for ``<PREFIX><pid>_<time>[_<hex>]``. Read PREFIX at call time so a
    run-scoped prefix (the reaper test sets one) is still ours."""
    return re.fullmatch(re.escape(PREFIX) + _NAME_TAIL, name or "") is not None


#: Stale after a day: long past any real run, short enough that orphans do not pile up.
STALE_AFTER_S = 24 * 3600
_REFUSING = "127.0.0.1:9/"  # the root conftest's "no database chosen" address


def _admin_urls() -> list[str]:
    from axiom.infra.db import DEFAULT_DB_URL

    urls = [os.environ.get("AXIOM_PG_TEST_URL", ""), os.environ.get("AXIOM_DB_URL", ""), DEFAULT_DB_URL]
    out = []
    for u in urls:
        if u.startswith("postgresql") and _REFUSING not in u and u not in out:
            out.append(u.replace("postgresql://", "postgresql+psycopg2://", 1))
    return out


def _engine(url, database):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    return create_engine(make_url(url).set(database=database), isolation_level="AUTOCOMMIT")


def checked_database_name(name: str) -> str:
    """Return ``name`` when CREATE/DROP may interpolate it.

    PostgreSQL cannot bind a database name. Only a name this script mints
    is allowed through, so a catalog row cannot break out of the quotes.
    """
    if not _is_hook_db_name(name):
        raise ValueError(f"refusing to create or drop a database named {name!r}")
    return name


def new_name() -> str:
    """``axiom_hook_<pid>_<time>_<random>``. The pid and the time alone collided:
    two creates in one second from one process, or two hosts with equal pids
    sharing a server, asked for the same name and the second CREATE failed."""
    return f"{PREFIX}{os.getpid()}_{int(time.time())}_{secrets.token_hex(4)}"


def made_at(name: str) -> int | None:
    """The creation time a name records: ``<pid>_<time>`` (older) or
    ``<pid>_<time>_<random>``. None when the name is not one of ours."""
    parts = name[len(PREFIX):].split("_") if name.startswith(PREFIX) else []
    if len(parts) not in (2, 3):
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _reap(admin) -> None:
    from sqlalchemy import text

    now = time.time()
    with admin.connect() as c:
        names = [r[0] for r in c.execute(text("select datname from pg_database where datname like :p"), {"p": PREFIX + "%"})]
        for name in names:
            made = made_at(name)
            if made is None or not _is_hook_db_name(name):
                continue
            if now - made < STALE_AFTER_S:
                continue
            busy = c.execute(text("select count(*) from pg_stat_activity where datname = :d"), {"d": name}).scalar()
            if not busy:
                checked = checked_database_name(name)
                # nosemgrep: avoid-sqlalchemy-text -- checked matches axiom_hook_<pid>_<time>[_<hex>]; a database name cannot be bound
                c.execute(text(f'DROP DATABASE IF EXISTS "{checked}"'))


def create() -> int:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    last = "no PostgreSQL server URL is configured"
    for url in _admin_urls():
        name = new_name()
        try:
            admin = _engine(url, "postgres")
            with admin.connect() as c:
                checked = checked_database_name(name)
                # nosemgrep: avoid-sqlalchemy-text -- checked matches axiom_hook_<pid>_<time>_<hex>; a database name cannot be bound
                c.execute(text(f'CREATE DATABASE "{checked}"'))
            try:
                _reap(admin)
            except Exception:  # noqa: BLE001 - reaping is housekeeping, never a reason to fail the gate
                pass
            admin.dispose()
        except Exception as exc:  # noqa: BLE001
            last = f"{make_url(url).host}:{make_url(url).port}: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            continue
        own = _engine(url, name)
        try:
            with own.connect() as c:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception:  # noqa: BLE001 - tests that need pgvector say so; the rest do not
            pass
        own.dispose()
        print(make_url(url).set(database=name).render_as_string(hide_password=False))
        return 0
    print(
        "pre-push needs a PostgreSQL server to create a throwaway test database on, and none was reachable "
        f"({last}).\n"
        "  Start your local stack, or point AXIOM_PG_TEST_URL at any server you may create databases on\n"
        "  (e.g. postgresql://user:pass@host:5432/postgres). Only a NEW axiom_hook_* database is created and\n"
        "  dropped; nothing existing is touched.",
        file=sys.stderr,
    )
    return 2


def drop(url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    name = parsed.database or ""
    try:
        name = checked_database_name(name)
    except ValueError:
        print(f"refusing to drop {name!r}: not an {PREFIX}* database this script created", file=sys.stderr)
        return 3
    admin = _engine(url, "postgres")
    try:
        with admin.connect() as c:
            # nosemgrep: avoid-sqlalchemy-text -- name matched axiom_hook_<pid>_<time>[_<hex>]; a database name cannot be bound
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    except Exception as exc:  # noqa: BLE001
        print(f"could not drop {name}: {type(exc).__name__}; the next run's reaper will", file=sys.stderr)
    finally:
        admin.dispose()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "create":
        return create()
    if len(argv) >= 3 and argv[1] == "drop":
        return drop(argv[2])
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
