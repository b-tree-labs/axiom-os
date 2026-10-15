# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The kit's local medallion: the host's Postgres, on the provider's machine.

``kit-up`` starts Postgres in Docker, applies the platform's own silver and
gold DDL, and confines a tenant role to the tenant's rows with row-level
security. ``kit-try`` then runs the provider's gold SQL **as that role**, so
"it works locally" also means "it cannot see anyone else's rows", which is the
property promotion depends on.

``AXIOM_KIT_DSN`` points the kit at a Postgres you already run instead, for a
machine without Docker. The same DDL and the same role are applied there.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from typing import Any

from .project import KitError, KitProject

IMAGE = "postgres:16"
PASSWORD = "kit-local-only"
#: A site that is never the tenant. ``kit-check`` writes one row under it, then
#: proves no tenant object can return it.
FOREIGN_SITE = "kit-isolation-probe"


def _state_file(project: KitProject):
    return project.state_dir / "medallion.json"


def _container(project: KitProject) -> str:
    """One container per kit folder: the tenant, plus a short hash of the folder.

    The tenant alone collided when two kits for one tenant ran at once (two
    checkouts, or two pytest-xdist workers), and the second `docker run` failed.
    """
    import hashlib

    folder = hashlib.sha256(str(project.root.resolve()).encode()).hexdigest()[:8]
    return f"axiom-kit-{project.role.removeprefix('kit_').replace('_', '-')}-{folder}"


def dsn(project: KitProject) -> str:
    """The DSN ``kit-up`` recorded, or ``AXIOM_KIT_DSN``. Raises when neither exists."""
    env = os.environ.get("AXIOM_KIT_DSN", "").strip()
    if env:
        return env
    path = _state_file(project)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))["dsn"]
    raise KitError("no local medallion is running. Run `kit-up` first.")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("docker"):
        raise KitError(
            "Docker is not on PATH. Install Docker Desktop, or set AXIOM_KIT_DSN to a "
            "Postgres 15+ you can create schemas and roles in."
        )
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@contextmanager
def connect(dsn_: str, *, role: str | None = None):
    import psycopg

    conn = psycopg.connect(dsn_, autocommit=True)
    try:
        if role:
            conn.execute(f'SET ROLE "{role}"')
        yield conn
    finally:
        conn.close()


def _wait(dsn_: str, timeout_s: float = 60.0) -> None:
    import psycopg

    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn_, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
                return
        except Exception as exc:  # noqa: BLE001 — retried until the deadline
            last = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            time.sleep(1)
    raise KitError(
        f"the local medallion did not accept connections within {timeout_s:.0f}s: {last}"
    )


def prepare(project: KitProject, dsn_: str) -> list[str]:
    """Apply the platform DDL, the tenant schema, the tenant role and its policy.

    Idempotent: safe on every ``kit-up`` and every ``kit-try``.
    """
    from ..conformance import GOLD_SIGNALS_DDL, SILVER_DDL, apply_conformance_ddl, catalogue

    role, schema, tenant = project.role, project.gold_schema, project.tenant
    with connect(dsn_) as conn, conn.cursor() as cur:
        # The same DDL, in the same order, the host's conform runner applies.
        apply_conformance_ddl(cur, SILVER_DDL + GOLD_SIGNALS_DDL)
        catalogue.ensure_schema(cur)
        # Every shared gold view reads silver. An ordinary view runs with its
        # owner's rights and would walk straight past the policy below, so each
        # one is made to run with the reader's. The host needs the same change
        # before a tenant role can read shared gold (the kit PRD, section 6).
        cur.execute(
            "SELECT format('%I.%I', schemaname, viewname) FROM pg_views WHERE schemaname = 'gold'"
        )
        for (view,) in cur.fetchall():
            cur.execute(f"ALTER VIEW {view} SET (security_invoker = true)")
        cur.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
            f'THEN CREATE ROLE "{role}" NOLOGIN; END IF; END $$'
        )
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(f'GRANT USAGE ON SCHEMA silver, gold, "{schema}" TO "{role}"')
        cur.execute(f'GRANT SELECT ON silver.signals TO "{role}"')
        cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA gold TO "{role}"')
        cur.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT SELECT ON TABLES TO "{role}"'
        )
        cur.execute("ALTER TABLE silver.signals ENABLE ROW LEVEL SECURITY")
        cur.execute(f'DROP POLICY IF EXISTS "{role}_rows" ON silver.signals')
        from psycopg import sql as _sql

        # DDL takes no bound parameters; the tenant is quoted as a literal by
        # the driver (and was already held to [a-z0-9-] by the project loader).
        cur.execute(
            _sql.SQL(
                "CREATE POLICY {} ON silver.signals FOR SELECT TO {} USING (site = {})"
            ).format(_sql.Identifier(f"{role}_rows"), _sql.Identifier(role), _sql.Literal(tenant))
        )
    return [f"schema {schema}", f"role {role} confined to site {tenant!r}"]


def up(project: KitProject) -> dict[str, Any]:
    """Start (or reuse) the local medallion and prepare it."""
    if os.environ.get("AXIOM_KIT_DSN", "").strip():
        d = dsn(project)
        _wait(d)
        return {"dsn": "(AXIOM_KIT_DSN)", "started": False, "prepared": prepare(project, d)}
    project.state_dir.mkdir(parents=True, exist_ok=True)
    name = _container(project)
    running = _docker("ps", "-q", "--filter", f"name=^{name}$").stdout.strip()
    if running and _state_file(project).is_file():
        d = dsn(project)
        _wait(d)
        return {"dsn": d, "started": False, "container": name, "prepared": prepare(project, d)}
    _docker("rm", "-f", name, check=False)
    port = _free_port()
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        f"POSTGRES_PASSWORD={PASSWORD}",
        "-p",
        f"127.0.0.1:{port}:5432",
        IMAGE,
    )
    d = f"postgresql://postgres:{PASSWORD}@127.0.0.1:{port}/postgres"
    _state_file(project).write_text(json.dumps({"dsn": d, "container": name}), encoding="utf-8")
    _wait(d)
    return {"dsn": d, "started": True, "container": name, "prepared": prepare(project, d)}


def down(project: KitProject) -> dict[str, Any]:
    """Stop and remove the local medallion. Its data is disposable by design."""
    name = _container(project)
    _docker("rm", "-f", name, check=False)
    path = _state_file(project)
    if path.is_file():
        path.unlink()
    return {"removed": name}


__all__ = ["FOREIGN_SITE", "connect", "down", "dsn", "prepare", "up"]
