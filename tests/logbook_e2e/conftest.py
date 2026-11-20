# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The world the logging end-to-end suite runs in (#1818; plan
``docs/working/logging-e2e-acceptance-plan.md``).

One fictional, domain-free log (``logbooks/e2e_log.toml``, handle ``desk``) at two sites on one
node, four people and an agent, three instruments, a seeded session number, an enrolled desk
device with a location secret, recorders for every notification, and the action audit chain in a
temporary state directory.

**One deliberate change from the plan.** The plan builds one world per session and runs the
stages in file order. CI runs the suite under xdist with testmon selection, so neither the order
nor the set of tests in a run is guaranteed. Each test therefore builds its own world on its own
throwaway schema (``attest_db`` rides the root ``real_postgres`` fixture, as
``tests/logbook_acceptance/`` does, so nothing touches a developer's database and CI fails
rather than skips without a server), and reaches the state an earlier stage leaves through the
helpers below instead of through shared state. The stages still read top to bottom as one story.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.attest.tests.conftest import (
    attest_db as _attest_schema,  # noqa: F401
)
from axiom.extensions.builtins.attest.tests.conftest import node_signer  # noqa: F401

HERE = Path(__file__).parent
E2E_LOG = HERE / "logbooks" / "e2e_log.toml"
LOG = "e2e_log"
SITE = "example-site"
OTHER = "other-site"
ROOM = "desk_room"
SECRET = b"e2e-desk-room-secret"
SEED = 5000
BASE = "http://gate.example"


def pytest_collection_modifyitems(config, items):
    """Every test here carries the ``logbook_e2e`` marker, so the suite can be run or left out
    by name (``-m logbook_e2e``)."""
    for item in items:
        if "/tests/logbook_e2e/" in str(item.path).replace("\\", "/"):
            item.add_marker(pytest.mark.logbook_e2e)


@dataclass(frozen=True)
class Person:
    user_id: str
    site: str
    roles: tuple[str, ...]
    name: str

    @property
    def handle(self) -> str:
        return f"@{self.user_id}:{self.site}"


ANN = Person("ann", SITE, ("member",), "Ann Example")
BO = Person("bo", SITE, ("lead",), "Bo Example")
CY = Person("cy", OTHER, ("member",), "Cy Example")
DEE = Person("dee", SITE, (), "Dee Example")


@dataclass
class World:
    state_dir: Path
    signer: Any
    sent: list[tuple[str, str]] = field(default_factory=list)
    cosign_sent: list[tuple[str, dict]] = field(default_factory=list)
    clock: dict[str, datetime] = field(default_factory=dict)


@pytest.fixture
def attest_db(real_postgres, _attest_schema):  # noqa: F811
    """A private attest schema at migration head, in this run's throwaway database."""
    return _attest_schema


def _instruments(site: str):
    from axiom.extensions.builtins.attest.field_sources import Instrument

    if site != SITE:
        return []
    return [
        Instrument("flow", "Flow", "L/min", {"kind": "magnitude", "value": "0.5"}),
        Instrument("level", "Level", "cm", {"kind": "magnitude", "value": "1"}),
        Instrument("temp", "Temperature", "degC", {"kind": "magnitude", "value": "0.2"}),
    ]


@pytest.fixture
def world(attest_db, node_signer, tmp_path, monkeypatch):  # noqa: F811
    from axiom.extensions.builtins.attest import (
        api,
        cosign,
        devices,
        field_sources,
        intervals,
        obligations,
        presence,
        registry,
        roles,
        service,
        signing,
    )

    registry.register_file(E2E_LOG)
    signing.set_provider(lambda: node_signer)
    for p in (ANN, BO, CY):
        for r in p.roles:
            roles.grant(p.handle, p.site, r, by="@admin:example-site", state_dir=tmp_path)
    field_sources.register("instruments", _instruments)
    intervals.register_seed(LOG, "session", lambda site: SEED)
    presence.set_secret_provider(lambda site, loc: SECRET if (site, loc) == (SITE, ROOM) else None)
    devices.enroll(site_id=SITE, device_id="desk-1", device_class="kiosk", location=ROOM,
                   mobility="fixed", by="@admin:example-site")  # fmt: skip
    devices.enroll(site_id=SITE, device_id="tablet-1", device_class="tablet", location=ROOM,
                   mobility="portable", by="@admin:example-site")  # fmt: skip
    w = World(state_dir=tmp_path, signer=node_signer)
    obligations.set_sender(lambda recipient, state: w.sent.append((recipient, state["state"])))
    cosign.set_sender(lambda recipient, info: w.cosign_sent.append((recipient, info)))
    monkeypatch.setattr(cosign, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(api, "_state_dir", lambda: tmp_path)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    w.clock["t"] = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(service, "_now", lambda: w.clock["t"])
    try:
        yield w
    finally:
        obligations.reset_sender()
        cosign.reset_sender()
        presence.reset_secret_provider()
        field_sources.unregister("instruments")
        signing.reset_provider()


# -- the person and the signing paths -------------------------------------------------------


def signatory(p: Person, posture: str = "sso"):
    from axiom.extensions.builtins.attest.service import Signatory
    from axiom.infra.principal import PrincipalContext

    return Signatory(principal=PrincipalContext(handle=p.handle, posture=posture, assured=True,
                                                idp="entra"),
                     roles=p.roles, display=p.name)  # fmt: skip


def auth(p: Person, minutes_ago: float = 1):
    from axiom.extensions.builtins.attest.grants import Authentication
    from axiom.infra.principal import PrincipalContext

    return Authentication(
        principal=PrincipalContext(handle=p.handle, posture="sso", assured=True, idp="entra"),
        auth_time=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        amr=("pwd",),
    )


def draft(p: Person, entry_type: str, fields: dict[str, Any] | None = None, *, meaning=None,
          title=None, site=SITE, origin="human") -> str:  # fmt: skip
    from axiom.extensions.builtins.attest import registry, service

    et = registry.get(LOG).type(entry_type)
    return service.create_draft(
        site_id=site, logbook=LOG, entry_type=entry_type, meaning=meaning or et.meanings[0],
        content={"title": title or entry_type.title(), "fields": fields or {}}, origin=origin,
        for_principal=p.handle,
    )  # fmt: skip


def sign_cli(p: Person, entry_type: str, fields=None, **kw) -> dict:
    """Present, answer and sign with no grant: a type that needs no fresh sign-in or presence."""
    from axiom.extensions.builtins.attest import service

    d = draft(p, entry_type, fields, **kw)
    pres = service.present(d)
    service.respond(
        pres.presentation_id, principal=signatory(p).principal, answer="sign", via="cli"
    )
    return service.sign(pres.presentation_id, signatory=signatory(p), signer=_signer()).record


def sign_web(p: Person, draft_id: str, *, device: str = "desk-1", code: str | None = None,
             minutes_ago: float = 1) -> dict:  # fmt: skip
    """The browser path through the library: present on screen, answer, mint a grant on a device,
    sign with it (ADR-146)."""
    from axiom.extensions.builtins.attest import grants, service

    pres = service.present(draft_id, modality="screen")
    service.respond(
        pres.presentation_id, principal=signatory(p).principal, answer="sign", via="screen"
    )
    token = grants.mint(pres.presentation_id, auth=auth(p, minutes_ago),
                        device=grants.Device(device_id=device, device_class="personal"),
                        presence_code=code, signer=_signer())  # fmt: skip
    return service.sign(pres.presentation_id, signatory=signatory(p), signer=_signer(),
                        source="screen", grant=token, grant_keys=keys()).record  # fmt: skip


def _signer():
    from axiom.extensions.builtins.attest import signing

    return signing.signer()


def keys() -> dict[str, bytes]:
    from axiom.extensions.builtins.attest import signing

    return signing.public_keys()


def readings(flow="12.5", level="80", temp="21.3") -> dict:
    return {"readings": {"flow": flow, "level": level, "temp": temp}}


def open_session(world: World) -> dict:
    return sign_cli(ANN, "OPEN", {"text": "session opened"})


def check(world: World, **values) -> dict:
    from axiom.extensions.builtins.attest import presence

    code = presence.code(SECRET, ROOM, datetime.now(UTC))
    return sign_web(ANN, draft(ANN, "CHECK", readings(**values)), device="tablet-1", code=code)


# -- the browser and the stream -------------------------------------------------------------


def user(p: Person):
    from axiom.webauth.users import User

    return User(user_id=p.user_id, email=f"{p.user_id}@example.org", name=p.name, roles=("user",),
                site=p.site, attributes={"idp": "entra"})  # fmt: skip


@pytest.fixture
def web(world):
    """A test client for the attest and gate routes; ``as_(person)`` signs a person in."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.attest import api
    from axiom.extensions.builtins.fleet import api as fleet_api
    from axiom.extensions.builtins.http.authz_hook import build_session_resolver
    from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
    from axiom.webauth.keys import reset_key_store_for_tests
    from axiom.webauth.users import InMemoryUserStore

    reset_key_store_for_tests()
    fleet_api.set_session_resolver_factory(lambda: build_session_resolver(issuer=BASE))
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app.include_router(router)
    app.include_router(build_webgate_router(InMemoryUserStore(), secure_cookies=False))
    c = TestClient(app, base_url=BASE, follow_redirects=False)
    yield c
    fleet_api.reset_session_resolver_factory()
    reset_key_store_for_tests()


def as_(client, p: Person | None, *, auth_age: int = 0):
    from axiom.webauth import SESSION_COOKIE
    from axiom.webauth.session import issue_session_token

    client.cookies.clear()
    if p is not None:
        token = issue_session_token(user(p), issuer=BASE, amr=("pwd",),
                                    auth_time=int(time.time()) - auth_age)  # fmt: skip
        client.cookies.set(SESSION_COOKIE, token)
    return client


def access_records(state_dir: Path) -> list[dict]:
    from axiom.infra import audit_trail

    return [r for r in audit_trail.read_chain(audit_trail.audit_path(state_dir)) if r.get("access")]


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# -- software: an agent with a key scoped to propose ----------------------------------------

TOKEN = "axk_e2e_helper"
HELPER = "@helper:example-site"


@pytest.fixture
def helper(web, monkeypatch):
    """``@helper`` on the roster, with a Bearer key scoped ``attest.propose`` at the site."""
    from axiom.extensions.builtins.attest import proposals
    from axiom.extensions.builtins.fleet import api as fleet_api
    from axiom.webauth.api_keys import ApiKeyIdentity

    monkeypatch.setattr(proposals, "_agent_names", lambda: {"helper"})
    identity = {
        "key": ApiKeyIdentity(key_id="k1", principal=HELPER, scopes=("attest.propose",), site=SITE)
    }

    class Store:
        def resolve(self, token):
            return identity["key"] if token == TOKEN else None

    fleet_api.set_key_store_factory(lambda: Store())
    yield identity
    fleet_api.reset_key_store_factory()


def as_helper(client):
    client.cookies.clear()
    return {"Authorization": f"Bearer {TOKEN}"}


# -- the journal ------------------------------------------------------------------------------

from tests.logbook_acceptance.test_j1 import bus, journal_db, live  # noqa: E402,F401

# -- the journal's routes, served, for the stream ---------------------------------------------


@pytest.fixture
def journal_app(world, live, monkeypatch):  # noqa: F811
    """The attest and journal routes in one app, as a node mounts them, for a real HTTP stream."""
    fastapi = pytest.importorskip("fastapi")

    from axiom.extensions.builtins.attest import api as attest_api
    from axiom.extensions.builtins.fleet import api as fleet_api
    from axiom.extensions.builtins.http.authz_hook import build_session_resolver
    from axiom.extensions.builtins.journal import api as journal_api
    from axiom.extensions.builtins.journal import follow
    from axiom.webauth.keys import reset_key_store_for_tests

    reset_key_store_for_tests()
    fleet_api.set_session_resolver_factory(lambda: build_session_resolver(issuer=BASE))
    monkeypatch.setenv(journal_api.INGEST_ENV, "0")
    monkeypatch.setattr(journal_api, "_state_dir", lambda: world.state_dir)
    monkeypatch.setattr(follow, "_state_dir", lambda: world.state_dir)
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    attest_api.register_routes(router)
    journal_api.register_routes(router)
    app.include_router(router)
    yield app
    fleet_api.reset_session_resolver_factory()
    reset_key_store_for_tests()


def stream_frames(
    app, p: Person, url: str, *, until, seconds: float = 6.0
) -> tuple[int, list[list[str]]]:
    """Open ``url`` as ``p`` on a real server and read server-sent frames until ``until(frames)``."""
    import httpx

    from axiom.extensions.builtins.http import ThreadedServer
    from axiom.webauth import SESSION_COOKIE
    from axiom.webauth.session import issue_session_token

    cookies = {SESSION_COOKIE: issue_session_token(user(p), issuer=BASE, amr=("pwd",))} if p else {}
    lines: list[str] = []
    frames: list[list[str]] = []
    with ThreadedServer(app).serving() as srv:
        with httpx.stream(
            "GET", srv.base_url + url, cookies=cookies, timeout=httpx.Timeout(10.0)
        ) as r:
            if r.status_code != 200:
                return r.status_code, []
            deadline = time.monotonic() + seconds
            for line in r.iter_lines():
                lines.append(line)
                frames = _frames(lines)
                if until(frames) or time.monotonic() > deadline:
                    break
    return 200, frames


def _frames(lines: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line == "":
            if current:
                out.append(current)
            current = []
        else:
            current.append(line)
    return out


def rows_of(frames) -> list[dict]:
    import json

    return [json.loads(f[-1][len("data: ") :]) for f in frames if "event: row" in f]
