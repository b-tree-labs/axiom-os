# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the coordinator HTTP server.

The server is the long-running process an instructor runs as `axi
classroom serve`. It re-reads the file-backed registries on every
request so invites minted by a separate `axi classroom invite`
process are picked up without restart.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_client import (
    ClassroomJoinClient,
)
from axiom.extensions.builtins.classroom.classroom_federation import create_cohort
from axiom.extensions.builtins.classroom.classroom_join_http import UrllibTransport
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.coordinator_server import (
    make_coordinator_handler,
)
from axiom.extensions.builtins.classroom.invite_token import (
    create_invite_token,
    encode_invite,
)
from axiom.extensions.builtins.classroom.student_membership import MembershipStore
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def coordinator_state(tmp_path):
    """Pre-provision instructor state: identity, cohort, one invite."""
    coord_dir = tmp_path / "coord"
    coord_keys = tmp_path / "coord_keys"
    identity = generate_identity(owner="prof@example.org", keys_dir=coord_keys)

    registry = FileInviteRegistry(coord_dir / "invites.json")
    store = FileCohortStore(coord_dir)

    cohort = create_cohort("NE101", identity.node_id)
    store.save(cohort, coordinator_url="http://placeholder/classroom/join")

    return {
        "coord_dir": coord_dir,
        "identity": identity,
        "registry": registry,
        "store": store,
        "classroom_id": "NE101",
    }


@pytest.fixture
def running_server(coordinator_state):
    state = coordinator_state
    handler_cls = make_coordinator_handler(
        coordinator_identity=state["identity"],
        classroom_id=state["classroom_id"],
        cohort_store=state["store"],
        invite_registry=state["registry"],
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{server.server_port}/classroom/join",
            **state,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Happy path — student joins through the real wire
# ---------------------------------------------------------------------------


class TestJoinCeremony:
    def test_student_can_join_through_running_server(self, running_server, tmp_path):
        state = running_server
        url = state["url"]

        invite = create_invite_token(
            classroom_id=state["classroom_id"],
            coordinator_id=state["identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        state["registry"].register(invite)

        student_identity = generate_identity(
            owner="alice@example.org",
            keys_dir=tmp_path / "student_keys",
        )
        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=UrllibTransport(),
            store=MembershipStore(base_dir=tmp_path / "student_axi"),
        )
        result = client.join(
            encoded_invite=encode_invite(invite),
            student_id="alice",
            coordinator_url=url,
        )
        assert result.accepted is True

    def test_cohort_update_persists_to_disk(self, running_server, tmp_path):
        state = running_server
        url = state["url"]

        invite = create_invite_token(
            classroom_id=state["classroom_id"],
            coordinator_id=state["identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        state["registry"].register(invite)

        student_identity = generate_identity(
            owner="alice@example.org",
            keys_dir=tmp_path / "student_keys",
        )
        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=UrllibTransport(),
            store=MembershipStore(base_dir=tmp_path / "student_axi"),
        )
        client.join(
            encoded_invite=encode_invite(invite),
            student_id="alice",
            coordinator_url=url,
        )

        # Re-read the cohort from disk — new process would see alice.
        reloaded = FileCohortStore(state["coord_dir"]).load(state["classroom_id"])
        assert any(m.student_id == "alice" for m in reloaded.members)

    def test_invite_is_marked_consumed_after_success(self, running_server, tmp_path):
        state = running_server
        url = state["url"]

        invite = create_invite_token(
            classroom_id=state["classroom_id"],
            coordinator_id=state["identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        state["registry"].register(invite)

        student_identity = generate_identity(
            owner="alice@example.org",
            keys_dir=tmp_path / "student_keys",
        )
        ClassroomJoinClient(
            student_identity=student_identity,
            transport=UrllibTransport(),
            store=MembershipStore(base_dir=tmp_path / "student_axi"),
        ).join(
            encoded_invite=encode_invite(invite),
            student_id="alice",
            coordinator_url=url,
        )

        # Fresh registry instance proves state went through disk.
        reloaded = FileInviteRegistry(state["coord_dir"] / "invites.json")
        assert reloaded.is_consumed(invite.token) is True


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_unknown_invite_returns_400(self, running_server):
        url = running_server["url"]
        # Forge a syntactically valid request that references an unknown token.
        req = urllib.request.Request(
            url,
            data=b'"clearly-not-a-real-request"',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read().decode())
        assert "error" in body


# ---------------------------------------------------------------------------
# Cross-process — invite minted AFTER server started must still work
# ---------------------------------------------------------------------------


class TestCrossProcessSharing:
    def test_invite_minted_after_server_start_is_accepted(
        self, running_server, tmp_path
    ):
        """Simulates the two-terminal workflow: `serve` already running
        when the instructor `invite`s. The new invite shows up without
        server restart because the handler re-reads the registry file."""
        state = running_server
        url = state["url"]

        # A FRESH registry instance writes — mimics `axi classroom invite`
        # running as a separate short-lived process.
        other_process_registry = FileInviteRegistry(state["coord_dir"] / "invites.json")
        invite = create_invite_token(
            classroom_id=state["classroom_id"],
            coordinator_id=state["identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        other_process_registry.register(invite)

        # The running server should see it on next request.
        student_identity = generate_identity(
            owner="bob@example.org",
            keys_dir=tmp_path / "student_keys",
        )
        result = ClassroomJoinClient(
            student_identity=student_identity,
            transport=UrllibTransport(),
            store=MembershipStore(base_dir=tmp_path / "student_axi"),
        ).join(
            encoded_invite=encode_invite(invite),
            student_id="bob",
            coordinator_url=url,
        )
        assert result.accepted is True
