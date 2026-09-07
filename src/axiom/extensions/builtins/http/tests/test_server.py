# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `http` built-in extension — app factory + runners.

These tests prove the primitive consumer extensions depend on:
FastAPI app construction + threaded uvicorn serving + clean shutdown.
Classroom-specific routing is tested elsewhere (it's a consumer).
"""

from __future__ import annotations

import urllib.request

import pytest

from axiom.extensions.builtins.http import (
    ThreadedServer,
    create_app,
)

# ---------------------------------------------------------------------------
# create_app — basic shape
# ---------------------------------------------------------------------------


class TestCreateApp:
    def test_returns_fastapi_app(self):
        from fastapi import FastAPI

        app = create_app(title="Test", version="0.0.1")
        assert isinstance(app, FastAPI)
        assert app.title == "Test"
        assert app.version == "0.0.1"

    def test_app_accepts_custom_routers(self):
        from fastapi import APIRouter
        from fastapi.testclient import TestClient

        app = create_app()
        router = APIRouter()

        @router.get("/ping")
        def _ping() -> dict:
            return {"pong": True}

        app.include_router(router)

        # Assert the route is actually served — behaviour, not internal
        # route-object shape. FastAPI's route representation after
        # include_router has churned across versions (e.g. the
        # `_IncludedRouter` wrapper that has no `.path`), so introspecting
        # `app.router.routes` is brittle; a request round-trip is not.
        with TestClient(app) as client:
            resp = client.get("/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": True}


# ---------------------------------------------------------------------------
# ThreadedServer — start, answer, shutdown cleanly
# ---------------------------------------------------------------------------


class TestThreadedServer:
    def test_starts_and_accepts_requests(self):
        from fastapi import APIRouter

        app = create_app()
        router = APIRouter()

        @router.get("/hello")
        def _hello() -> dict:
            return {"message": "hello"}

        app.include_router(router)

        with ThreadedServer(app).serving() as srv:
            assert srv.bound_port > 0
            with urllib.request.urlopen(
                srv.base_url + "/hello", timeout=5,
            ) as resp:
                assert resp.status == 200
                import json as _json
                body = _json.loads(resp.read().decode("utf-8"))
                assert body == {"message": "hello"}

    def test_bound_port_is_stable_after_start(self):
        app = create_app()
        srv = ThreadedServer(app)
        srv.start()
        try:
            first = srv.bound_port
            second = srv.bound_port
            assert first == second
            assert first > 0
        finally:
            srv.shutdown()

    def test_shutdown_is_idempotent(self):
        app = create_app()
        srv = ThreadedServer(app)
        srv.start()
        srv.shutdown()
        srv.shutdown()  # must not raise

    def test_start_twice_raises(self):
        app = create_app()
        srv = ThreadedServer(app)
        srv.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                srv.start()
        finally:
            srv.shutdown()

    def test_concurrent_requests_actually_concurrent(self):
        """The whole point of moving off stdlib HTTPServer — two slow
        requests hitting the server at the same time should run
        concurrently, not serialize."""
        import asyncio
        import threading
        import time

        from fastapi import APIRouter

        app = create_app()
        router = APIRouter()

        @router.get("/slow")
        async def _slow() -> dict:
            await asyncio.sleep(0.25)
            return {"ok": True}

        app.include_router(router)

        with ThreadedServer(app).serving() as srv:
            url = srv.base_url + "/slow"
            results: list[float] = []

            def _fire() -> None:
                t0 = time.time()
                with urllib.request.urlopen(url, timeout=5):
                    pass
                results.append(time.time() - t0)

            # Fire three concurrent requests. Total wall-clock should
            # be much less than 3 × 0.25s = 0.75s if concurrency works.
            threads = [threading.Thread(target=_fire) for _ in range(3)]
            t_start = time.time()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            wall = time.time() - t_start

            # Each request takes ~0.25s; if serialized would be ~0.75s.
            # Give some slack for scheduling — should be < 0.5s.
            assert wall < 0.5, (
                f"requests appear to serialize (wall={wall:.3f}s); "
                "FastAPI/uvicorn async is not actually concurrent"
            )
