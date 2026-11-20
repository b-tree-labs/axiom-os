# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi status` reports a service that does not answer. It does not wait it out.

A full-suite run killed `axi status --json` at its 15s caller budget. The same
command finishes in about five seconds when every closed port refuses at once.
The extra time is a port that accepts and then says nothing: the API probe
waits five seconds for /health, the signal probe waits five more, and those
sit in front of building the tool surface. Two silent ports and the command
never prints.

A database that accepts the connection and then does not answer a query is the
same failure, one layer in. connect_timeout does not cover it.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

import psycopg2

from axiom.extensions.builtins.data_platform.tests import timescale_container
from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus
from axiom.infra import node_functions as nf


def _blackhole(port: int) -> list[socket.socket]:
    """Accept connections on every loopback address and never reply."""
    sockets: list[socket.socket] = []

    def hold(sock: socket.socket) -> None:
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            # Keep the accepted socket open so the client blocks in its read.
            time.sleep(30)
            conn.close()

    for family, host in (
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET6, "::1"),
    ):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            sock.close()
            continue
        sock.listen(8)
        threading.Thread(target=hold, args=(sock,), daemon=True).start()
        sockets.append(sock)
    if not sockets:
        pytest.skip(f"cannot bind a silent listener on {port}")
    return sockets


def _close(sockets: list[socket.socket]) -> None:
    for sock in sockets:
        sock.close()


def test_silent_ports_do_not_spend_the_callers_whole_budget():
    """The three ports status probes, each accepting and silent.

    In-process the tool surface is already imported, so this measures the
    probes. Sequential five-second reads of two of them take well over six
    seconds; a status that reports silence comes back inside it.
    """
    opened: list[socket.socket] = []
    for port in (11434, 8000, 8765):
        opened.extend(_blackhole(port))
    try:
        started = time.monotonic()
        health = HealthChecker().check_all()
        elapsed = time.monotonic() - started
    finally:
        _close(opened)
    assert elapsed < 6, f"status waited {elapsed:.1f}s on services that said nothing"
    by_name = {svc.name: svc for svc in health.services}
    assert by_name["Signal Server"].status == HealthStatus.UNKNOWN
    assert by_name["API Server"].status == HealthStatus.UNKNOWN
    assert "overall" in health.to_dict()


def test_the_platform_url_reaches_libpq(monkeypatch, tmp_path):
    """The default URL names its SQLAlchemy driver. libpq rejects that spelling
    before it connects, so status reported 'invalid dsn' and never learned
    whether the database was down."""
    cfg = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(cfg))
    nf.apply(role="platform", functions=["ingest", "data_platform"], path=cfg)
    monkeypatch.setenv(
        "AXIOM_DB_URL", "postgresql+psycopg2://axiom:axiom@127.0.0.1:9/axiom_db"
    )
    health = HealthChecker().check_database()
    assert health.status == HealthStatus.UNHEALTHY
    assert "invalid dsn" not in health.message.lower()


def test_a_database_query_that_does_not_return_is_not_waited_out(monkeypatch):
    """connect_timeout ends a refused connection. It does not end a query.

    A catalog lock makes `SELECT extversion FROM pg_extension` wait forever,
    and status sat in that wait until the caller killed it. The check reports
    the database as not answering.
    """
    if not timescale_container.docker_available():
        pytest.skip("docker is not available")
    name, url = timescale_container.start()
    holder = psycopg2.connect(url)
    holder.autocommit = False
    holder.cursor().execute("LOCK TABLE pg_extension IN ACCESS EXCLUSIVE MODE")
    monkeypatch.setenv("AXIOM_DB_URL", url.replace("postgresql://", "postgresql+psycopg2://", 1))
    try:
        box: dict = {}

        def run() -> None:
            box["health"] = HealthChecker().check_database()

        thread = threading.Thread(target=run, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(3)
        assert not thread.is_alive(), "status is still waiting on a query that is not answering"
        assert time.monotonic() - started < 3
        health = box["health"]
        assert health.status == HealthStatus.UNHEALTHY
        # A driver-prefixed URL must be connected, not rejected as a malformed DSN,
        # and the failure is the query that did not answer.
        assert "invalid dsn" not in health.message.lower()
    finally:
        holder.rollback()
        holder.close()
        timescale_container.stop(name)
