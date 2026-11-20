# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A test harness: the real library router over a throwaway SQLite catalog, for client work and screenshots.

Not a runtime. It binds the same SQLite seams the tests bind (ADR-174: SQLite
in tests, never in a running node), a temporary bronze directory, no embedder,
and a stand-in for the serving stack's authz hook that takes the principal and
site from ``x-test-principal`` / ``x-test-site`` (default ``@ben:alpha`` at
``alpha``). It listens on loopback only.

    python -m axiom.extensions.builtins.library.tests.harness_server --port 8977 \\
        [--manifest path/to/axiom-extension.toml ...] [--seed DIR] [--slow-ms 40]

``--manifest`` loads a pack's contributions (extractors, previewers, a Data Book
scheme, routes); ``--seed`` uploads every file in a directory through the
pipeline before serving; ``--slow-ms`` delays each chunk so a large upload can
be watched (and photographed) mid-flight.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def build_app(work: Path, *, manifests: list[str], slow_ms: int = 0):
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from axiom.extensions.builtins.library import contrib
    from axiom.extensions.builtins.library import store as lib_store
    from axiom.extensions.builtins.library.api import build_router
    from axiom.extensions.builtins.library.db_models import Base
    from axiom.infra.site_authority import bind_caller_site, unbind_caller_site
    from axiom.rag import store_factory

    os.environ.setdefault("AXIOM_LIBRARY_BRONZE_ROOT", str(work / "bronze"))
    os.environ.setdefault("AXIOM_SITE", "alpha")
    os.environ.setdefault("AXIOM_LIBRARY_TAXONOMY", "generic")
    engine = create_engine(f"sqlite:///{work / 'catalog.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    store_factory.allow_sqlite_for_tests(True)
    rag_url = f"sqlite:///{work / 'rag.db'}"

    def open_rag():
        s = store_factory.create_store(rag_url)
        s.connect()
        return s

    import axiom.rag.embeddings as emb
    from axiom.extensions.builtins.data_platform.rag_embed import embedder

    embedder.embed_texts = lambda texts: None  # keyword retrieval only; no embedding service
    emb.embed_texts = lambda texts: None
    import axiom.extensions.builtins.library.forward as fwd

    fwd.bound_node = lambda: None
    lib_store.set_provider(provider)
    lib_store.set_rag_store(open_rag)
    contrib.reset(rediscover=False)
    for m in manifests:
        contrib.add_manifest(m, scope="pack")

    app = FastAPI(title="library harness")
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["Upload-Offset"])

    @app.middleware("http")
    async def verified(request: Request, call_next):
        request.state.principal = request.headers.get("x-test-principal") or "@ben:alpha"
        token = bind_caller_site(request.headers.get("x-test-site") or "alpha")
        try:
            if slow_ms and request.method == "PATCH" and "/uploads/" in request.url.path:
                await asyncio.sleep(slow_ms / 1000)
            return await call_next(request)
        finally:
            unbind_caller_site(token)

    app.router.routes.extend(build_router().routes)
    return app


def seed(directory: Path) -> int:
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.scope import Caller

    caller = Caller(principal="@ben:alpha", site="alpha", surface="harness")
    n = 0
    for p in sorted(directory.iterdir()):
        if p.is_file():
            try:
                service.upload(caller, p.name, p.read_bytes())
                n += 1
            except Exception as exc:  # noqa: BLE001 - a seed file the library refuses is reported, not fatal
                print(f"seed: {p.name}: {exc}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8977)
    ap.add_argument("--manifest", action="append", default=[])
    ap.add_argument("--seed")
    ap.add_argument("--slow-ms", type=int, default=0)
    ap.add_argument("--workdir")
    args = ap.parse_args()
    work = Path(args.workdir or tempfile.mkdtemp(prefix="library-harness-"))
    work.mkdir(parents=True, exist_ok=True)
    app = build_app(work, manifests=args.manifest, slow_ms=args.slow_ms)
    if args.seed:
        print(f"seeded {seed(Path(args.seed))} file(s)")
    import uvicorn

    print(f"library harness on http://127.0.0.1:{args.port}/api/v1/library (state in {work})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
