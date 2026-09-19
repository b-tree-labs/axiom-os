# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenFgaHttpClient — the live adapter, driven against a fake OpenFGA server.

The fake speaks the three endpoints the client uses (check / read / write) plus
the bootstrap ones, keeps tuples in memory, evaluates ``group#member`` usersets
and the starter model's owner ⊂ editor ⊂ viewer hierarchy, and records every
request so the wire shape is asserted, not assumed: bearer header, consistency,
model pinning, ≤100-tuple write chunks with deletes first, read pagination.
The same client then satisfies GUARD's ``FgaCheckClient`` and the reconciler's
``TupleStore`` in one object — the two ports ADR-083 / ADR-103 left open.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.authz.decide import DecideContext
from axiom.extensions.builtins.authz.openfga import FgaCheckClient, OpenFgaSubstrate
from axiom.extensions.builtins.authz.openfga_http import (
    WRITE_CHUNK,
    OpenFgaConfig,
    OpenFgaError,
    OpenFgaHttpClient,
    create_store,
    maybe_register_substrate,
    starter_type_definitions,
    substrate_from_config,
)
from axiom.extensions.builtins.authz.substrate import NullSubstrate, SubstrateDecision
from axiom.extensions.builtins.directory.protocol import GroupRef
from axiom.extensions.builtins.directory.reconcile import (
    MembershipReconciler,
    RelationTuple,
    TupleStore,
)

STORE = "01STORE"
MODEL = "01MODEL"


class FakeOpenFga:
    """An in-memory OpenFGA over the HTTP contract the client relies on."""

    def __init__(self, *, page_size: int | None = None, fail: Exception | None = None) -> None:
        self.tuples: set[tuple[str, str, str]] = set()
        self.calls: list[tuple[str, str, dict | None]] = []
        self.page_size = page_size
        self.fail = fail
        self.stores: dict[str, str] = {}
        self.models: dict[str, list] = {}

    # -- the transport signature
    def __call__(self, method: str, path: str, body: dict | None) -> dict:
        self.calls.append((method, path, dict(body) if body else None))
        if self.fail:
            raise self.fail
        if method == "GET" and path == "/healthz":
            return {"status": "SERVING"}
        if method == "POST" and path == "/stores":
            sid = f"01S{len(self.stores) + 1}"
            self.stores[sid] = body["name"]
            return {"id": sid, "name": body["name"]}
        if method == "GET" and path == "/stores":
            return {"stores": [{"id": k, "name": v} for k, v in self.stores.items()]}
        if path.endswith("/authorization-models"):
            self.models[MODEL] = body["type_definitions"]
            return {"authorization_model_id": MODEL}
        if path.endswith("/check"):
            k = body["tuple_key"]
            ctx = {
                (t["user"], t["relation"], t["object"])
                for t in (body.get("contextual_tuples") or {}).get("tuple_keys", [])
            }
            return {"allowed": self._holds(k["user"], k["relation"], k["object"], ctx)}
        if path.endswith("/read"):
            return self._read(body)
        if path.endswith("/write"):
            return self._write(body)
        raise OpenFgaError(f"fake: unknown {method} {path}", status=404)

    # -- semantics
    _IMPLIED = {"editor": ("owner",), "viewer": ("editor",)}

    def _holds(self, user, relation, obj, ctx) -> bool:
        universe = self.tuples | ctx
        for u, r, o in universe:
            if o != obj or r != relation:
                continue
            if u == user:
                return True
            if u.endswith("#member"):
                group = u[: -len("#member")]
                if (user, "member", group) in universe:
                    return True
        for implied in self._IMPLIED.get(relation, ()):
            if self._holds(user, implied, obj, ctx):
                return True
        return False

    def _read(self, body) -> dict:
        k = body["tuple_key"]
        matching = sorted(t for t in self.tuples if t[2] == k["object"] and t[1] == k["relation"])
        size = self.page_size or body.get("page_size") or 100
        start = int(body.get("continuation_token") or 0)
        page = matching[start : start + size]
        nxt = start + size
        return {
            "tuples": [{"key": {"user": u, "relation": r, "object": o}} for u, r, o in page],
            "continuation_token": str(nxt) if nxt < len(matching) else "",
        }

    def _write(self, body) -> dict:
        writes = (body.get("writes") or {}).get("tuple_keys", [])
        deletes = (body.get("deletes") or {}).get("tuple_keys", [])
        if len(writes) + len(deletes) > WRITE_CHUNK:
            raise OpenFgaError("fake: too many tuples in one write", status=400)
        for t in deletes:
            key = (t["user"], t["relation"], t["object"])
            if key not in self.tuples:
                raise OpenFgaError("fake: cannot delete a tuple that does not exist", status=400)
            self.tuples.discard(key)
        for t in writes:
            key = (t["user"], t["relation"], t["object"])
            if key in self.tuples:
                raise OpenFgaError("fake: tuple already exists", status=400)
            self.tuples.add(key)
        return {}


def _client(fake: FakeOpenFga, **kw) -> OpenFgaHttpClient:
    return OpenFgaHttpClient(transport=fake, store_id=STORE, **kw)


# ---------------------------------------------------------------- the two ports


def test_client_satisfies_both_ports():
    c = _client(FakeOpenFga())
    assert isinstance(c, FgaCheckClient)
    assert isinstance(c, TupleStore)


# ---------------------------------------------------------------- check


def test_check_wire_shape_and_result():
    fake = FakeOpenFga()
    fake.tuples.add(("user:alice", "viewer", "resource:doc-1"))
    c = _client(fake, model_id=MODEL)
    assert c.check(user="user:alice", relation="viewer", object="resource:doc-1") is True
    assert c.check(user="user:bob", relation="viewer", object="resource:doc-1") is False
    method, path, body = fake.calls[0]
    assert (method, path) == ("POST", f"/stores/{STORE}/check")
    assert body["tuple_key"] == {
        "user": "user:alice",
        "relation": "viewer",
        "object": "resource:doc-1",
    }
    assert body["consistency"] == "HIGHER_CONSISTENCY"
    assert body["authorization_model_id"] == MODEL
    assert "contextual_tuples" not in body


def test_check_passes_contextual_tuples_and_group_usersets_resolve():
    fake = FakeOpenFga()
    fake.tuples.add(("group:ops#member", "editor", "resource:doc-1"))
    c = _client(fake)
    # alice is not in group:ops in the store — only asserted for this decision
    ctx = (("user:alice", "member", "group:ops"),)
    assert (
        c.check(
            user="user:alice", relation="viewer", object="resource:doc-1", contextual_tuples=ctx
        )
        is True
    )  # editor implies viewer
    assert c.check(user="user:alice", relation="viewer", object="resource:doc-1") is False
    body = fake.calls[0][2]
    assert body["contextual_tuples"] == {
        "tuple_keys": [{"user": "user:alice", "relation": "member", "object": "group:ops"}]
    }


def test_check_error_propagates_as_openfga_error():
    c = _client(FakeOpenFga(fail=OpenFgaError("down", status=503)))
    with pytest.raises(OpenFgaError):
        c.check(user="user:a", relation="viewer", object="resource:x")


# ---------------------------------------------------------------- read / write (TupleStore)


def test_list_members_walks_every_page():
    fake = FakeOpenFga(page_size=3)
    for i in range(8):
        fake.tuples.add((f"user:u{i}", "member", "group:g"))
    fake.tuples.add(("user:other", "member", "group:h"))
    c = _client(fake)
    members = c.list_members("group:g", "member")
    assert sorted(members) == sorted(f"user:u{i}" for i in range(8))
    reads = [b for m, p, b in fake.calls if p.endswith("/read")]
    assert len(reads) == 3  # 3 + 3 + 2
    assert reads[0]["tuple_key"] == {"object": "group:g", "relation": "member"}
    assert "continuation_token" not in reads[0] and reads[1]["continuation_token"] == "3"


def test_write_chunks_deletes_first_and_never_sends_empty_requests():
    fake = FakeOpenFga()
    c = _client(fake, model_id=MODEL)
    c.write()  # no-op
    assert fake.calls == []
    existing = [RelationTuple(f"user:d{i}", "member", "group:g") for i in range(150)]
    for t in existing:
        fake.tuples.add((t.user, t.relation, t.object))
    adds = [RelationTuple(f"user:a{i}", "member", "group:g") for i in range(120)]
    c.write(adds=adds, deletes=existing)
    writes = [b for m, p, b in fake.calls if p.endswith("/write")]
    # 150 deletes → 2 chunks, 120 adds → 2 chunks; deletes before adds
    assert len(writes) == 4
    assert "deletes" in writes[0] and "writes" not in writes[0]
    assert "deletes" in writes[1] and "writes" not in writes[1]
    assert "writes" in writes[2] and "deletes" not in writes[2]
    assert all(
        len((b.get("writes") or b.get("deletes"))["tuple_keys"]) <= WRITE_CHUNK for b in writes
    )
    assert all(b["authorization_model_id"] == MODEL for b in writes)
    assert fake.tuples == {(t.user, t.relation, t.object) for t in adds}


def test_reconciler_runs_end_to_end_over_the_http_store():
    fake = FakeOpenFga()
    fake.tuples.update(
        {("user:stale", "member", "group:ops"), ("user:keep", "member", "group:ops")}
    )
    store = _client(fake)
    result = MembershipReconciler(store=store).reconcile(
        GroupRef(id="ops", provider="entra"), desired_subjects=["keep", "new"]
    )
    assert result.added == ("new",) and result.removed == ("stale",)
    assert fake.tuples == {
        ("user:keep", "member", "group:ops"),
        ("user:new", "member", "group:ops"),
    }
    # …and the substrate now sees the membership through a group grant
    fake.tuples.add(("group:ops#member", "viewer", "resource:dash"))
    assert store.check(user="user:new", relation="viewer", object="resource:dash") is True
    assert store.check(user="user:stale", relation="viewer", object="resource:dash") is False


# ---------------------------------------------------------------- bootstrap


def test_bootstrap_store_model_and_health():
    fake = FakeOpenFga()
    sid = create_store(fake, "axiom")
    assert sid in fake.stores
    c = OpenFgaHttpClient(transport=fake, store_id=sid)
    assert c.healthy() is True
    mid = c.write_authorization_model(starter_type_definitions())
    assert mid == MODEL and c.model_id == MODEL
    types = {t["type"] for t in fake.models[MODEL]}
    assert types == {"user", "group", "resource"}
    resource = next(t for t in fake.models[MODEL] if t["type"] == "resource")
    assert set(resource["relations"]) == {"owner", "editor", "viewer", "blocked"}


def test_healthy_is_false_when_unreachable():
    c = OpenFgaHttpClient(transport=FakeOpenFga(fail=OpenFgaError("nope")), store_id=STORE)
    assert c.healthy() is False


# ---------------------------------------------------------------- substrate + config


def test_substrate_over_http_client_allows_denies_abstains():
    from axiom.extensions.builtins.authz.tests.test_openfga_substrate import _env

    fake = FakeOpenFga()
    fake.tuples.add(("user:@alice:test", "notification_send", "slack:team-rsc/#alerts"))
    cfg = OpenFgaConfig(url="http://fga", store_id=STORE)
    sub = substrate_from_config(cfg, transport=fake)
    assert isinstance(sub, OpenFgaSubstrate)
    assert sub.check(_env()) is SubstrateDecision.ALLOW
    fake.tuples.add(("user:@alice:test", "blocked", "slack:team-rsc/#alerts"))
    assert sub.check(_env()) is SubstrateDecision.DENY
    assert (
        substrate_from_config(cfg, transport=FakeOpenFga()).check(_env())
        is SubstrateDecision.ABSTAIN
    )


def test_substrate_outage_follows_on_error_policy():
    from axiom.extensions.builtins.authz.tests.test_openfga_substrate import _env

    down = FakeOpenFga(fail=OpenFgaError("down", status=503))
    lenient = substrate_from_config(OpenFgaConfig(url="u", store_id=STORE), transport=down)
    assert lenient.check(_env()) is SubstrateDecision.ABSTAIN
    strict = substrate_from_config(
        OpenFgaConfig(url="u", store_id=STORE, on_error=SubstrateDecision.DENY), transport=down
    )
    assert strict.check(_env()) is SubstrateDecision.DENY


def test_config_from_env(tmp_path):
    assert OpenFgaConfig.from_env({}) is None
    tok = tmp_path / "fga.token"
    tok.write_text("psk-123\n")
    cfg = OpenFgaConfig.from_env(
        {
            "AXIOM_OPENFGA_URL": "http://127.0.0.1:8080/",
            "AXIOM_OPENFGA_STORE_ID": STORE,
            "AXIOM_OPENFGA_MODEL_ID": MODEL,
            "AXIOM_OPENFGA_TOKEN_FILE": str(tok),
            "AXIOM_OPENFGA_ON_ERROR": "deny",
        }
    )
    assert cfg.url == "http://127.0.0.1:8080/" and cfg.store_id == STORE
    assert cfg.model_id == MODEL and cfg.token == "psk-123"
    assert cfg.on_error is SubstrateDecision.DENY
    assert "psk-123" not in repr(cfg)  # the token never prints


@pytest.mark.parametrize(
    "env",
    [
        {"AXIOM_OPENFGA_URL": "http://x"},
        {"AXIOM_OPENFGA_STORE_ID": STORE},
        {
            "AXIOM_OPENFGA_URL": "http://x",
            "AXIOM_OPENFGA_STORE_ID": STORE,
            "AXIOM_OPENFGA_ON_ERROR": "explode",
        },
    ],
)
def test_half_configured_is_refused(env):
    with pytest.raises(ValueError):
        OpenFgaConfig.from_env(env)


def test_config_from_settings_store():
    class Settings:
        def __init__(self, d):
            self.d = d

        def get(self, key, default=None):
            return self.d.get(key, default)

    assert OpenFgaConfig.from_settings(Settings({})) is None
    cfg = OpenFgaConfig.from_settings(
        Settings(
            {
                "authz.openfga.url": "http://fga:8080",
                "authz.openfga.store_id": STORE,
                "authz.openfga.on_error": "deny",
            }
        )
    )
    assert cfg.store_id == STORE and cfg.on_error is SubstrateDecision.DENY


def test_maybe_register_substrate_is_a_noop_when_unconfigured():
    ctx = DecideContext()
    assert (
        maybe_register_substrate(
            ctx, env={}, settings=type("S", (), {"get": lambda s, k, d=None: d})()
        )
        is False
    )
    assert isinstance(ctx.substrate, NullSubstrate)


def test_maybe_register_substrate_installs_openfga():
    ctx = DecideContext()
    fake = FakeOpenFga()
    ok = maybe_register_substrate(
        ctx,
        env={"AXIOM_OPENFGA_URL": "http://fga", "AXIOM_OPENFGA_STORE_ID": STORE},
        transport=fake,
    )
    assert ok is True
    assert isinstance(ctx.substrate, OpenFgaSubstrate)
    assert fake.calls == []  # registration touches nothing; the first check does
