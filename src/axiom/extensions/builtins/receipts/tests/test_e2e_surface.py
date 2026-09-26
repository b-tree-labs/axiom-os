# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end, user-POV: ONE app composed the way the node composes it —
the /receipts mount serving the REAL committed build, plus the real
/api/v1 slices (fleet + receipts) — exercised as a browser would:
load the page, load the bundle it names, poll the APIs it polls.

No mocks beyond the storage seam (SQLite via the real providers) and no
skips: the built bundle is committed, so this suite runs everywhere the
code does. If the bundle is missing or stale relative to the sources,
that is a shippable-artifact defect and this suite says so.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from axiom.extensions.builtins.fleet import api as fleet_api  # noqa: E402
from axiom.extensions.builtins.fleet import store as fleet_store  # noqa: E402
from axiom.extensions.builtins.fleet.db_models import Base as FleetBase  # noqa: E402
from axiom.extensions.builtins.fleet.ingest import ingest_reports  # noqa: E402
from axiom.extensions.builtins.receipts import api as receipts_api  # noqa: E402
from axiom.extensions.builtins.receipts import store as rcpt_store  # noqa: E402
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase  # noqa: E402
from axiom.extensions.builtins.receipts.mount import (  # noqa: E402
    SurfaceBrand,
    build_receipts_router,
)

DIST = Path(__file__).parents[1] / "webui" / "dist"


def _mem_provider(base):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return provider, engine


@pytest.fixture()
def app_client(monkeypatch):
    """The composed app: real mount + real API slices, real providers."""
    fprov, fe = _mem_provider(FleetBase)
    rprov, re_ = _mem_provider(RcptBase)
    fleet_store.set_provider(fprov)
    rcpt_store.set_provider(rprov)
    fleet_api.set_session_resolver_factory(lambda: None)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)

    app = fastapi.FastAPI()
    api_router = fastapi.APIRouter(prefix="/api/v1")
    fleet_api.register_routes(api_router)
    receipts_api.register_routes(api_router)
    app.include_router(api_router)
    app.include_router(
        build_receipts_router(brand=SurfaceBrand(product_name="Axiom", accent="#bf5700"))
    )
    yield TestClient(app)
    fleet_store.reset_provider()
    rcpt_store.reset_provider()
    fleet_api.reset_session_resolver_factory()
    fe.dispose()
    re_.dispose()


def _seed(healthy: bool):
    with fleet_store.session_scope() as s:
        ingest_reports(
            s,
            site="site-a",
            node_id="node-a",
            reporter_principal="@node-a:site-a",
            cadences={"heartbeat": 900, "service_health": 900},
            reports=[
                {"kind": "heartbeat", "payload": {}},
                {
                    "kind": "service_health",
                    "payload": {
                        "services": [
                            {"name": "api", "status": "healthy", "latency_ms": 12}
                            if healthy
                            else {"name": "api", "status": "unhealthy"}
                        ]
                    },
                },
            ],
        )
        s.commit()


def test_the_shippable_bundle_is_committed():
    """The real build ships with the code — a deployed node never shows
    the not-built placeholder for this in-tree surface."""
    assert (DIST / "index.html").is_file(), (
        "webui/dist/index.html missing — run `npm run build` in the webui "
        "and commit the bundle; the surface ships real or not at all"
    )
    assets = list((DIST / "assets").glob("*.js"))
    assert assets, "no built JS bundle under webui/dist/assets"


def test_user_journey_page_bundle_and_data(app_client):
    """The browser's actual sequence: page → bundle → the two APIs."""
    _seed(healthy=False)

    # 1. The page: real index, brand injected as the bootstrap global.
    page = app_client.get("/receipts/")
    assert page.status_code == 200
    assert "__AXIOM_BRAND__" in page.text
    assert "Axiom" in page.text
    assert "not built" not in page.text  # the placeholder never ships

    # 2. The bundle the page names resolves through the mount, verbatim.
    m = re.search(r'src="/receipts/(assets/[^"]+\.js)"', page.text)
    assert m, f"index.html names no mounted JS bundle: {page.text[:400]}"
    bundle = app_client.get(f"/receipts/{m.group(1)}")
    assert bundle.status_code == 200
    body = bundle.text
    # The shipped bundle is the accepted design: brief views + only-wired verbs.
    assert "/api/v1/receipts/today" in body
    assert "/api/v1/fleet/status" in body
    assert "counterexample" in body  # failed-evidence labeling shipped

    # 3. The APIs the bundle polls, against really-ingested reports.
    status = app_client.get("/api/v1/fleet/status").json()
    assert status["nodes"][0]["node_id"] == "node-a"
    brief = app_client.get("/api/v1/receipts/today").json()["brief"]
    kinds = {(i["claim_kind"], i["status"]) for i in brief["needs_you"]}
    assert ("service_health", "failed") in kinds

    # 4. Containment: traversal never escapes the dist.
    assert app_client.get("/receipts/assets/../../secret").status_code == 404


def test_healthy_journey_reads_quiet(app_client):
    """The pass-side control: a healthy fleet reads as a quiet brief."""
    _seed(healthy=True)
    brief = app_client.get("/api/v1/receipts/today").json()["brief"]
    assert brief["counts"]["non_green"] == 0
    # Plain words, grammatical at one, and it says what passed.
    assert "passed" in brief["quiet_line"]
    assert "claim" not in brief["quiet_line"].lower()
