# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A throwaway TimescaleDB in a container, for the tests that need a real one.

Never the developer's database: every test that uses this names the DSN it
was handed. The container is called ``tsdb-<hex>`` so a shared machine can
list and remove its own (``docker ps --filter name=tsdb-``).

Without docker the tests skip locally and fail on CI, where a skipped test
would hide a broken guarantee.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Sequence

import pytest

#: The image CI pulls. ``AXIOM_TSDB_TEST_IMAGE`` overrides it, so the suite can
#: be run against the version a node actually carries (2.30.2 at the time of
#: writing; both behave identically on every call the platform makes).
DEFAULT_IMAGE = "timescale/timescaledb:2.17.2-pg16"
IMAGE_ENV = "AXIOM_TSDB_TEST_IMAGE"
PREFIX_ENV = "AXIOM_TEST_CONTAINER_PREFIX"
DEFAULT_PREFIX = "tsdb"
PASSWORD = "ts-test-only"


def image() -> str:
    return os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker_available() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode == 0


def start(extra: Sequence[str] = (), postgres_args: Sequence[str] = ()) -> tuple[str, str]:
    """Start a container and wait until it answers. Returns ``(name, dsn)``."""
    if not docker_available():
        if os.environ.get("CI"):
            pytest.fail("docker is required on CI for the TimescaleDB tests")
        pytest.skip("docker is not available")
    prefix = os.environ.get(PREFIX_ENV) or DEFAULT_PREFIX
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    # No background jobs, ever. A policy a test installs (compression,
    # retention) is otherwise run by a TimescaleDB background worker at a time
    # of its own choosing, and the next test's teardown DDL (DROP SCHEMA ...
    # CASCADE) then deadlocks with it: the DROP holds the hypertable and waits
    # for the job's advisory lock, the job holds that lock and waits for the
    # hypertable (CI, 2026-10-16). No test here needs a job to run: they read
    # the job catalogue and call compress_chunk / drop_chunks themselves, so a
    # job firing could only ever make a result depend on timing.
    command = list(postgres_args) or ["postgres"]
    command += ["-c", "timescaledb.max_background_workers=0"]
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={PASSWORD}",
         "-p", f"127.0.0.1:{port}:5432", *extra, image(), *command],
        check=True, capture_output=True,
    )
    url = f"postgresql://postgres:{PASSWORD}@127.0.0.1:{port}/postgres"
    import psycopg

    deadline = time.monotonic() + 120
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as c:
                c.execute("SELECT 1")
            return name, url
        except Exception:  # noqa: BLE001 - not up yet
            if time.monotonic() > deadline:
                stop(name)
                raise
            time.sleep(1)


def stop(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


__all__ = ["DEFAULT_IMAGE", "DEFAULT_PREFIX", "IMAGE_ENV", "PREFIX_ENV", "docker_available", "image", "start", "stop"]
