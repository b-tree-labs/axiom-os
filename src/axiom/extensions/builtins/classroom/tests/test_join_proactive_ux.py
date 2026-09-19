# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Proactive-UX tests for `axi classroom join`.

Two principles, both per
`feedback_proactive_ux_minimize_cognitive_load` — ship them so we
never regress to an "error + tell the user to run another command"
experience:

1. If the invite's envelope embeds a ``coordinator_url``, the student
   does NOT need ``--coordinator URL`` on the CLI. Paste one string,
   done.
2. If the student hasn't run ``axi federation init`` yet, the join
   command does it for them — with a one-line narration — rather
   than erroring out with "run X first."
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_coordinator import (
    InMemoryInviteRegistry,
    coordinator_join_endpoint,
)
from axiom.extensions.builtins.classroom.classroom_federation import create_cohort
from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.invite_token import (
    create_invite_token,
    decode_invite,
    encode_invite,
)
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# HTTP server fixture — real coordinator over loopback
# ---------------------------------------------------------------------------


def _make_handler_class(state):
    class _H(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
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

    return _H


@pytest.fixture
def running_coordinator(tmp_path):
    coord = generate_identity(
        owner="ondrej@ctu.cz",
        display_name="Test Peer",
        keys_dir=tmp_path / "coord-keys",
    )
    cohort = create_cohort("ne101-prague-2026", coord.node_id)
    registry = InMemoryInviteRegistry()
    state = {
        "coordinator_identity": coord,
        "cohort": cohort,
        "registry": registry,
    }
    server = HTTPServer(("127.0.0.1", 0), _make_handler_class(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "state": state,
            "url": f"http://127.0.0.1:{server.server_port}/classroom/join",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# UX principle 1 — invite-embedded coordinator_url
# ---------------------------------------------------------------------------


class TestInviteEmbeddedCoordinatorUrl:
    def test_invite_carries_coordinator_url_roundtrip(self):
        invite = create_invite_token(
            classroom_id="c1",
            coordinator_id="n1",
            ttl_hours=1,
            coordinator_url="https://test-coordinator.example/classroom/join",
        )
        encoded = encode_invite(invite)
        decoded = decode_invite(encoded)
        assert decoded.coordinator_url == "https://test-coordinator.example/classroom/join"

    def test_invite_without_coordinator_url_is_still_valid(self):
        """Backward compatibility: older invites (no url) still decode."""
        invite = create_invite_token("c1", "n1", 1)
        assert invite.coordinator_url is None
        decoded = decode_invite(encode_invite(invite))
        assert decoded.coordinator_url is None

    def test_cli_uses_invite_url_when_no_flag(
        self, running_coordinator, tmp_path, monkeypatch
    ):
        """Student pastes ONE string. No --coordinator flag needed."""
        state = running_coordinator["state"]
        url = running_coordinator["url"]

        # Instructor mints an invite that carries the URL.
        invite = create_invite_token(
            classroom_id=state["cohort"].classroom_id,
            coordinator_id=state["coordinator_identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        state["registry"].register(invite)
        encoded = encode_invite(invite)

        # Redirect identity + axi home to tmp_path via env override.
        monkeypatch.setattr(
            "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
            tmp_path / "identity",
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        # NOTE: no --coordinator flag!
        rc = main(["join", encoded, "--student-id", "alice", "--json"])
        assert rc == 0
        # Coordinator state has alice.
        assert any(m.student_id == "alice" for m in state["cohort"].members)


# ---------------------------------------------------------------------------
# UX principle 2 — auto-init identity
# ---------------------------------------------------------------------------


class TestAutoInitIdentity:
    def test_join_auto_initializes_identity_if_missing(
        self, running_coordinator, tmp_path, monkeypatch, capsys
    ):
        """Student who never ran `axi federation init` can still join."""
        state = running_coordinator["state"]
        url = running_coordinator["url"]

        invite = create_invite_token(
            classroom_id=state["cohort"].classroom_id,
            coordinator_id=state["coordinator_identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        state["registry"].register(invite)
        encoded = encode_invite(invite)

        # Fresh environment — no identity exists.
        keys_dir = tmp_path / "identity"
        monkeypatch.setattr(
            "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
            keys_dir,
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        assert not keys_dir.exists()

        rc = main(["join", encoded, "--student-id", "alice"])
        assert rc == 0

        # Identity WAS auto-generated.
        assert keys_dir.exists()
        assert (keys_dir / "private.pem").is_file()

        # Narration was emitted (proactive explanation, no jargon).
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "one-time" in combined.lower() or "first time" in combined.lower()
        # Should NOT leak internal terminology.
        assert "node identity" not in combined.lower()
        assert "generating" not in combined.lower()

    def test_json_output_suppresses_identity_narration(
        self, running_coordinator, tmp_path, monkeypatch, capsys
    ):
        """With --json, the auto-init narration stays on stderr so JSON stdout is parseable."""
        state = running_coordinator["state"]
        url = running_coordinator["url"]

        invite = create_invite_token(
            classroom_id=state["cohort"].classroom_id,
            coordinator_id=state["coordinator_identity"].node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        state["registry"].register(invite)
        encoded = encode_invite(invite)

        monkeypatch.setattr(
            "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
            tmp_path / "identity",
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        rc = main(["join", encoded, "--student-id", "alice", "--json"])
        assert rc == 0

        captured = capsys.readouterr()
        # stdout is valid JSON (no narration bleed).
        data = json.loads(captured.out)
        assert data["accepted"] is True


# ---------------------------------------------------------------------------
# Dry-run still works when neither source provides a URL
# ---------------------------------------------------------------------------


class TestDryRunFallback:
    def test_invite_without_url_and_no_flag_falls_to_preview(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))

        invite = create_invite_token("c1", "n1", ttl_hours=1)  # no url
        encoded = encode_invite(invite)

        rc = main(["join", encoded])
        assert rc == 0
        out = capsys.readouterr().out
        # Student-friendly preview output — mentions what they joined
        # and tells them what to do next.
        assert "looks good" in out.lower()
        assert "ask your instructor" in out.lower()
