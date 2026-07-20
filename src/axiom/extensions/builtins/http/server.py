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
    """
    config = uvicorn.Config(
        app=app, host=host, port=port,
        log_level=log_level,
        # Prevent uvicorn from hijacking the process's signal
        # handlers — the CLI wants to catch Ctrl-C itself.
        lifespan="on",
    )
    server = uvicorn.Server(config)
    server.run()


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


__all__ = ["ThreadedServer", "create_app", "run_server"]
