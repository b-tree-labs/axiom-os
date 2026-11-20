# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``GET /program/status`` — the status read on the composed HTTP substrate.

Mounted the way every extension mounts: a ``service`` provide-block whose
entry returns a ``MountSpec`` with ``requires_authz=True``, so
``compose_app`` refuses to serve it at all without an authz hook and the
hook refuses a request that carries no credential. The route dispatches
through ``invoke_capability`` on the ``web`` surface — the same door as
the CLI and MCP — and reads only the node's own data file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.http.compose import compose_app  # noqa: E402
from axiom.extensions.builtins.http.registry import RouterRegistry  # noqa: E402
from axiom.extensions.builtins.program import mount  # noqa: E402

_MANIFEST = Path(mount.__file__).parent / "axiom-extension.toml"
_TOKEN = "tok-program-test"


@pytest.fixture
def bare(state_dir):
    """The router alone — route behaviour without the substrate."""
    app = FastAPI()
    app.include_router(mount.build_program_router(state_dir=state_dir))
    return TestClient(app)


@pytest.fixture
def no_ambient_credentials(monkeypatch):
    for name in (
        "AXIOM_SERVE_INSECURE",
        "AXIOM_API_KEY",
        "AXIOM_HTTP_API_KEYS",
        "AXIOM_GATE_API_KEYS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    # dev mode on a NETWORK bind: credentials are wired with no anonymous
    # fallback (bearer required), and no graduated policy is needed for the
    # authenticated read — the policy engine's own graduation is not this
    # extension's subject. Production is covered separately below.
    monkeypatch.setenv("AXIOM_MODE", "dev")


def _composed(state_dir, **kwargs):
    reg = RouterRegistry()
    reg.register(mount.mount_spec(state_dir=state_dir))
    return TestClient(compose_app(registry=reg, include_builtins=False, **kwargs))


class TestTheMount:
    def test_mount_spec_requires_authz_under_program(self, state_dir):
        spec = mount.mount_spec(state_dir=state_dir)
        assert spec.prefix == "/program"
        assert spec.extension == "program"
        assert spec.requires_authz is True

    def test_the_manifest_declares_the_service_mount(self):
        provides = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))["extension"]["provides"]
        services = [p for p in provides if p["kind"] == "service"]
        assert [s["entry"] for s in services] == [
            "axiom.extensions.builtins.program.mount:mount_spec"
        ]
        assert services[0]["requires_authz"] is True

    def test_the_route_is_a_concrete_get(self, state_dir):
        router = mount.mount_spec(state_dir=state_dir).router
        routes = {(r.path, tuple(sorted(r.methods or ()))) for r in router.routes}
        assert ("/program/status", ("GET",)) in routes


class TestAuthz:
    def test_without_an_authz_hook_the_mount_is_not_served(self, state_dir, no_ambient_credentials):
        client = _composed(state_dir, auto_authz=False)
        assert client.get("/program/status", params={"scope": "schedule"}).status_code == 404

    def test_an_unauthenticated_request_is_refused(
        self, state_dir, no_ambient_credentials, monkeypatch
    ):
        monkeypatch.setenv("AXIOM_HTTP_API_KEYS", f"{_TOKEN}:@svc:local")
        client = _composed(state_dir, bind_host="0.0.0.0")
        resp = client.get("/program/status", params={"scope": "schedule"})
        assert resp.status_code in (401, 403)
        assert "items" not in resp.text

    def test_production_refuses_an_unauthenticated_request(
        self, state_dir, no_ambient_credentials, monkeypatch
    ):
        monkeypatch.setenv("AXIOM_MODE", "production")
        monkeypatch.setenv("AXIOM_HTTP_API_KEYS", f"{_TOKEN}:@svc:local")
        for bind in ("127.0.0.1", "0.0.0.0"):
            client = _composed(state_dir, bind_host=bind)
            resp = client.get("/program/status", params={"scope": "schedule"})
            assert resp.status_code in (401, 403), bind
            assert "items" not in resp.text

    def test_a_wrong_credential_is_refused(self, state_dir, no_ambient_credentials, monkeypatch):
        monkeypatch.setenv("AXIOM_HTTP_API_KEYS", f"{_TOKEN}:@svc:local")
        client = _composed(state_dir, bind_host="0.0.0.0")
        resp = client.get(
            "/program/status",
            params={"scope": "schedule"},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert resp.status_code in (401, 403)

    def test_an_authenticated_request_is_answered(
        self, state_dir, no_ambient_credentials, monkeypatch
    ):
        monkeypatch.setenv("AXIOM_HTTP_API_KEYS", f"{_TOKEN}:@svc:local")
        client = _composed(state_dir, bind_host="0.0.0.0")
        resp = client.get(
            "/program/status",
            params={"scope": "schedule"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 4


class TestTheRoute:
    def test_schedule_returns_the_skills_answer(self, bare):
        resp = bare.get("/program/status", params={"scope": "schedule"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "schedule"
        assert [i["id"] for i in body["items"]] == ["i-one", "i-two", "i-three", "i-four"]

    def test_drift_is_served_and_labelled(self, bare):
        body = bare.get("/program/status", params={"scope": "drift"}).json()
        assert body["basis"] == "data-file-only"
        assert body["count"] == 3

    def test_an_unknown_principal_is_a_404(self, bare):
        resp = bare.get("/program/status", params={"scope": "person", "key": "@nobody:example-org"})
        assert resp.status_code == 404

    def test_a_bad_scope_is_a_422(self, bare):
        assert bare.get("/program/status", params={"scope": "everything"}).status_code == 422

    def test_a_keyed_scope_without_a_key_is_a_422(self, bare):
        assert bare.get("/program/status", params={"scope": "lane"}).status_code == 422

    def test_a_caller_cannot_name_the_data_file(self, bare, data_file):
        resp = bare.get("/program/status", params={"scope": "schedule", "data": str(data_file)})
        assert resp.status_code == 422
        assert "data" in resp.text

    def test_no_data_file_on_the_node_is_a_503(self, tmp_path):
        app = FastAPI()
        app.include_router(mount.build_program_router(state_dir=tmp_path / "empty"))
        resp = TestClient(app).get("/program/status", params={"scope": "schedule"})
        assert resp.status_code == 503


def _seed_log(state_dir):
    """Populate the node's change log the way the node would — one sync."""
    import logging

    from axiom.extensions.builtins.program.skills import sync
    from axiom.infra.skills import SkillContext, SkillRegistry

    ctx = SkillContext(
        registry=SkillRegistry(),
        state_dir=state_dir,
        logger=logging.getLogger("test.program.http.seed"),
        user_prompt=None,
        surface="cli",
    )
    sync.run({}, ctx)


class TestTheChangesRoute:
    def test_the_changes_route_is_a_concrete_get(self, state_dir):
        router = mount.mount_spec(state_dir=state_dir).router
        routes = {(r.path, tuple(sorted(r.methods or ()))) for r in router.routes}
        assert ("/program/changes", ("GET",)) in routes

    def test_changes_returns_the_deltas(self, bare, state_dir):
        _seed_log(state_dir)
        resp = bare.get("/program/changes", params={"principal": "@casey:example-org"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 9

    def test_a_get_never_advances_the_watermark(self, bare, state_dir):
        _seed_log(state_dir)
        first = bare.get("/program/changes", params={"principal": "@casey:example-org"}).json()
        second = bare.get("/program/changes", params={"principal": "@casey:example-org"}).json()
        # a GET is a read — both calls see everything, nothing was marked seen
        assert first["advanced"] is False
        assert first["count"] == second["count"] == 9

    def test_advance_is_not_a_forwarded_param_over_http(self, bare, state_dir):
        _seed_log(state_dir)
        resp = bare.get(
            "/program/changes",
            params={"principal": "@casey:example-org", "advance": "true"},
        )
        assert resp.status_code == 422
        assert "advance" in resp.text

    def test_a_bad_principal_is_a_422(self, bare, state_dir):
        _seed_log(state_dir)
        resp = bare.get("/program/changes", params={"principal": "not-a-principal"})
        assert resp.status_code == 422
