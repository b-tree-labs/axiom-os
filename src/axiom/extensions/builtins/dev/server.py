# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ONE local Postgres (pgvector) for every lane on this machine.

A clean machine has no database server, and `dev up` rightly refuses to guess
one. This starts a single container under a stable name, with a named volume
and a host port reserved through the lane registry, and keeps its password in
the vault (never printed, never on a command line). Each lane's own database
is then created on it by the schema step. `dev down` leaves it running (lanes
share it); `dev db server down` stops it. The data volume is kept.

Discovery order for `dev up` when no database server was named: an explicit
``AXIOM_PG_TEST_URL`` that answers, then this managed server, then (Docker
present) start it, else one-line fixes. The implicit ``localhost:5432`` is not
a candidate: on a machine running another stack it is somebody else's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

NAME = "axiom-dev-postgres"
VOLUME = "axiom-dev-postgres-data"
IMAGE = "pgvector/pgvector:pg16"
SECRET = "dev-postgres"
LANE = "dev-postgres"
USER = "postgres"
TEST_URL_ENV = "AXIOM_PG_TEST_URL"


class ServerError(RuntimeError):
    """Said as is: what is wrong and the one-line fix."""


def docker_path() -> str | None:
    return shutil.which("docker")


def docker(
    args: list[str], *, env: dict[str, str] | None = None, timeout: float = 120
) -> tuple[int, str]:
    """Run docker; (returncode, combined output). A seam so tests need no daemon."""
    try:
        p = subprocess.run(
            [docker_path() or "docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def state() -> str:
    """running | stopped | absent | unavailable (no docker, or its daemon is down)."""
    if docker_path() is None:
        return "unavailable"
    rc, out = docker(["inspect", "-f", "{{.State.Running}}", NAME], timeout=20)
    if rc == 0:
        return "running" if out.strip() == "true" else "stopped"
    if "No such" in out or "no such" in out:
        return "absent"
    return "unavailable"


def reserved_port(registry) -> int:
    """The host port, reserved through the lane registry (one record, reused)."""
    from axiom.extensions.builtins.lane import ports
    from axiom.extensions.builtins.lane.registry import UNMANAGED, Lane

    held = registry.get(LANE)
    if held is not None:
        return held.front
    front, api = ports.resolve(LANE, taken=registry.taken_ports())
    registry.claim(
        Lane(
            name=LANE,
            front=front,
            api=api,
            dsn_var=UNMANAGED,
            note="the shared dev Postgres server (axi dev db server)",
        )
    )
    return front


def url(vault, port: int) -> str:
    """The server's admin URL. Holds the password: never print it."""
    with vault.get(SECRET) as s:
        pw = s.as_str()
    return f"postgresql://{USER}:{pw}@127.0.0.1:{port}/{'postgres'}"


def reachable(server_url: str, timeout: float = 3.0) -> bool:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from axiom.infra.db import require_named_driver

    try:
        eng = create_engine(
            make_url(require_named_driver(server_url)).set(database="postgres"),
            connect_args={"connect_timeout": int(timeout)},
        )
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:  # noqa: BLE001 - any failure is "not reachable"
        return False


def _wait(vault, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reachable(url(vault, port), timeout=2):
            return
        time.sleep(1)
    raise ServerError(
        f"the dev Postgres did not accept connections within {int(timeout)}s: docker logs {NAME}"
    )


def up(
    vault, registry, *, say: Callable[[str], None] = lambda s: None, timeout: float = 90
) -> dict[str, Any]:
    st = state()
    if st == "unavailable":
        raise ServerError(
            "Docker is not available (not installed, or its daemon is not running): install or start "
            "Docker Desktop, or name a server with --db-url postgresql://USER@HOST:PORT/postgres"
        )
    port = reserved_port(registry)
    if st == "running":
        if not vault.exists(SECRET):
            raise ServerError(
                f"{NAME} is running but its password is not in the vault: docker rm -f -v {NAME} "
                f"&& docker volume rm {VOLUME} (this drops the databases on it), then run again"
            )
        return {"state": "running", "port": port, "started": False, "created": False}
    if st == "stopped":
        say(f"starting the dev database server ({NAME}, port {port})")
        rc, out = docker(["start", NAME])
        if rc != 0:
            raise ServerError(f"docker start {NAME} failed: {out.splitlines()[-1] if out else rc}")
        _wait(vault, port, timeout)
        return {"state": "running", "port": port, "started": True, "created": False}
    rc, _ = docker(["volume", "inspect", VOLUME], timeout=20)
    if rc == 0 and not vault.exists(SECRET):
        raise ServerError(
            f"the data volume {VOLUME} exists but its password is not in the vault: "
            f"docker volume rm {VOLUME} (this drops the databases on it), then run again"
        )
    if not vault.exists(SECRET):
        from axiom.extensions.builtins.webgate.skills._accounts import generate_password

        # The vault first: no container exists whose password nobody holds.
        vault.set(
            SECRET,
            generate_password().encode("utf-8"),
            notes="password of the shared dev Postgres server",
        )
    with vault.get(SECRET) as s:
        pw = s.as_str()
    say(
        f"starting the dev database server ({NAME}, {IMAGE}, port {port}); the first start pulls the image"
    )
    rc, out = docker(
        [
            "run",
            "-d",
            "--name",
            NAME,
            "-v",
            f"{VOLUME}:/var/lib/postgresql/data",
            "-e",
            "POSTGRES_PASSWORD",
            "-p",
            f"127.0.0.1:{port}:5432",
            "--restart",
            "unless-stopped",
            IMAGE,
        ],
        env={"POSTGRES_PASSWORD": pw},
        timeout=600,
    )
    if rc != 0:
        raise ServerError(f"docker run failed: {out.splitlines()[-1] if out else rc}")
    _wait(vault, port, timeout)
    return {"state": "running", "port": port, "started": True, "created": True}


def down() -> dict[str, Any]:
    st = state()
    if st != "running":
        return {"state": st, "stopped": False}
    rc, out = docker(["stop", NAME])
    if rc != 0:
        raise ServerError(f"docker stop {NAME} failed: {out}")
    return {"state": "stopped", "stopped": True}


def status(vault, registry) -> dict[str, Any]:
    held = registry.get(LANE)
    return {
        "state": state(),
        "port": held.front if held else None,
        "container": NAME,
        "volume": VOLUME,
        "password_in_vault": bool(vault.exists(SECRET)),
    }


def acquire(vault, registry, *, say: Callable[[str], None] | None = None) -> tuple[str, list[str]]:
    """A server URL for `dev up` when none was named, or the one-line fixes."""
    say = say or (lambda s: print(s, file=sys.stderr))
    explicit = os.environ.get(TEST_URL_ENV, "").strip()
    if explicit.startswith("postgresql") and reachable(explicit):
        return explicit, []
    st = state()
    if st == "running" and vault.exists(SECRET):
        return url(vault, reserved_port(registry)), []
    if st != "unavailable":
        try:
            info = up(vault, registry, say=say)
        except ServerError as exc:
            return "", [str(exc)]
        return url(vault, info["port"]), []
    return "", [
        "no database server was named and none is running. One of: install Docker Desktop "
        "(then `dev up` starts one for you), or name a server: dev up --db-url "
        "postgresql://USER@HOST:PORT/postgres, or set AXIOM_DB_URL",
    ]
