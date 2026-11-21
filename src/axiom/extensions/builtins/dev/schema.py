# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A lane's own database, migrated for THIS checkout.

The migration runner is the platform's (``axiom.infra.db.provision_extension``
over ``extensions_with_migrations``); this module only aims it. It runs in a
child process with the checkout's source first on ``PYTHONPATH`` and the lane's
DSN in the environment, so "the migrations this checkout ships" is literal and
a database is never migrated by some other checkout's code.

Drift is classified, never repaired: a database *ahead* of (at a revision this
checkout does not have) or *diverged* from (a branched revision graph) the
checkout stops the run. Nothing is dropped except by ``reset``, which only ever
touches the lane's own database name.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARK = "@@AXI-SCHEMA@@"

_HEADS = """
import json
from axiom.infra import db
from alembic.script import ScriptDirectory
out = {}
for name, d in db.extensions_with_migrations():
    sd = ScriptDirectory(str(d))
    out[name] = {"heads": list(sd.get_heads()),
                 "revisions": [r.revision for r in sd.walk_revisions()],
                 "current": db.current_revision(name)}
print("@@AXI-SCHEMA@@" + json.dumps(out))
"""

_MIGRATE = """
import json
from axiom.infra import db
out = {}
for name, d in db.extensions_with_migrations():
    r = db.provision_extension(name, d)
    out[name] = {"ok": r.ok, "after": r.after, "before": r.before, "skipped": r.skipped,
                 "error": r.error}
print("@@AXI-SCHEMA@@" + json.dumps(out))
"""


class SchemaError(RuntimeError):
    """Said to the person as is: what is wrong and the next command."""


def lane_dsn(template: str, database: str) -> str:
    """``template``'s server and credentials with the lane's database name."""
    from sqlalchemy.engine import make_url

    from axiom.infra.db import require_named_driver

    url = make_url(require_named_driver(template))
    if not url.drivername.startswith("postgresql"):
        raise SchemaError("a lane database is Postgres (ADR-174); the database URL is not")
    return url.set(database=database).render_as_string(hide_password=False)


def _admin(dsn: str):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    admin = make_url(dsn).set(database="postgres")
    return create_engine(admin, isolation_level="AUTOCOMMIT")


def database_exists(dsn: str) -> bool:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    name = make_url(dsn).database
    eng = _admin(dsn)
    try:
        with eng.connect() as c:
            return bool(
                c.execute(text("SELECT 1 FROM pg_database WHERE datname=:n"), {"n": name}).scalar()
            )
    finally:
        eng.dispose()


def ensure_database(dsn: str) -> bool:
    """Create the lane's database if absent. True when it was created."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    if database_exists(dsn):
        return False
    name = make_url(dsn).database
    eng = _admin(dsn)
    try:
        with eng.connect() as c:
            # nosemgrep: avoid-sqlalchemy-text -- name passed _ident ([a-z][a-z0-9_], <=63); a database name cannot be bound
            c.execute(text(f'CREATE DATABASE "{_ident(name)}"'))
    finally:
        eng.dispose()
    return True


def _ident(name: str) -> str:
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name or ""):
        raise SchemaError(f"{name!r} is not a database name a lane can own")
    return name


def reset_database(dsn: str, lane_database: str) -> None:
    """Drop and recreate the lane's own database; refuse any other name."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    name = make_url(dsn).database or ""
    if not lane_database or name != lane_database:
        raise SchemaError(
            f"refusing to reset {name!r}: only this lane's own database "
            f"({lane_database or 'none claimed'}) can be reset"
        )
    _ident(name)
    eng = _admin(dsn)
    try:
        with eng.connect() as c:
            c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()"
                ),
                {"n": name},
            )
            # nosemgrep: avoid-sqlalchemy-text -- name passed _ident ([a-z][a-z0-9_], <=63); a database name cannot be bound
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            # nosemgrep: avoid-sqlalchemy-text -- name passed _ident ([a-z][a-z0-9_], <=63); a database name cannot be bound
            c.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        eng.dispose()


def _child(code: str, root: Path, roots: list[Path], dsn: str, dsn_var: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [*(str(p) for p in roots), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env["AXIOM_DB_URL"] = dsn
    env[dsn_var] = dsn
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=300
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(MARK):
            return json.loads(line[len(MARK) :])
    tail = "\n".join((proc.stderr or proc.stdout).splitlines()[-8:])
    raise SchemaError(f"could not read this checkout's migrations: {tail}")


def probe(root: Path, roots: list[Path], dsn: str, dsn_var: str = "AXIOM_DB_URL") -> dict[str, Any]:
    return _child(_HEADS, root, roots, dsn, dsn_var)


def migrate(
    root: Path, roots: list[Path], dsn: str, dsn_var: str = "AXIOM_DB_URL"
) -> dict[str, Any]:
    return _child(_MIGRATE, root, roots, dsn, dsn_var)


@dataclass(frozen=True)
class Drift:
    extension: str
    kind: str  # behind | ahead | diverged
    database: str
    checkout: str
    cause: str


def classify(report: dict[str, Any]) -> list[Drift]:
    out: list[Drift] = []
    for name, r in sorted(report.items()):
        heads, revs, cur = r["heads"], set(r["revisions"]), r["current"]
        if len(heads) > 1:
            out.append(
                Drift(
                    name,
                    "diverged",
                    cur or "base",
                    ",".join(heads),
                    "this checkout's own revision graph has several heads",
                )
            )
        elif cur and cur not in revs:
            out.append(
                Drift(
                    name,
                    "ahead",
                    cur,
                    heads[0] if heads else "base",
                    f"the database is at {cur}, a revision this checkout does not have "
                    "(a branch with a newer migration ran here)",
                )
            )
        elif cur != (heads[0] if heads else ""):
            out.append(
                Drift(
                    name,
                    "behind",
                    cur or "base",
                    heads[0] if heads else "base",
                    "the checkout has newer migrations",
                )
            )
    return out


def blocking(drifts: list[Drift]) -> list[Drift]:
    return [d for d in drifts if d.kind in ("ahead", "diverged")]


def describe(d: Drift) -> str:
    return f"{d.extension}: {d.kind}: database {d.database}, checkout {d.checkout}: {d.cause}"


def commit_of(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def record(home: Path, root: Path, after: dict[str, Any]) -> dict[str, Any]:
    data = {
        "commit": commit_of(root),
        "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "extensions": {
            n: v.get("after") or "" for n, v in sorted(after.items()) if not v.get("skipped")
        },
    }
    Path(home).mkdir(parents=True, exist_ok=True)
    (Path(home) / "schema.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def read_record(home: Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(home) / "schema.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sync(
    root: Path, roots: list[Path], dsn: str, home: Path, dsn_var: str = "AXIOM_DB_URL"
) -> dict[str, Any]:
    """Make sure the database exists and is at this checkout's heads.

    Raises SchemaError on ahead/diverged drift or a failed migration.
    """
    created = ensure_database(dsn)
    drifts = classify(probe(root, roots, dsn, dsn_var))
    if stop := blocking(drifts):
        raise SchemaError(
            "\n".join(
                [
                    *(describe(d) for d in stop),
                    "run `axi dev db reset --yes` to recreate this lane's database from this checkout (nothing is dropped automatically)",
                ]
            )
        )
    after = migrate(root, roots, dsn, dsn_var)
    failed = [f"{n}: {v['error']}" for n, v in after.items() if not v["ok"]]
    # One extension's broken migration must not stop the rest (the platform's own
    # rule in provision_extension), but it is never silent: it is returned, printed
    # by `dev up`, and left out of the recorded heads.
    rec = record(home, root, {n: v for n, v in after.items() if v["ok"]})
    return {
        "created": created,
        "migrated": [d.extension for d in drifts],
        "problems": failed,
        **rec,
    }
