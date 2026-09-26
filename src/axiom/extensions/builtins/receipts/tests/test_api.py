# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/api/v1/receipts/today — the web projection of the ONE composer:
read-only (never consumes trust deltas), credential-scoped, plain-name
vocabulary on the wire."""

from __future__ import annotations

import contextlib

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
from axiom.extensions.builtins.receipts import api  # noqa: E402
from axiom.extensions.builtins.receipts import store as rcpt_store  # noqa: E402
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase  # noqa: E402
from axiom.extensions.builtins.receipts.db_models import BriefSnapshot  # noqa: E402

CADENCES = {"heartbeat": 900, "service_health": 900}


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
def client(monkeypatch):
    fprov, fe = _mem_provider(FleetBase)
    rprov, re_ = _mem_provider(RcptBase)
    fleet_store.set_provider(fprov)
    rcpt_store.set_provider(rprov)
    fleet_api.set_session_resolver_factory(lambda: None)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)

    router = fastapi.APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app = fastapi.FastAPI()
    app.include_router(router)
    yield TestClient(app)
    fleet_store.reset_provider()
    rcpt_store.reset_provider()
    fleet_api.reset_session_resolver_factory()
    fe.dispose()
    re_.dispose()


def _seed(node="node-a", *, site="site-a", healthy=True, ago_seconds: int = 0):
    """``ago_seconds`` backdates the whole batch, which is how a node goes
    quiet: nothing arrives, so the newest report simply gets old."""
    from datetime import UTC, datetime, timedelta

    when = datetime.now(UTC) - timedelta(seconds=ago_seconds) if ago_seconds else None
    with fleet_store.session_scope() as s:
        ingest_reports(
            s,
            site=site,
            node_id=node,
            reporter_principal=f"@{node}:{site}",
            now=when,
            cadences=CADENCES,
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


def test_today_serves_the_composed_brief(client):
    _seed()
    body = client.get("/api/v1/receipts/today").json()
    brief = body["brief"]
    assert brief["quiet_line"]
    assert brief["counts"]["green"] >= 1
    assert isinstance(brief["needs_you"], list)
    # Vocabulary rule: internal source ids never reach the wire.
    assert "decision roles" in brief["not_yet_wired"]
    assert "seat" not in brief["not_yet_wired"]


def test_web_reads_never_consume_the_delta_baseline(client):
    _seed()
    # Two consecutive web reads: identical apart from the timestamp, and
    # no snapshot rows appear — browsing must not advance the baseline.
    a = client.get("/api/v1/receipts/today").json()["brief"]
    b = client.get("/api/v1/receipts/today").json()["brief"]
    a.pop("generated_at"), b.pop("generated_at")
    assert a == b
    with rcpt_store.session_scope() as s:
        assert s.query(BriefSnapshot).count() == 0


def test_unhealthy_service_needs_you_on_the_wire(client):
    _seed(healthy=False)
    brief = client.get("/api/v1/receipts/today").json()["brief"]
    kinds = {(i["claim_kind"], i["status"]) for i in brief["needs_you"]}
    assert ("service_health", "failed") in kinds
    failed = next(i for i in brief["needs_you"] if i["status"] == "failed")
    assert failed["evidence"]  # receipts face-up, verbatim


def test_out_of_scope_bearer_site_is_404(client, monkeypatch):
    _seed()
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a")

    class _Id:
        principal = "@x"
        site = "site-b"

    fleet_api.set_key_store_factory(lambda: type("KS", (), {"resolve": lambda self, t: _Id()})())
    try:
        r = client.get("/api/v1/receipts/today", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 404
    finally:
        fleet_api.reset_key_store_factory()


def test_case_detail_round_trip_and_404(client):
    _seed(healthy=False)
    brief = client.get("/api/v1/receipts/today").json()["brief"]
    assert brief["counts"]["cases"] >= 1
    case = brief["cases"][0]
    got = client.get(f"/api/v1/receipts/case/{case['case_id']}").json()["case"]
    assert got["case_id"] == case["case_id"]
    assert got["items"][0]["evidence"]
    assert client.get("/api/v1/receipts/case/c-00000000").status_code == 404


def _principal(monkeypatch, principal="ben@local", site="site-a"):
    """Resolve a session the way the gate would."""

    class _Cred:
        claims = {"sub": principal, "site": site}

    fleet_api.set_session_resolver_factory(lambda: lambda request: _Cred())


def test_deciding_records_the_session_principal_not_the_body(client, monkeypatch):
    _seed(healthy=False)
    brief = client.get("/api/v1/receipts/today").json()["brief"]
    case_id = brief["cases"][0]["case_id"]

    # Unauthenticated: a decision has nobody to attribute it to.
    assert (
        client.post(f"/api/v1/receipts/case/{case_id}/decide", json={"chosen": "hold"}).status_code
        == 401
    )

    _principal(monkeypatch)
    # Even if a caller tries to name someone else, the body has no such
    # field — the decider comes from the session.
    r = client.post(
        f"/api/v1/receipts/case/{case_id}/decide",
        json={"chosen": "hold", "note": "checking the artifact", "decider": "@someone-else"},
    )
    assert r.status_code == 200
    verdict = r.json()["verdict"]
    # The session's email subject is ben@local; the handle is @ben:site-a,
    # not @ben@local:site-a. ADR-020 is single-@ and this string is written
    # into an append-only record — an unparseable decider is permanent.
    assert verdict["decider"] == "@ben:site-a"
    assert verdict["chosen"] == "hold"
    assert verdict["outcome"] is None  # arrives later, by observation

    # The decision now rides the case, so the docket shows it was decided.
    got = client.get(f"/api/v1/receipts/case/{case_id}").json()["case"]
    assert got["verdict"]["chosen"] == "hold"
    assert got["verdict"]["note"] == "checking the artifact"


def test_an_unknown_choice_is_refused(client, monkeypatch):
    _seed(healthy=False)
    case_id = client.get("/api/v1/receipts/today").json()["brief"]["cases"][0]["case_id"]
    _principal(monkeypatch)
    r = client.post(f"/api/v1/receipts/case/{case_id}/decide", json={"chosen": "nuke"})
    assert r.status_code == 422


def test_deciding_about_a_case_that_is_gone_is_refused(client, monkeypatch):
    _seed(healthy=False)
    _principal(monkeypatch)
    r = client.post("/api/v1/receipts/case/c-00000000/decide", json={"chosen": "hold"})
    assert r.status_code == 404


class TestDeciderIsAlwaysAResolvableHandle:
    """A decision's decider is written into an append-only record, so a
    handle that nothing can parse is permanent. ADR-020 is single-@."""

    def _principal(self, claims):
        from unittest.mock import patch

        from axiom.extensions.builtins.receipts.api import _session_principal

        credential = type("C", (), {"claims": claims})()
        with patch(
            "axiom.extensions.builtins.fleet.api._session_resolver_factory",
            lambda: lambda _request: credential,
        ):
            return _session_principal(object())

    def test_an_email_subject_never_becomes_a_double_at_handle(self):
        handle = self._principal({"email": "ben@local"})
        assert handle == "@ben"
        assert handle.count("@") == 1

    def test_the_site_claim_becomes_the_context_half(self):
        assert (
            self._principal({"sub": "a1b2c3@idp.example.edu", "site": "org-a"}) == "@a1b2c3:org-a"
        )

    def test_a_subject_too_malformed_to_name_is_no_decider(self):
        assert self._principal({"sub": "a@b@c"}) is None
        assert self._principal({"sub": ""}) is None


def test_the_decision_records_who_they_were_called(client, monkeypatch):
    """A gate session keyed on a provider GUID must still produce a page
    a person can read. The handle stays the identity; the label is what
    the surface shows."""
    _seed(healthy=False)
    case_id = client.get("/api/v1/receipts/today").json()["brief"]["cases"][0]["case_id"]

    class _Cred:
        claims = {
            "sub": "78f6cda0-4068-4c73-b8f7-53210aee4379",
            "name": "Lee, Robin",
            "email": "rlee@example.edu",
            "site": "site-a",
        }

    fleet_api.set_session_resolver_factory(lambda: lambda request: _Cred())
    verdict = client.post(
        f"/api/v1/receipts/case/{case_id}/decide", json={"chosen": "acknowledge"}
    ).json()["verdict"]
    assert verdict["decider"] == "@78f6cda0-4068-4c73-b8f7-53210aee4379:site-a"
    assert verdict["decider_label"] == "Lee, Robin"


def test_no_display_name_falls_back_to_the_address_local_part_not_the_subject(client, monkeypatch):
    _seed(healthy=False)
    case_id = client.get("/api/v1/receipts/today").json()["brief"]["cases"][0]["case_id"]

    class _Cred:
        claims = {"sub": "78f6cda0-4068", "email": "rlee@example.edu", "site": "site-a"}

    fleet_api.set_session_resolver_factory(lambda: lambda request: _Cred())
    verdict = client.post(
        f"/api/v1/receipts/case/{case_id}/decide", json={"chosen": "hold"}
    ).json()["verdict"]
    assert verdict["decider_label"] == "rlee"


def test_deciding_to_fix_actually_runs_the_fix(client, monkeypatch):
    """The slice that makes this a supervised operator rather than a
    record of judgement: the decision carries the fix out, through the
    platform's own gateway, and the record says what ran."""
    _seed(node="this-node", ago_seconds=9_000)
    monkeypatch.setenv("AXIOM_FLEET_NODE_ID", "this-node")
    brief = client.get("/api/v1/receipts/today").json()["brief"]
    case_id = next(c["case_id"] for c in brief["cases"] if c["entity_id"] == "this-node")

    # The case about OURSELVES knows it can be fixed here, and offers it.
    detail = client.get(f"/api/v1/receipts/case/{case_id}").json()["case"]
    assert detail["handling"]["can_run"] is True
    assert detail["handling"]["needs_person"] is False
    assert detail["handling"]["fix_summary"]
    offered = {o["choice"]: o for o in detail["options"]}
    assert "fix" in offered
    # The button says the method, in the fix's own words.
    assert offered["fix"]["detail"] == "make this node report now"
    assert offered["discuss"]["records"] is False

    _principal(monkeypatch, principal="ben@local", site="site-a")
    ran: list[tuple[str, dict]] = []

    def _fake_report(params, ctx):
        from axiom.infra.skills import SkillResult

        ran.append(("fleet.report", dict(params)))
        # "sent" is the remedy's proves_done key: a probe-shaped skill can
        # exit ok having deliberately done nothing, and that is not a fix.
        return SkillResult(
            ok=True, value={"enrolled": True, "sent": 2}, actions_taken=["pushed a report"]
        )

    import axiom.extensions.builtins.receipts.skills as rskills

    real_bind = rskills.bind_default

    def _bind_with_fake():
        registry = real_bind()
        registry._skills["fleet.report"] = _fake_report  # noqa: SLF001 — test double
        return registry

    monkeypatch.setattr(rskills, "bind_default", _bind_with_fake)

    verdict = client.post(f"/api/v1/receipts/case/{case_id}/decide", json={"chosen": "fix"}).json()[
        "verdict"
    ]

    assert ran and ran[0][0] == "fleet.report", "the declared fix was not carried out"
    assert verdict["chosen"] == "fix"
    assert verdict["ran_capability"] == "fleet.report"
    assert verdict["ran_ok"] is True
    assert "pushed a report" in verdict["ran_detail"]


def test_a_case_about_another_node_is_never_offered_a_fix_we_cannot_run(client, monkeypatch):
    """fleet.report reports about the node it runs on. Offering it for a
    DIFFERENT silent node would be a button that lies."""
    _seed(node="somebody-else", ago_seconds=9_000)
    monkeypatch.setenv("AXIOM_FLEET_NODE_ID", "this-node")
    brief = client.get("/api/v1/receipts/today").json()["brief"]
    case_id = brief["cases"][0]["case_id"]
    detail = client.get(f"/api/v1/receipts/case/{case_id}").json()["case"]
    assert detail["handling"]["can_run"] is False
    assert detail["handling"]["reason"] == "not_ours"
    assert "somebody-else" in detail["handling"]["because"]
    assert "fix" not in {o["choice"] for o in detail["options"]}


def test_a_fix_that_exits_ok_having_done_nothing_is_recorded_as_not_done(client, monkeypatch):
    """fleet.report returns ok with {"enrolled": False} on a node that
    has not opted in, because a probe never warns. The case must not read
    that as handled."""
    _seed(node="this-node", ago_seconds=9_000)
    monkeypatch.setenv("AXIOM_FLEET_NODE_ID", "this-node")
    case_id = client.get("/api/v1/receipts/today").json()["brief"]["cases"][0]["case_id"]
    _principal(monkeypatch)

    def _no_op(params, ctx):
        from axiom.infra.skills import SkillResult

        return SkillResult(
            ok=True,
            value={"enrolled": False},
            actions_taken=["fleet push not configured; node is not enrolled — nothing sent"],
        )

    import axiom.extensions.builtins.receipts.skills as rskills

    real_bind = rskills.bind_default

    def _bind_with_noop():
        registry = real_bind()
        registry._skills["fleet.report"] = _no_op  # noqa: SLF001 — test double
        return registry

    monkeypatch.setattr(rskills, "bind_default", _bind_with_noop)

    verdict = client.post(f"/api/v1/receipts/case/{case_id}/decide", json={"chosen": "fix"}).json()[
        "verdict"
    ]
    assert verdict["ran_ok"] is False, "a no-op must not be recorded as a successful fix"
    assert "not enrolled" in verdict["ran_detail"]

    detail = client.get(f"/api/v1/receipts/case/{case_id}").json()["case"]
    assert detail["handling"]["reason"] == "did_not_work"
    assert "not enrolled" in detail["handling"]["because"]
    assert detail["handling"]["can_run"] is False
