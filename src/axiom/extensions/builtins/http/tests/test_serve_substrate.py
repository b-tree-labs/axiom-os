# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the composed serving substrate (spec-serve §13).

Covers the BUILD-NOW scope: registry compose order + conflict detection,
the shared middleware error envelope + request id, the authz seam, and
that the three built-in consumer mounts compose into one app.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.compose import compose_app, route_table
from axiom.extensions.builtins.http.middleware import (
    AuthzDecision,
    MiddlewareConfig,
)
from axiom.extensions.builtins.http.registry import (
    MountSpec,
    PrefixConflictError,
    RouterRegistry,
)


def _router(path: str) -> APIRouter:
    r = APIRouter()

    @r.get(path)
    def _h() -> dict:
        return {"ok": True}

    return r


# ---------------------------------------------------------------------------
# Registry (SRV-004 / SRV-006)
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_specs_sorted_by_prefix(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/zeta", _router("/zeta"), "z"))
        reg.register(MountSpec("/alpha", _router("/alpha"), "a"))
        prefixes = [s.prefix for s in reg.specs()]
        assert prefixes == ["/alpha", "/zeta"]

    def test_conflicting_prefix_raises(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/classroom", _router("/x"), "classroom"))
        with pytest.raises(PrefixConflictError):
            reg.register(
                MountSpec("/classroom/coordinator", _router("/y"), "other")
            )

    def test_non_conflicting_sibling_prefix_ok(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/classroom", _router("/x"), "classroom"))
        # /classroomx is NOT a path-segment prefix of /classroom.
        reg.register(MountSpec("/classroomx", _router("/y"), "other"))
        assert len(reg.specs()) == 2

    def test_profile_filter(self):
        reg = RouterRegistry()
        reg.register(
            MountSpec("/a2a", _router("/a2a"), "fed", profiles=("server",))
        )
        reg.register(MountSpec("/open", _router("/open"), "x"))
        assert [s.prefix for s in reg.specs(profile="library")] == ["/open"]
        assert {s.prefix for s in reg.specs(profile="server")} == {
            "/a2a",
            "/open",
        }

    def test_bad_prefix_rejected(self):
        with pytest.raises(ValueError):
            MountSpec("noslash", _router("/x"), "x")
        with pytest.raises(ValueError):
            MountSpec("/trailing/", _router("/x"), "x")


# ---------------------------------------------------------------------------
# Compose order + conflict at compose time
# ---------------------------------------------------------------------------


class TestCompose:
    def test_compose_mounts_in_sorted_order(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/b", _router("/b"), "b"))
        reg.register(MountSpec("/a", _router("/a"), "a"))
        # allow_insecure: this test exercises mount ORDER, not authz.
        app = compose_app(registry=reg, include_builtins=False, allow_insecure=True)
        client = TestClient(app)
        assert client.get("/a").status_code == 200
        assert client.get("/b").status_code == 200

    def test_compose_conflict_raises_before_bind(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/dup", _router("/dup"), "one"))
        with pytest.raises(PrefixConflictError):
            reg.register(MountSpec("/dup", _router("/dup2"), "two"))


# ---------------------------------------------------------------------------
# Middleware (SRV-020 / SRV-021)
# ---------------------------------------------------------------------------


class TestMiddleware:
    def test_error_envelope_shape(self):
        reg = RouterRegistry()
        r = APIRouter()

        @r.get("/boom")
        def _boom() -> dict:
            raise RuntimeError("kaboom")

        reg.register(MountSpec("/boom", r, "demo"))
        # allow_insecure: this test exercises the error envelope, not authz.
        app = compose_app(registry=reg, include_builtins=False, allow_insecure=True)
        # raise_server_exceptions=False so the handler runs instead of
        # re-raising into the test.
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/boom")
        assert resp.status_code == 500
        body = resp.json()
        assert set(body["error"]) == {
            "code",
            "message",
            "request_id",
            "extension",
        }
        assert body["error"]["code"] == "internal"
        assert body["error"]["extension"] == "demo"
        assert body["error"]["request_id"].startswith("req_")

    def test_request_id_header_present(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/ping", _router("/ping"), "demo"))
        # allow_insecure: this test exercises the request-id header, not authz.
        app = compose_app(registry=reg, include_builtins=False, allow_insecure=True)
        client = TestClient(app)
        resp = client.get("/ping")
        assert resp.status_code == 200
        assert resp.headers["x-request-id"].startswith("req_")

    def test_http_exception_normalized(self):
        from fastapi import HTTPException

        reg = RouterRegistry()
        r = APIRouter()

        @r.get("/nope")
        def _nope() -> dict:
            raise HTTPException(status_code=404, detail="not here")

        reg.register(MountSpec("/nope", r, "demo"))
        app = compose_app(registry=reg, include_builtins=False)
        client = TestClient(app)
        resp = client.get("/nope")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Authz seam (SRV-022)
# ---------------------------------------------------------------------------


class TestAuthzSeam:
    def test_deny_returns_403_envelope_on_guarded_mount(self):
        reg = RouterRegistry()
        reg.register(MountSpec("/guarded", _router("/guarded"), "demo"))
        mw = MiddlewareConfig(
            authz=lambda req: AuthzDecision(allow=False, reason="nope")
        )
        app = compose_app(registry=reg, middleware=mw, include_builtins=False)
        client = TestClient(app)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"
        assert resp.json()["error"]["message"] == "nope"

    def test_public_mount_bypasses_authz(self):
        reg = RouterRegistry()
        reg.register(
            MountSpec(
                "/.well-known",
                _router("/.well-known/card"),
                "fed",
                requires_authz=False,
            )
        )
        mw = MiddlewareConfig(
            authz=lambda req: AuthzDecision(allow=False, reason="denied")
        )
        app = compose_app(registry=reg, middleware=mw, include_builtins=False)
        client = TestClient(app)
        resp = client.get("/.well-known/card")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Built-in consumer mounts (SRV-005)
# ---------------------------------------------------------------------------


class TestBuiltinMounts:
    def test_route_table_contains_three_consumers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.extensions.builtins.http.mounts.get_user_state_dir",
            lambda: tmp_path,
        )
        reg = RouterRegistry()
        table = route_table(registry=reg)
        prefixes = {e.prefix for e in table}
        assert "/ingest" in prefixes
        assert "/classroom" in prefixes
        assert "/herald/inbound" in prefixes

    def test_builtins_compose_into_one_app(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.extensions.builtins.http.mounts.get_user_state_dir",
            lambda: tmp_path,
        )
        reg = RouterRegistry()
        # allow_insecure: this test exercises built-in mount composition, not authz.
        app = compose_app(registry=reg, allow_insecure=True)
        client = TestClient(app)
        # The ingest route exists (422 on empty body proves it's mounted,
        # not 404).
        resp = client.post("/ingest", json={})
        assert resp.status_code != 404
        # Herald rejects an unknown vendor with 404 from its handler (the
        # route is mounted; the 404 is the handler's, not a missing route).
        resp = client.post("/herald/inbound/unknown", json={})
        assert resp.status_code in (404, 401)


# ---------------------------------------------------------------------------
# Manifest discovery (SRV-003)
# ---------------------------------------------------------------------------


class TestManifestDiscovery:
    def test_service_block_with_prefix_is_mounted(self):
        reg = RouterRegistry()
        manifests = [
            {
                "extension": {
                    "provides": [
                        {
                            "kind": "service",
                            "name": "demo",
                            "prefix": "/demo",
                            "entry": (
                                "axiom.extensions.builtins.http.tests."
                                "test_serve_substrate:_demo_mount_spec"
                            ),
                        }
                    ]
                }
            }
        ]
        table = route_table(
            registry=reg, manifests=manifests, include_builtins=False
        )
        assert any(e.prefix == "/demo" for e in table)


def _demo_mount_spec() -> MountSpec:
    """Module-level factory used by the manifest-discovery test."""
    return MountSpec("/demo", _router("/demo"), "demo")


def test_compose_isolates_a_failing_mount():
    """A mount that raises at include-time must not sink the app — its
    siblings still mount (spec-serve §14.1 coupled-blast-radius mitigation)."""
    from fastapi import APIRouter

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry

    good = APIRouter()

    @good.get("/ok")
    def _ok():
        return {"ok": True}

    reg = RouterRegistry()
    reg.register(MountSpec(prefix="/bad", router=object(), extension="bad"))  # include raises
    reg.register(MountSpec(prefix="/good", router=good, extension="good"))

    # allow_insecure: this test exercises fault isolation, not authz.
    app = compose_app(registry=reg, include_builtins=False, allow_insecure=True)  # must not raise
    # Assert behaviorally, not by scraping app.routes: FastAPI >= 0.138 adds an
    # included router as a single lazy wrapper (_IncludedRouter) with no flat
    # `.path`, so route-introspection is version-fragile. Hitting the endpoint
    # proves the good sibling actually mounted despite the bad mount.
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/ok")
    assert resp.status_code == 200, (
        f"good sibling did not survive the bad mount (got {resp.status_code})"
    )
    assert resp.json() == {"ok": True}


def test_compose_fails_closed_on_authz_required_mount_without_hook(monkeypatch):
    """An auth-required mount must NOT be served when no authz hook is wired
    (fail-closed, SRV-022). Serving e.g. POST /ingest with no enforcement is a
    security hole — anonymous external writes."""
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry

    monkeypatch.delenv("AXIOM_SERVE_INSECURE", raising=False)

    secured = APIRouter()

    @secured.post("/ingest")
    def _ingest():
        return {"wrote": True}

    reg = RouterRegistry()
    reg.register(MountSpec(prefix="/data", router=secured, extension="data",
                           requires_authz=True))

    # No middleware.authz hook → the mount is refused, not silently exposed.
    # auto_authz=False exercises the raw substrate contract (the default
    # adapter is covered separately in test_authz_hook + the auto-wire test).
    app = compose_app(registry=reg, include_builtins=False, auto_authz=False)
    assert TestClient(app).post("/ingest").status_code == 404, (
        "auth-required mount was served without an authz hook — fail-OPEN hole"
    )

    # Explicit opt-out serves it (dev/loopback escape hatch).
    app_insecure = compose_app(registry=reg, include_builtins=False,
                               allow_insecure=True)
    assert TestClient(app_insecure).post("/ingest").status_code == 200

    # A public mount (requires_authz=False) is served with no hook, as expected.
    pub = APIRouter()

    @pub.get("/health")
    def _health():
        return {"ok": True}

    reg2 = RouterRegistry()
    reg2.register(MountSpec(prefix="/h", router=pub, extension="h",
                            requires_authz=False))
    assert TestClient(compose_app(registry=reg2, include_builtins=False)).get(
        "/health").status_code == 200


def test_compose_auto_wires_default_authz_hook_in_dev(monkeypatch):
    """With ``auto_authz`` (the default), an auth-required mount is SERVED in
    dev mode because ``compose_app`` auto-wires the GUARD adapter — no
    per-deployment middleware plumbing. The same mount is refused when there
    is nothing safe to wire (production, no keys)."""
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry

    monkeypatch.delenv("AXIOM_SERVE_INSECURE", raising=False)
    monkeypatch.delenv("AXIOM_API_KEY", raising=False)
    monkeypatch.delenv("AXIOM_HTTP_API_KEYS", raising=False)

    secured = APIRouter()

    @secured.get("/secure")
    def _secure():
        return {"ok": True}

    def _reg():
        reg = RouterRegistry()
        reg.register(MountSpec(prefix="/s", router=secured, extension="s",
                               requires_authz=True))
        return reg

    # Dev mode → adapter wires a permit-all engine + dev principal → served.
    monkeypatch.setenv("AXIOM_MODE", "dev")
    app = compose_app(registry=_reg(), include_builtins=False)
    assert TestClient(app).get("/secure").status_code == 200

    # Production with no keys → adapter returns None → fail-closed refusal.
    monkeypatch.setenv("AXIOM_MODE", "production")
    app_prod = compose_app(registry=_reg(), include_builtins=False)
    assert TestClient(app_prod).get("/secure").status_code == 404
