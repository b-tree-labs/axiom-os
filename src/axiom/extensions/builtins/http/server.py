# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""FastAPI app factory + threaded uvicorn runner.

Consumers call :func:`create_app` to get a properly-configured
:class:`fastapi.FastAPI`, mount their own routers on it, and then run
it via either :func:`run_server` (blocks the calling thread — what a
CLI long-running serve command wants) or :class:`ThreadedServer` (for
tests + programmatic control that needs clean shutdown).

Both paths go through the same ``uvicorn.Server`` instance under the
hood so behavior is identical. Default bind is ``127.0.0.1:0`` (OS-
assigned port) so tests don't collide; CLIs override with a stable port.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

if TYPE_CHECKING:
    from .middleware import MiddlewareConfig


def create_app(
    *,
    title: str = "Axiom Service",
    version: str = "0.1.0",
    description: str = "",
    middleware: MiddlewareConfig | None = None,
) -> FastAPI:
    """Return a FastAPI app with Axiom's default configuration.

    Consumers add routers via ``app.include_router(...)`` after
    calling this.

    When ``middleware`` is supplied, the shared middleware chain (request
    logging + error normalization, plus the authz / peer-sig seams when
    their hooks are set) is installed (spec-serve §5–6). ``middleware=None``
    leaves the app bare — the historical behavior consumers relied on.
    """
    app = FastAPI(
        title=title,
        version=version,
        description=description,
        # Hide the default redoc / schema endpoints unless explicitly
        # opted into — classroom doesn't need them, and surfacing them
        # broadens the attack surface on a self-hosted instructor box.
        redoc_url=None,
    )
    if middleware is not None:
        # Imported lazily so a bare create_app() keeps its zero middleware
        # import cost and the modules stay decoupled.
        from .middleware import install_middleware

        install_middleware(app, middleware)
    return app


# ---------------------------------------------------------------------------
# Blocking runner — what a `serve` command calls
# ---------------------------------------------------------------------------


def install_readiness(app: FastAPI) -> None:
    """Add ``/readyz``: 200 while this copy takes new work, 503 once it drains.

    ``/healthz`` says the process is up. ``/readyz`` says whether a front door
    should send it anything new (ADR-182 D3): a copy being replaced is still up
    and still finishing its requests, and must not look like a failure.
    """
    if getattr(app.state, "axiom_readiness", False):
        return
    app.state.axiom_readiness = True
    app.state.axiom_draining = False

    from fastapi.responses import JSONResponse

    @app.get("/readyz", include_in_schema=False)
    def _readyz() -> JSONResponse:
        if app.state.axiom_draining:
            return JSONResponse({"ready": False, "reason": "draining"}, status_code=503)
        return JSONResponse({"ready": True})


def mark_draining(app: FastAPI) -> None:
    """This copy is being replaced: report not-ready, keep finishing work."""
    app.state.axiom_draining = True


#: How long a draining copy keeps reading connections it accepted just before
#: it stopped accepting. Long enough for a request already on the wire to be
#: parsed; short enough not to matter to a switch.
_ACCEPTED_GRACE_S = 0.5


class _SupervisedServer(uvicorn.Server):
    """A copy of the app run by :class:`axiom.infra.switch.Supervisor`.

    It accepts on the supervisor's socket, tells the supervisor it is ready
    only once startup has finished, and on SIGTERM reports not-ready, stops
    accepting and finishes what it holds — the drain half of a switch.
    """

    def __init__(self, config: uvicorn.Config, app: FastAPI, ready_fd: int | None) -> None:
        super().__init__(config)
        self._app = app
        self._ready_fd = ready_fd

    async def startup(self, sockets=None) -> None:  # type: ignore[override]
        await super().startup(sockets=sockets)
        if self._ready_fd is not None and not self.should_exit:
            import os

            try:
                os.write(self._ready_fd, b"R")
            finally:
                os.close(self._ready_fd)
                self._ready_fd = None

    def handle_exit(self, sig, frame) -> None:  # type: ignore[override]
        mark_draining(self._app)
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets=None) -> None:  # type: ignore[override]
        """Stop accepting, let just-accepted connections be read, then shut down.

        uvicorn closes every connection with no request in progress the moment
        it stops accepting. That includes one accepted a millisecond ago whose
        request bytes have not been parsed yet, and its client sees a reset —
        measured at 2 per 2 switches under load before this grace existed. The
        grace lets those requests start; uvicorn then waits for them as for
        any other in-flight request.
        """
        import asyncio

        for server in self.servers:
            server.close()
        await asyncio.sleep(_ACCEPTED_GRACE_S)
        await super().shutdown(sockets=sockets)


def run_server(
    app: FastAPI,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    log_level: str = "warning",
) -> None:
    """Run ``app`` to completion on the current thread.

    Blocks until the process receives Ctrl-C / SIGTERM. Log level
    defaults to ``warning`` so the CLI output isn't flooded with
    access logs; callers can bump it for debugging.

    Under a :class:`axiom.infra.switch.Supervisor` (``AXI_LISTEN_FD`` set),
    the copy accepts on the supervisor's socket instead of binding its own,
    signals readiness on ``AXI_READY_FD``, and drains for ``AXI_DRAIN_S``
    seconds on SIGTERM, so it can be replaced with no refused request.
    """
    import os
    import socket

    install_readiness(app)
    listen_fd = os.environ.get("AXI_LISTEN_FD")
    if listen_fd is None:
        config = uvicorn.Config(
            app=app, host=host, port=port,
            log_level=log_level,
            # Prevent uvicorn from hijacking the process's signal
            # handlers — the CLI wants to catch Ctrl-C itself.
            lifespan="on",
        )
        _SupervisedServer(config, app, ready_fd=None).run()
        return

    sock = socket.socket(fileno=int(listen_fd))
    ready_fd = os.environ.get("AXI_READY_FD")
    config = uvicorn.Config(
        app=app,
        log_level=log_level,
        lifespan="on",
        timeout_graceful_shutdown=float(os.environ.get("AXI_DRAIN_S", "30")),
    )
    server = _SupervisedServer(config, app, ready_fd=int(ready_fd) if ready_fd else None)
    server.run(sockets=[sock])


# ---------------------------------------------------------------------------
# Threaded runner — what tests + integration harnesses call
# ---------------------------------------------------------------------------


@dataclass
class ThreadedServer:
    """Uvicorn server running in a background thread.

    Use the :meth:`start` / :meth:`shutdown` explicit API, or the
    :meth:`serving` context manager for tests::

        with ThreadedServer(app).serving() as srv:
            requests.get(f"http://{srv.host}:{srv.port}/")
    """

    app: FastAPI
    host: str = "127.0.0.1"
    port: int = 0
    log_level: str = "warning"
    _server: uvicorn.Server | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def start(self, *, startup_timeout_s: float = 5.0) -> None:
        """Spin up the server in a background thread and wait for it
        to accept connections."""
        if self._server is not None:
            raise RuntimeError("ThreadedServer already started")
        config = uvicorn.Config(
            app=self.app, host=self.host, port=self.port,
            log_level=self.log_level,
            # Avoid touching the process-level signal handlers — the
            # test runner owns those.
            lifespan="on",
            # Nor the process's logging. uvicorn's default config attaches
            # StreamHandlers bound to the stdout/stderr of the moment; under
            # pytest that is a capture that closes after the test, and every
            # later test on the worker failed "I/O operation on closed file".
            # With no config, uvicorn's records propagate to whatever the
            # host configured.
            log_config=None,
        )
        self._server = uvicorn.Server(config)
        # Prevent uvicorn from installing its signal handlers (which
        # fight with pytest on Ctrl-C).
        self._server.install_signal_handlers = lambda: None  # type: ignore[assignment]

        self._thread = threading.Thread(
            target=self._server.run, daemon=True,
        )
        self._thread.start()

        # Block until uvicorn reports itself started; otherwise the
        # caller might race past the socket bind.
        import time
        deadline = time.time() + startup_timeout_s
        while time.time() < deadline:
            if self._server.started:
                return
            time.sleep(0.02)
        # Stop the thread we started: raising with it still running left a
        # server that finished starting later, unowned and never shut down.
        self.shutdown()
        raise TimeoutError(
            f"server did not start within {startup_timeout_s}s"
        )

    def shutdown(self, *, timeout_s: float = 5.0) -> None:
        if self._server is None or self._thread is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout=timeout_s)
        self._server = None
        self._thread = None

    @property
    def bound_port(self) -> int:
        """Actual port the server is listening on (resolved after
        :meth:`start` when ``port=0``)."""
        if self._server is None:
            raise RuntimeError("server not started")
        # uvicorn exposes servers as a list of per-socket servers; each
        # carries the underlying asyncio transport with a sockname.
        for srv in (self._server.servers or []):
            for sock in getattr(srv, "sockets", []):
                return sock.getsockname()[1]
        return self.port  # fallback if introspection fails

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.bound_port}"

    @contextmanager
    def serving(self) -> Iterator[ThreadedServer]:
        self.start()
        try:
            yield self
        finally:
            self.shutdown()


__all__ = ["ThreadedServer", "create_app", "install_readiness", "mark_draining", "run_server"]
