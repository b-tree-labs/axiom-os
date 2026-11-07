# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom invite <classroom_id>` — instructor side.

Closes the end-to-end demo loop: without this command, an instructor
could only mint invites in a Python REPL. With it, the instructor
runs a single command and gets a copy-paste-ready string to email.

Proactive UX invariants tested here (per
`feedback_proactive_ux_minimize_cognitive_load`):

- **No identity required up front.** First run auto-generates the
  instructor's node identity with an inferred owner + one-line narration.
- **URL only typed once.** The first invite records the coordinator
  URL; subsequent invites for the same classroom pick it up from
  disk without a flag.
- **No jargon in happy output.** The output is framed as something
  the instructor pastes into an email; no "node", "manifest",
  "base64", etc.
"""

from __future__ import annotations

import json
import re
from datetime import UTC

import pytest

from axiom.extensions.builtins.classroom.cli import _friendly_expiry, main
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.invite_token import decode_invite

# Terms that must never leak into instructor-facing happy-path output.
_JARGON_FORBIDDEN = (
    "node_id",
    "coordinator_node",
    "base64",
    "JSON envelope",
    "manifest",
    "POST",
    "endpoint",
    "signature",
)


def _assert_no_jargon(text: str) -> None:
    leaked = [t for t in _JARGON_FORBIDDEN if t.lower() in text.lower()]
    assert not leaked, f"output leaked implementation detail: {leaked}\noutput:\n{text}"


@pytest.fixture
def home_tmp(tmp_path, monkeypatch):
    """Point HOME + identity keys dir at tmp so tests are hermetic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        tmp_path / "identity",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path — one command, one copy-paste-ready invite
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_mints_invite_with_coordinator_url(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://test-coordinator.example/classroom/join",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Output frames the invite for emailing — instructor should see
        # both the classroom name and a `axi classroom join <TOKEN>` block.
        assert "NE101" in out
        assert "axi classroom join " in out
        _assert_no_jargon(out)

    def test_emitted_invite_is_decodable(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Extract the encoded token — it's the non-whitespace string on
        # the line after `axi classroom join`.
        match = re.search(r"axi classroom join (\S+)", out)
        assert match, f"no invite token in output:\n{out}"
        encoded = match.group(1)
        invite = decode_invite(encoded)
        assert invite.classroom_id == "NE101"
        assert invite.coordinator_url == "https://x/classroom/join"

    def test_mint_registers_in_file_registry(self, home_tmp):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        registry = FileInviteRegistry(
            home_tmp / ".axi" / "coordinator" / "invites.json"
        )
        tokens = registry.list_for_classroom("NE101")
        assert len(tokens) == 1
        assert tokens[0].classroom_id == "NE101"

    def test_mint_creates_cohort_on_first_run(self, home_tmp):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        store = FileCohortStore(home_tmp / ".axi" / "coordinator")
        assert store.exists("NE101")
        assert store.get_coordinator_url("NE101") == "https://x/classroom/join"


# ---------------------------------------------------------------------------
# Proactive UX 1 — auto-init identity
# ---------------------------------------------------------------------------


class TestAutoInitIdentity:
    def test_first_run_auto_creates_identity(self, home_tmp):
        keys_dir = home_tmp / "identity"
        assert not keys_dir.exists()

        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        assert (keys_dir / "private.pem").is_file()

    def test_first_run_narrates_one_time_setup(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        capsys.readouterr().out + capsys.readouterr().err
        # Re-capture after double read — merged below.

    def test_first_run_narration_avoids_jargon(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert ("one-time" in combined.lower()) or ("first time" in combined.lower())
        assert "node identity" not in combined.lower()
        assert "generating" not in combined.lower()


# ---------------------------------------------------------------------------
# Proactive UX 2 — URL is remembered after first invite
# ---------------------------------------------------------------------------


class TestCoordinatorUrlMemory:
    def test_second_invite_no_url_flag_uses_stored(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://remembered.example/classroom/join",
        ])
        assert rc == 0
        capsys.readouterr()  # drain

        # Second invite — notice: no --coordinator-url flag.
        rc = main(["invite", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        match = re.search(r"axi classroom join (\S+)", out)
        assert match
        invite = decode_invite(match.group(1))
        assert invite.coordinator_url == "https://remembered.example/classroom/join"

    def test_flag_overrides_stored_url(self, home_tmp, capsys):
        main([
            "invite", "NE101",
            "--coordinator-url", "https://old.example/classroom/join",
        ])
        capsys.readouterr()

        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://new.example/classroom/join",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        match = re.search(r"axi classroom join (\S+)", out)
        invite = decode_invite(match.group(1))
        assert invite.coordinator_url == "https://new.example/classroom/join"

        # And the store is updated for future invites.
        store = FileCohortStore(home_tmp / ".axi" / "coordinator")
        assert store.get_coordinator_url("NE101") == "https://new.example/classroom/join"


# ---------------------------------------------------------------------------
# Proactive UX 3 — bare base URLs auto-normalise to /classroom/join
# ---------------------------------------------------------------------------


class TestCoordinatorUrlNormalisation:
    """Regression for the smoke-test bug where instructors who passed a bare
    base URL (e.g. ``http://host:8788``) minted invites that the student-side
    join client could not reach (it POSTs the literal coordinator URL).
    Normalising in ``_cmd_invite`` keeps the invariant in one place.
    """

    def test_bare_base_url_gets_join_suffix(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "http://127.0.0.1:8788",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        match = re.search(r"axi classroom join (\S+)", out)
        invite = decode_invite(match.group(1))
        assert invite.coordinator_url == "http://127.0.0.1:8788/classroom/join"

    def test_url_with_trailing_slash_gets_normalised(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "http://127.0.0.1:8788/",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        match = re.search(r"axi classroom join (\S+)", out)
        invite = decode_invite(match.group(1))
        assert invite.coordinator_url == "http://127.0.0.1:8788/classroom/join"

    def test_already_correct_url_is_idempotent(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        match = re.search(r"axi classroom join (\S+)", out)
        invite = decode_invite(match.group(1))
        assert invite.coordinator_url == "https://x/classroom/join"


# ---------------------------------------------------------------------------
# Bulk invite — multi-student onboarding
# ---------------------------------------------------------------------------


class TestBulkInvite:
    """`--count N` mints N independent single-use invites in one call.

    Per-student auditability is preserved (each invite is its own token,
    consumed on first join). UX-wise, the instructor copy-pastes one
    invite per student rather than running the command N times.
    """

    def test_count_3_mints_three_invites(self, home_tmp, capsys):
        import json
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--count", "3",
            "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "invites" in data
        assert len(data["invites"]) == 3
        # All three tokens must be distinct.
        tokens = {decode_invite(inv["invite"]).token for inv in data["invites"]}
        assert len(tokens) == 3

    def test_count_1_keeps_legacy_json_shape(self, home_tmp, capsys):
        """Backward-compat: --count 1 (the default) keeps the old single-
        invite JSON shape so existing scripts don't break."""
        import json
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--count", "1",
            "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "invite" in data
        assert "invites" not in data

    def test_count_3_text_output_lists_each(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--count", "3",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Each invite line should appear with its own join token.
        join_lines = re.findall(r"axi classroom join (\S+)", out)
        assert len(join_lines) == 3
        # And all three decode to distinct tokens.
        tokens = {decode_invite(t).token for t in join_lines}
        assert len(tokens) == 3


# ---------------------------------------------------------------------------
# Error paths — framed with a clear next step, no jargon
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_first_invite_without_url_errors_with_hint(self, home_tmp, capsys):
        # No stored URL, no flag → we can't embed anything useful.
        rc = main(["invite", "NE101"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--coordinator-url" in err
        # No jargon in the error — just a friendly next step.
        assert "manifest" not in err.lower()
        assert "base64" not in err.lower()


# ---------------------------------------------------------------------------
# JSON output for scripting
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_output_shape(self, home_tmp, capsys):
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--json",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["classroom_id"] == "NE101"
        assert "invite" in data
        assert "expires" in data
        # Re-decode to sanity-check.
        invite = decode_invite(data["invite"])
        assert invite.classroom_id == "NE101"
        assert invite.coordinator_url == "https://x/classroom/join"

    def test_json_stays_parseable_on_first_run(self, home_tmp, capsys):
        """Auto-init narration must not bleed into stdout when --json."""
        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--json",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        # stdout is valid JSON (narration, if any, went to stderr).
        data = json.loads(captured.out)
        assert data["classroom_id"] == "NE101"


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestFriendlyExpiry:
    def test_iso_becomes_human_readable(self):
        # The instructor should see something they'd tell a student.
        out = _friendly_expiry("2026-04-29T17:33:32+00:00")
        assert "2026" not in out  # raw ISO year gone
        assert "T17:33:32" not in out  # ISO time separator gone
        assert "Apr" in out or "Wed" in out  # one of the friendly tokens

    def test_malformed_input_passes_through(self):
        # Never crash the CLI on a weird server-provided timestamp.
        assert _friendly_expiry("not-a-date") == "not-a-date"


class TestTtl:
    def test_default_ttl_is_at_least_a_day(self, home_tmp, capsys):
        from datetime import datetime, timedelta

        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        invite = decode_invite(data["invite"])
        expires = datetime.fromisoformat(invite.expires)
        # Default should give a student at least a day to accept.
        assert expires - datetime.now(UTC) > timedelta(hours=23)

    def test_custom_ttl_honored(self, home_tmp, capsys):
        from datetime import datetime, timedelta

        rc = main([
            "invite", "NE101",
            "--coordinator-url", "https://x/classroom/join",
            "--ttl-hours", "2",
            "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        invite = decode_invite(data["invite"])
        expires = datetime.fromisoformat(invite.expires)
        delta = expires - datetime.now(UTC)
        # Give ourselves a minute of slack for clock & test execution.
        assert timedelta(hours=1, minutes=55) <= delta <= timedelta(hours=2, minutes=5)
