# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``/readyz`` is the one unauthenticated path, and says nothing but ready or not.

A front door or external probe cannot present a credential. A served app whose
``/readyz`` sat behind its authz answered 403 and could not be used as a
readiness check (the supervisor was unaffected: it reads a pipe). The readiness
answer is therefore served ahead of the authz layer, and carries no identity and
no data.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.middleware import AuthzDecision, MiddlewareConfig
from axiom.extensions.builtins.http.server import (
    create_app,
    install_readiness,
    mark_draining,
    mark_ready,
    mark_starting,
)


def _deny_all(request):
    return AuthzDecision(allow=False, reason="no credential", code="forbidden")


def _app():
    app = create_app(middleware=MiddlewareConfig(authz=_deny_all))

    @app.get("/secret")
    def secret():
        return {"secret": "data", "who": "somebody"}

    @app.post("/readyz")
    def not_the_probe():
        return {"x": 1}

    install_readiness(app)
    return app


def test_ready_answers_200_without_credentials_though_authz_denies_everything_else():
    c = TestClient(_app())
    assert c.get("/secret").status_code == 403, "the authz layer is really in force"
    r = c.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ready": True}


def test_it_answers_503_while_starting_and_200_once_started():
    app = _app()
    c = TestClient(app)
    mark_starting(app)
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json() == {"ready": False}
    mark_ready(app)
    assert c.get("/readyz").status_code == 200


def test_it_answers_503_while_draining():
    app = _app()
    mark_draining(app)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503 and r.json() == {"ready": False}


def test_the_answer_leaks_nothing_else():
    app = _app()
    c = TestClient(app)
    for starting in (False, True):
        (mark_starting if starting else mark_ready)(app)
        r = c.get("/readyz", headers={"authorization": "Bearer whatever", "x-request-id": "abc"})
        assert set(json.loads(r.content)) == {"ready"}, "no reason, version, identity or data"
        assert len(r.content) < 32
        assert set(r.headers) <= {"content-type", "content-length", "cache-control"}
        assert r.headers["cache-control"] == "no-store"
    mark_ready(app)
    head = c.head("/readyz")
    assert head.status_code == 200 and head.content == b""


def test_only_get_and_head_on_that_exact_path_skip_authz():
    c = TestClient(_app())
    assert c.post("/readyz").status_code == 403
    assert c.get("/readyz/extra").status_code == 403
    assert c.get("/readyzz").status_code == 403


def test_installing_twice_changes_nothing():
    app = _app()
    install_readiness(app)
    assert sum(1 for m in app.user_middleware if "Readiness" in m.cls.__name__) == 1


def test_a_real_server_answers_it_unauthenticated():
    import urllib.request

    from axiom.extensions.builtins.http.server import ThreadedServer

    with ThreadedServer(_app()).serving() as srv:
        with urllib.request.urlopen(f"{srv.base_url}/readyz", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read()) == {"ready": True}
