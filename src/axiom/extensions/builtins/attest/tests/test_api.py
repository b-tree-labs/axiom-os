# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/api/v1/attest from a browser's side: draft, present, answer, get a grant
at the gate, sign, and watch it arrive on the stream."""

from __future__ import annotations

import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.attest import api, signing  # noqa: E402
from axiom.extensions.builtins.fleet import api as fleet_api  # noqa: E402
from axiom.extensions.builtins.http.authz_hook import build_session_resolver  # noqa: E402
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router  # noqa: E402
from axiom.webauth import SESSION_COOKIE  # noqa: E402
from axiom.webauth.keys import reset_key_store_for_tests  # noqa: E402
from axiom.webauth.session import issue_session_token  # noqa: E402
from axiom.webauth.users import InMemoryUserStore, User  # noqa: E402

from .test_signing import SITE  # noqa: E402

BASE = "http://gate.example"
OP1 = User(user_id="op1", email="op1@example.org", name="Op One", roles=("user",), site=SITE,
           attributes={"idp": "entra"})  # fmt: skip
OP2 = User(user_id="op2", email="op2@example.org", name="Op Two", roles=("user",), site=SITE,
           attributes={"idp": "entra"})  # fmt: skip
ROUND = {"logbook": "demo_log", "entry_type": "ROUND_CHECK", "meaning": "performed",
         "title": "Round check", "fields": {"reading": "ok", "walkdown": True}}  # fmt: skip


@pytest.fixture
def client(attest_db, node_signer, tmp_path, monkeypatch):
    reset_key_store_for_tests()
    signing.set_provider(lambda: node_signer)
    fleet_api.set_session_resolver_factory(lambda: build_session_resolver(issuer=BASE))
    (tmp_path / "attest").mkdir()
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\nroles = ["operator"]\n'
    )
    monkeypatch.setattr(api, "_state_dir", lambda: tmp_path)
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app.include_router(router)
    app.include_router(build_webgate_router(InMemoryUserStore(), secure_cookies=False))
    c = TestClient(app, base_url=BASE, follow_redirects=False)
    yield c
    fleet_api.reset_session_resolver_factory()
    signing.reset_provider()
    reset_key_store_for_tests()


def as_(c, user):
    c.cookies.set(SESSION_COOKIE, issue_session_token(user, issuer=BASE, amr=("pwd",)))
    return c


def sign_round(c, **over):
    d = as_(c, OP1).post("/api/v1/attest/drafts", json={**ROUND, **over})
    assert d.status_code == 201, d.text
    p = c.post(f"/api/v1/attest/drafts/{d.json()['draft_id']}/present", json={"modality": "screen"})
    assert p.status_code == 201, p.text
    pid = p.json()["presentation_id"]
    r = c.post(
        f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"}
    )
    assert r.status_code == 200, r.text
    g = c.post("/gate/grants", json={"presentation_id": pid, "console_id": "console-1"})
    assert g.status_code == 200, g.text
    s = c.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": g.json()["grant"]})
    assert s.status_code == 201, s.text
    return s.json()["record"]


def test_the_whole_browser_path_signs_a_record(client, node_signer):
    rec = sign_round(client)
    assert rec["seq"] == 1 and rec["signer"]["principal"] == "@op1:site-a"
    assert rec["source"] == "screen" and rec["assurance"]["grant_id"]
    listed = client.get("/api/v1/attest/demo_log/records").json()
    assert [r["seq"] for r in listed["records"]] == [1]
    v = client.post("/api/v1/attest/demo_log/verify").json()
    assert v["ok"] and v["checked"] == 1


def test_everything_needs_a_session(client):
    assert client.get("/api/v1/attest/logbooks").status_code == 401
    assert client.post("/api/v1/attest/drafts", json=ROUND).status_code == 401


def test_logbooks_describe_what_a_surface_needs_to_render_them(client):
    logbooks = as_(client, OP1).get("/api/v1/attest/logbooks").json()["logbooks"]
    rc = next(
        t for b in logbooks if b["id"] == "demo_log" for t in b["types"] if t["id"] == "ROUND_CHECK"
    )
    assert rc["confirm"]["modalities_any"] == ["screen", "cli", "voice"]
    assert [f["id"] for f in rc["fields"]] == ["reading", "walkdown", "note"]


def test_someone_elses_draft_does_not_exist_for_you(client):
    d = as_(client, OP1).post("/api/v1/attest/drafts", json=ROUND).json()
    as_(client, OP2)
    assert client.get(f"/api/v1/attest/drafts/{d['draft_id']}").status_code == 404
    assert client.post(f"/api/v1/attest/drafts/{d['draft_id']}/present", json={}).status_code == 404


def test_signing_over_http_needs_a_grant(client):
    d = as_(client, OP1).post("/api/v1/attest/drafts", json=ROUND).json()
    pid = client.post(f"/api/v1/attest/drafts/{d['draft_id']}/present", json={}).json()[
        "presentation_id"
    ]
    client.post(
        f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"}
    )
    r = client.post("/api/v1/attest/sign", json={"presentation_id": pid})
    assert r.status_code == 400 and r.json()["code"] == "no_grant"


def test_a_signer_without_a_role_is_refused_with_a_reason(client):
    d = as_(client, OP2).post("/api/v1/attest/drafts", json=ROUND).json()
    pid = client.post(f"/api/v1/attest/drafts/{d['draft_id']}/present", json={}).json()[
        "presentation_id"
    ]
    client.post(
        f"/api/v1/attest/presentations/{pid}/respond", json={"answer": "sign", "via": "screen"}
    )
    g = client.post("/gate/grants", json={"presentation_id": pid}).json()["grant"]
    r = client.post("/api/v1/attest/sign", json={"presentation_id": pid, "grant": g})
    assert r.status_code == 403 and "holds no role" in r.json()["detail"]


def test_another_site_is_refused_not_widened(client):
    r = as_(client, OP1).post("/api/v1/attest/drafts", json={**ROUND, "site": "site-b"})
    assert r.status_code == 403 and r.json()["code"] == "site"
    assert client.get("/api/v1/attest/demo_log/records?site=site-b").status_code == 403


def test_ask_with_a_correction_returns_the_next_presentation(client):
    d = as_(client, OP1).post("/api/v1/attest/drafts", json=ROUND).json()
    pid = client.post(f"/api/v1/attest/drafts/{d['draft_id']}/present", json={}).json()[
        "presentation_id"
    ]
    r = client.post(
        f"/api/v1/attest/presentations/{pid}/respond",
        json={"answer": "ask", "via": "screen", "correction": {"reading": "4.3 bar"}},
    ).json()
    assert "4.3 bar" in r["next_presentation"]["forms"]["card"]


def test_stream_replays_what_was_missed_after_last_event_id(client):
    sign_round(client)
    sign_round(client)
    with client.stream(
        "GET", "/api/v1/attest/demo_log/stream?once=1", headers={"Last-Event-ID": "1"}
    ) as r:
        body = "".join(r.iter_text())
    assert "event: ready" in body
    assert "id: 2\nevent: signed" in body and "id: 1\n" not in body


def test_stream_delivers_a_record_signed_while_connected(client, node_signer):
    """Over a real server: Starlette's TestClient cannot read an open stream.
    A signature made through the service (the CLI path) reaches the browser's
    stream too, because the service publishes after its commit."""
    import httpx

    from axiom.extensions.builtins.http import ThreadedServer

    from .test_signing import _round_check, _sign

    token = issue_session_token(OP1, issuer=BASE, amr=("pwd",))
    got: list[str] = []

    def sign_later():
        time.sleep(0.5)
        _sign(_round_check(), node_signer)

    with ThreadedServer(client.app).serving() as srv:
        url = srv.base_url + "/api/v1/attest/demo_log/stream"
        t = threading.Thread(target=sign_later)
        with httpx.stream(
            "GET", url, cookies={SESSION_COOKIE: token}, timeout=httpx.Timeout(10.0)
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            t.start()
            for line in r.iter_lines():
                got.append(line)
                if line.startswith("data:") and '"seq": 1' in line:
                    break
        t.join()
    assert "id: 1" in got and "event: signed" in got


def test_a_logbook_lists_each_readings_fields_instruments_for_the_site(client):
    from axiom.extensions.builtins.attest import field_sources, registry
    from axiom.extensions.builtins.attest.field_sources import Instrument
    from axiom.extensions.builtins.attest.logbooks import parse_logbook

    field_sources.register("instruments", lambda site: [Instrument("power", "Power", "kW")])
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "check_log", "version": "1"},
                "type": [{"id": "CHECK", "meanings": ["performed"], "roles": ["operator"],
                          "fields": [{"id": "readings", "type": "readings", "observe": True,
                                      "from_site": "instruments"}]}],
            },
            source="test:api-checks",
        )
    )  # fmt: skip
    try:
        b = as_(client, OP1).get("/api/v1/attest/logbooks/check_log").json()
    finally:
        field_sources.unregister("instruments")
    f = b["types"][0]["fields"][0]
    assert f["instruments"] == [{"id": "power", "label": "Power", "unit": "kW"}]
    assert f["observe"] is True
