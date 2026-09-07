# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the file-backed coordinator invite registry.

Instructors run `axi classroom invite` to mint tokens and `axi classroom
serve` in a separate process to accept student joins. Both need to share
the same registry state — so the registry must survive process
restarts and be safely re-openable. `InMemoryInviteRegistry` covers
tests + single-process demos; `FileInviteRegistry` is the production
surface backing both CLI commands.

Contract: implements the `InviteRegistry` protocol identically to the
in-memory impl, plus durability across instance recreation.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.invite_token import create_invite_token

# ---------------------------------------------------------------------------
# Protocol conformance — duck-type: can we swap it into the ceremony?
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_file_registry_has_invite_registry_surface(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        for method in ("register", "find_by_token", "is_consumed", "mark_consumed"):
            assert callable(getattr(reg, method)), f"missing {method}"


# ---------------------------------------------------------------------------
# Registration & lookup
# ---------------------------------------------------------------------------


class TestRegisterAndFind:
    def test_find_unknown_token_returns_none(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        assert reg.find_by_token("nope") is None

    def test_registered_invite_is_findable(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        invite = create_invite_token("c1", "n1", ttl_hours=24)
        reg.register(invite)
        found = reg.find_by_token(invite.token)
        assert found is not None
        assert found.classroom_id == "c1"
        assert found.coordinator_id == "n1"
        assert found.token == invite.token
        assert found.expires == invite.expires

    def test_register_preserves_optional_coordinator_url(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        invite = create_invite_token(
            "c1", "n1", ttl_hours=24,
            coordinator_url="https://test-coordinator.example/classroom/join",
        )
        reg.register(invite)
        found = reg.find_by_token(invite.token)
        assert found is not None
        assert found.coordinator_url == "https://test-coordinator.example/classroom/join"

    def test_multiple_invites_coexist(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        a = create_invite_token("c1", "n1", 1)
        b = create_invite_token("c1", "n1", 1)
        c = create_invite_token("c2", "n1", 1)
        reg.register(a)
        reg.register(b)
        reg.register(c)
        assert reg.find_by_token(a.token).classroom_id == "c1"
        assert reg.find_by_token(b.token).token == b.token
        assert reg.find_by_token(c.token).classroom_id == "c2"


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------


class TestConsumption:
    def test_freshly_registered_invite_is_not_consumed(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        invite = create_invite_token("c1", "n1", 1)
        reg.register(invite)
        assert reg.is_consumed(invite.token) is False

    def test_mark_consumed_flips_the_state(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        invite = create_invite_token("c1", "n1", 1)
        reg.register(invite)
        reg.mark_consumed(invite.token)
        assert reg.is_consumed(invite.token) is True

    def test_is_consumed_false_for_unknown_token(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        # No crash, just False — matches in-memory impl.
        assert reg.is_consumed("never-seen") is False


# ---------------------------------------------------------------------------
# Durability — the whole point of the file registry
# ---------------------------------------------------------------------------


class TestDurability:
    def test_registered_invite_survives_fresh_instance(self, tmp_path):
        path = tmp_path / "invites.json"
        reg1 = FileInviteRegistry(path)
        invite = create_invite_token("c1", "n1", 24)
        reg1.register(invite)

        reg2 = FileInviteRegistry(path)
        found = reg2.find_by_token(invite.token)
        assert found is not None
        assert found.classroom_id == "c1"

    def test_consumed_state_survives_fresh_instance(self, tmp_path):
        path = tmp_path / "invites.json"
        reg1 = FileInviteRegistry(path)
        invite = create_invite_token("c1", "n1", 24)
        reg1.register(invite)
        reg1.mark_consumed(invite.token)

        reg2 = FileInviteRegistry(path)
        assert reg2.is_consumed(invite.token) is True

    def test_concurrent_instances_see_each_others_writes(self, tmp_path):
        """Mint in process A, observe in process B — the ser/vice model."""
        path = tmp_path / "invites.json"
        minter = FileInviteRegistry(path)
        server = FileInviteRegistry(path)

        invite = create_invite_token("c1", "n1", 24)
        minter.register(invite)

        # Server, which existed BEFORE the mint, must still pick it up
        # because find_by_token re-reads disk on each call.
        found = server.find_by_token(invite.token)
        assert found is not None
        assert found.token == invite.token


# ---------------------------------------------------------------------------
# Initial state / empty file handling
# ---------------------------------------------------------------------------


class TestEmptyAndMissing:
    def test_missing_file_is_not_an_error(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "does_not_exist.json")
        assert reg.find_by_token("anything") is None
        assert reg.is_consumed("anything") is False

    def test_first_register_creates_file_and_parent_dirs(self, tmp_path):
        nested = tmp_path / "a" / "b" / "invites.json"
        reg = FileInviteRegistry(nested)
        invite = create_invite_token("c1", "n1", 1)
        reg.register(invite)
        assert nested.is_file()

    def test_file_format_is_human_readable_json(self, tmp_path):
        """Instructor debugging should be able to eyeball the file."""
        path = tmp_path / "invites.json"
        reg = FileInviteRegistry(path)
        invite = create_invite_token("ne101", "coord", 1)
        reg.register(invite)
        reg.mark_consumed(invite.token)

        data = json.loads(path.read_text())
        # Exactly two top-level keys, stable names.
        assert set(data.keys()) == {"invites", "consumed"}
        assert invite.token in data["invites"]
        assert invite.token in data["consumed"]
        assert data["invites"][invite.token]["classroom_id"] == "ne101"


# ---------------------------------------------------------------------------
# Helpers an instructor-facing CLI will want
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_list_invites_for_classroom(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        a = create_invite_token("NE101", "n1", 1)
        b = create_invite_token("NE101", "n1", 1)
        c = create_invite_token("OTHER", "n1", 1)
        for inv in (a, b, c):
            reg.register(inv)

        listed = reg.list_for_classroom("NE101")
        tokens = {inv.token for inv in listed}
        assert tokens == {a.token, b.token}

    def test_list_invites_empty_for_unknown_classroom(self, tmp_path):
        reg = FileInviteRegistry(tmp_path / "invites.json")
        assert reg.list_for_classroom("NOPE") == []


# ---------------------------------------------------------------------------
# Resilience — a half-written file shouldn't wedge the server
# ---------------------------------------------------------------------------


class TestResilience:
    def test_garbled_file_raises_clear_error(self, tmp_path):
        path = tmp_path / "invites.json"
        path.write_text("not json at all {{{")
        reg = FileInviteRegistry(path)
        # A corrupt registry is a serious problem — don't silently drop
        # invites. Raise a clear ValueError so the CLI can tell the
        # instructor to inspect the file.
        with pytest.raises(ValueError, match="corrupt"):
            reg.find_by_token("anything")
