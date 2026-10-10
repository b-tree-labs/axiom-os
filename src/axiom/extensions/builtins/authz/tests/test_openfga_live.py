# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Opt-in live test against a real OpenFGA server.

Skipped unless ``AXIOM_OPENFGA_LIVE_URL`` is set. Point it at a throwaway
server (``docker run --rm -p 8080:8080 openfga/openfga run`` is enough) — the
test creates its own store, publishes the starter model, and cleans nothing up
on the server (a fresh store per run keeps runs independent).
"""

from __future__ import annotations

import os
import uuid

import pytest

from axiom.extensions.builtins.authz.openfga_http import (
    HttpxTransport,
    OpenFgaHttpClient,
    create_store,
    starter_type_definitions,
)
from axiom.extensions.builtins.directory.protocol import GroupRef
from axiom.extensions.builtins.directory.reconcile import MembershipReconciler, RelationTuple

LIVE = os.environ.get("AXIOM_OPENFGA_LIVE_URL", "").strip()

pytestmark = pytest.mark.skipif(not LIVE, reason="set AXIOM_OPENFGA_LIVE_URL to run")


@pytest.fixture(scope="module")
def client() -> OpenFgaHttpClient:
    transport = HttpxTransport(LIVE, token=os.environ.get("AXIOM_OPENFGA_LIVE_TOKEN") or None)
    store_id = create_store(transport, f"axiom-live-{uuid.uuid4().hex[:8]}")
    c = OpenFgaHttpClient(transport=transport, store_id=store_id)
    assert c.healthy()
    c.write_authorization_model(starter_type_definitions())
    return c


def test_live_check_read_write_round_trip(client: OpenFgaHttpClient):
    client.write(
        adds=[
            RelationTuple("user:alice", "member", "group:ops"),
            RelationTuple("group:ops#member", "editor", "resource:dash"),
        ]
    )
    assert client.check(user="user:alice", relation="viewer", object="resource:dash") is True
    assert client.check(user="user:bob", relation="viewer", object="resource:dash") is False
    assert client.list_members("group:ops", "member") == ["user:alice"]
    # contextual tuple: bob is asserted into ops for this one decision
    assert (
        client.check(
            user="user:bob",
            relation="viewer",
            object="resource:dash",
            contextual_tuples=(("user:bob", "member", "group:ops"),),
        )
        is True
    )
    # explicit deny relation the substrate checks first
    client.write(adds=[RelationTuple("user:alice", "blocked", "resource:dash")])
    assert client.check(user="user:alice", relation="blocked", object="resource:dash") is True


def test_live_reconciler_over_the_real_store(client: OpenFgaHttpClient):
    group = GroupRef(id=f"g-{uuid.uuid4().hex[:6]}", provider="live")
    r = MembershipReconciler(store=client)
    first = r.reconcile(group, desired_subjects=["a", "b", "c"])
    assert set(first.added) == {"a", "b", "c"}
    second = r.reconcile(group, desired_subjects=["b", "d"])
    assert set(second.removed) == {"a", "c"} and second.added == ("d",)
    assert sorted(client.list_members(f"group:{group.id}", "member")) == ["user:b", "user:d"]
    # empty desired set removes everyone — offboarding is a delete, not a no-op
    third = r.reconcile(group, desired_subjects=[])
    assert set(third.removed) == {"b", "d"}
    assert client.list_members(f"group:{group.id}", "member") == []


def test_live_write_chunking_over_100_tuples(client: OpenFgaHttpClient):
    group = GroupRef(id=f"big-{uuid.uuid4().hex[:6]}", provider="live")
    subjects = [f"s{i}" for i in range(230)]
    MembershipReconciler(store=client).reconcile(group, desired_subjects=subjects)
    assert len(client.list_members(f"group:{group.id}", "member")) == 230
