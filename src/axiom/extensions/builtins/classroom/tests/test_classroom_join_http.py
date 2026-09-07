# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration test — classroom-join ceremony over real HTTP.

Tier A PR 6. Spins up an ``http.server.HTTPServer`` in a background
thread that wraps :func:`coordinator_join_endpoint`, drives the ceremony
through :class:`UrllibTransport`, and asserts end-to-end success.

Pure-function ceremony tests live in ``test_classroom_join_e2e.py`` —
they're faster and hermetic. This file exists solely to prove the
``UrllibTransport`` wiring agrees with the coordinator endpoint's
response format over actual sockets.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_client import (
    ClassroomJoinClient,
)
from axiom.extensions.builtins.classroom.classroom_coordinator import (
    InMemoryInviteRegistry,
    coordinator_join_endpoint,
)
from axiom.extensions.builtins.classroom.classroom_federation import create_cohort
from axiom.extensions.builtins.classroom.classroom_join_http import (
    UrllibTransport,
)
from axiom.extensions.builtins.classroom.invite_token import (
    create_invite_token,
    encode_invite,
)
from axiom.extensions.builtins.classroom.student_membership import (
    MembershipStore,
)
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Background HTTP server wrapping the pure coordinator endpoint
# ---------------------------------------------------------------------------


def _make_handler_class(state):
    """Produce a BaseHTTPRequestHandler class bound to shared ceremony state."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence test noise
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            status, response_body, updated = coordinator_join_endpoint(
                encoded_request=body,
                coordinator_identity=state["coordinator_identity"],
                cohort=state["cohort"],
                invite_registry=state["registry"],
            )
            if updated is not None:
                state["cohort"] = updated
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response_body.encode("utf-8"))

    return _Handler


@pytest.fixture
def coordinator_http_server(tmp_path):
    """Start a single-threaded HTTPServer on a random port, tear down cleanly."""
    coordinator_identity = generate_identity(
        owner="ondrej@ctu.cz",
        display_name="Test Peer",
        keys_dir=tmp_path / "coord-keys",
    )
    cohort = create_cohort("ne101-prague-2026", coordinator_identity.node_id)
    registry = InMemoryInviteRegistry()
    state = {
        "coordinator_identity": coordinator_identity,
        "cohort": cohort,
        "registry": registry,
    }

    server = HTTPServer(("127.0.0.1", 0), _make_handler_class(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "server": server,
            "url": f"http://127.0.0.1:{server.server_port}/classroom/join",
            "state": state,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# End-to-end over real HTTP
# ---------------------------------------------------------------------------


class TestRealHttpCeremony:
    def test_student_joins_via_real_http(self, coordinator_http_server, tmp_path):
        state = coordinator_http_server["state"]
        # Instructor mints invite + registers it with the coordinator.
        invite = create_invite_token(
            classroom_id=state["cohort"].classroom_id,
            coordinator_id=state["coordinator_identity"].node_id,
            ttl_hours=24,
        )
        state["registry"].register(invite)
        encoded_invite = encode_invite(invite)

        # Student side: fresh identity, fresh store.
        student_identity = generate_identity(
            owner="alice@example.org",
            display_name="Alice",
            keys_dir=tmp_path / "alice-keys",
        )
        store = MembershipStore(base_dir=tmp_path / "alice-state")

        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=UrllibTransport(timeout_s=10.0),
            store=store,
        )
        result = client.join(
            encoded_invite=encoded_invite,
            student_id="alice",
            coordinator_url=coordinator_http_server["url"],
        )
        assert result.accepted is True
        assert result.membership is not None
        # Manifest saved.
        loaded = store.load(state["cohort"].classroom_id)
        assert loaded.student_id == "alice"
        # Coordinator side recorded the member.
        assert len(state["cohort"].members) == 1
        assert state["cohort"].members[0].student_id == "alice"

    def test_server_returns_400_and_client_surfaces_error(
        self, coordinator_http_server, tmp_path
    ):
        state = coordinator_http_server["state"]
        # Mint an invite but DON'T register it — coordinator will reject.
        invite = create_invite_token(
            classroom_id=state["cohort"].classroom_id,
            coordinator_id=state["coordinator_identity"].node_id,
            ttl_hours=24,
        )
        encoded_invite = encode_invite(invite)

        student_identity = generate_identity(
            owner="bob@example.org",
            display_name="Bob",
            keys_dir=tmp_path / "bob-keys",
        )
        store = MembershipStore(base_dir=tmp_path / "bob-state")

        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=UrllibTransport(timeout_s=10.0),
            store=store,
        )
        result = client.join(
            encoded_invite=encoded_invite,
            student_id="bob",
            coordinator_url=coordinator_http_server["url"],
        )
        assert result.accepted is False
        assert "invite" in result.error.lower()
        assert store.list_ids() == []
