# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom join <invite>` — CLI-level wiring.

Data-model tests live in ``test_invite_token.py``. This file covers
only what the CLI dispatcher adds: argument parsing, text/json output
shape, exit codes, and error paths through the handler.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.invite_token import (
    InviteToken,
    create_invite_token,
    encode_invite,
)


class TestClassroomJoinCLI:
    def test_valid_invite_returns_zero_and_prints_classroom(self, capsys):
        invite = create_invite_token(
            classroom_id="ne101-prague-2026",
            coordinator_id="peer_node_abc",
            ttl_hours=24,
        )
        encoded = encode_invite(invite)

        rc = main(["join", encoded])
        assert rc == 0
        out = capsys.readouterr().out
        # Student sees the classroom name. coordinator_id is internal and
        # intentionally hidden from student-facing output.
        assert "ne101-prague-2026" in out

    def test_valid_invite_json_output_shape(self, capsys):
        invite = create_invite_token("c1", "n1", 24)
        encoded = encode_invite(invite)

        rc = main(["join", encoded, "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["accepted"] is True
        assert payload["classroom_id"] == "c1"
        assert payload["coordinator_id"] == "n1"
        assert "expires" in payload
        assert "error" not in payload

    def test_expired_invite_returns_one(self, capsys):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        invite = InviteToken(
            token="abc",
            classroom_id="c1",
            coordinator_id="n1",
            expires=past,
        )
        encoded = encode_invite(invite)

        rc = main(["join", encoded])
        assert rc == 1
        err = capsys.readouterr().err
        # Humanized message — "expired" mentioned with a next-step suggestion.
        assert "expired" in err.lower()
        assert "instructor" in err.lower()

    def test_expired_invite_json_includes_error(self, capsys):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        invite = InviteToken(
            token="abc",
            classroom_id="c1",
            coordinator_id="n1",
            expires=past,
        )
        encoded = encode_invite(invite)

        rc = main(["join", encoded, "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["accepted"] is False
        assert "expired" in payload["error"].lower()
        assert payload["classroom_id"] == "c1"

    def test_malformed_invite_returns_one(self, capsys):
        rc = main(["join", "this-is-not-a-valid-invite"])
        assert rc == 1
        err = capsys.readouterr().err
        # Humanized message hints at copy-paste issue + suggests a fix.
        assert "damaged" in err.lower() or "copy" in err.lower()

    def test_malformed_invite_json(self, capsys):
        rc = main(["join", "garbage!!!", "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["accepted"] is False
        assert "error" in payload

    def test_empty_invite_rejected(self, capsys):
        rc = main(["join", ""])
        assert rc == 1

    def test_missing_invite_arg_is_argparse_error(self, capsys):
        # argparse exits with 2 on missing positional; we don't override.
        with pytest.raises(SystemExit) as exc_info:
            main(["join"])
        assert exc_info.value.code == 2
