# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A bound reader is metered as itself, by the key it presented, not as an anonymous caller.

Chat bound to a node counted every data read, from any site-bound key, as an anonymous caller,
so every bound reader shared one bucket (0.2 reads a second, burst 3, one at a time) and the
third read in a turn was refused. The bound install admitted each forwarded read at its own
gold door under its local, unverified identity and no surface (the anonymous class) before
asking the node, which then metered the read again under the verified key. Now a forwarded read
is metered once, by the node, keyed on the verified principal and the key it presented.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform import read_api, read_target
from axiom.extensions.builtins.data_platform.skills import gold
from axiom.infra import serving_guard as sg
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult


def _ctx(*, handle: str | None, credential_id: str | None = None, surface: str | None = "web"):
    principal = (PrincipalContext(handle=handle, posture="attested", assured=True,
                                  credential_id=credential_id)
                 if handle else PrincipalContext(handle="@anonymous"))
    return SkillContext(registry=SkillRegistry(), state_dir=Path("."),
                        logger=logging.getLogger("test"), principal=principal, surface=surface)


class _Conn:
    def set_session(self, **kw):
        pass

    def cursor(self):
        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Cur()

    def close(self):
        pass


@pytest.fixture
def node(monkeypatch, tmp_path):
    """This install holds the medallion; the database is stubbed; a fresh guard."""
    monkeypatch.setattr(gold, "_GUARD", None)
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(read_target, "resolve_read_target", lambda: read_target.ReadTarget("medallion"))
    import axiom.extensions.builtins.data_platform._dsn as dsn_mod

    monkeypatch.setattr(dsn_mod, "resolve_dsn", lambda: "dbname=stub")
    fake_pg = types.ModuleType("psycopg2")
    fake_pg.connect = lambda dsn, **kw: _Conn()
    monkeypatch.setitem(sys.modules, "psycopg2", fake_pg)
    monkeypatch.setattr(gold.gq, "list_tables", lambda cur: [{"table": "t"}])


def _read(ctx) -> SkillResult:
    return gold.tables({}, ctx)


class TestTheDoorPassesTheVerifiedCaller:
    def _client(self, captured: dict, *, principal: str | None, credential: str | None) -> TestClient:
        app = FastAPI()

        @app.middleware("http")
        async def _gate(request: Request, call_next):  # stands in for the authz middleware
            if principal:
                request.state.principal = principal
                request.state.credential_id = credential
            return await call_next(request)

        app.include_router(read_api.build_data_read_router())
        return TestClient(app)

    def _capture(self, monkeypatch) -> dict:
        captured: dict = {}
        monkeypatch.setattr(read_api, "holds_medallion", lambda: True)

        def fake_tables(params, ctx):
            captured["ctx"] = ctx
            return SkillResult(ok=True, value=[])

        monkeypatch.setattr(read_api.gold, "tables", fake_tables)
        return captured

    def test_the_principal_and_its_key_reach_the_skill(self, monkeypatch):
        captured = self._capture(monkeypatch)
        client = self._client(captured, principal="@reader:site-a", credential="key-1")
        assert client.post("/data/query", json={"verb": "data.tables"}).status_code == 200
        ctx = captured["ctx"]
        assert ctx.principal.handle == "@reader:site-a"
        assert ctx.principal.credential_id == "key-1"
        assert sg.caller_class(surface=ctx.surface, assured=True) == "interactive"
        assert gold.meter_key(ctx.principal) == "@reader:site-a#key-1"

    def test_no_verified_caller_is_anonymous(self, monkeypatch):
        captured = self._capture(monkeypatch)
        client = self._client(captured, principal=None, credential=None)
        client.post("/data/query", json={"verb": "data.tables"})
        ctx = captured["ctx"]
        assert gold.meter_key(ctx.principal) == "@anonymous"
        assert sg.caller_class(surface=ctx.surface, assured=False) == "anonymous"


class TestEachKeyHasItsOwnBucket:
    def test_two_keys_get_separate_buckets(self, node):
        a = _ctx(handle="@reader:site-a", credential_id="key-a")
        b = _ctx(handle="@reader:site-a", credential_id="key-b")
        burst = int(sg.ServingPolicy.shipped_defaults().limits("interactive").burst)
        for _ in range(burst):
            assert _read(a).ok
        assert not _read(a).ok, "key a spent its burst"
        assert _read(b).ok, "key b has a bucket of its own"

    def test_two_sites_keys_get_separate_buckets(self, node):
        burst = int(sg.ServingPolicy.shipped_defaults().limits("interactive").burst)
        for _ in range(burst):
            assert _read(_ctx(handle="@reader:site-a", credential_id="k1")).ok
        assert _read(_ctx(handle="@reader:site-b", credential_id="k2")).ok

    def test_a_keys_reads_are_not_counted_as_anonymous(self, node):
        for _ in range(5):
            assert _read(_ctx(handle="@reader:site-a", credential_id="key-a")).ok
        metered = gold._guard().metering()
        assert "@anonymous" not in metered
        assert metered["@reader:site-a#key-a"]["admitted"] == 5

    def test_suspending_a_principal_suspends_each_of_its_keys(self, node, monkeypatch):
        guard = gold._guard()
        monkeypatch.setattr(guard, "policy", guard.policy.replace(
            suspended_principals=frozenset({"@reader:site-a"})))
        r = _read(_ctx(handle="@reader:site-a", credential_id="key-a"))
        assert not r.ok and "suspended" in r.errors[0]


class TestAnonymousStaysLimited:
    def test_anonymous_has_its_own_small_bucket(self, node):
        anon = _ctx(handle=None, surface=None)
        burst = int(sg.ServingPolicy.shipped_defaults().limits("anonymous").burst)
        for _ in range(burst):
            assert _read(anon).ok
        refused = _read(anon)
        assert not refused.ok and "anonymous" in refused.errors[0]
        assert _read(_ctx(handle="@reader:site-a", credential_id="key-a")).ok, (
            "a verified key does not share the anonymous bucket"
        )


class TestABoundInstallLeavesMeteringToTheNode:
    def test_forwarded_reads_are_not_admitted_locally(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gold, "_GUARD", None)
        monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(read_target, "resolve_read_target",
                            lambda: read_target.ReadTarget("binding", site="site-a", face_url="https://node"))
        asked: list[str] = []

        def fake_ask(target, *, verb, params):
            asked.append(verb)
            return SkillResult(ok=True, value=[])

        monkeypatch.setattr(read_target, "ask", fake_ask)
        chat = _ctx(handle=None, surface=None)  # how a chat tool calls the skill: no surface
        for _ in range(10):
            assert _read(chat).ok
        assert len(asked) == 10, "every read reached the node"
        assert gold._guard().metering() == {}, "none was charged to a local, anonymous bucket"
