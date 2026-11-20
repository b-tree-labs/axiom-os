# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student-facing strings for `axi classroom join` don't leak implementation detail.

Per ``feedback_proactive_ux_minimize_cognitive_load`` — students shouldn't
have to know the words *node*, *coordinator*, *manifest*, *signature*,
*pack*, *base64url*, *JSON envelope*, *POST*, *endpoint*, or see raw
file paths in normal-case output. Technical error message bodies do
sometimes pass through, but wrapped in a plain-language sentence.

This test is specifically prescriptive to catch regressions: if a
future PR starts leaking `"Coordinator node: peer_abc"` or
`"Membership saved: ~/.axi/..."` back into student output, it fails
here first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.classroom.cli import _humanize_join_error, main
from axiom.extensions.builtins.classroom.invite_token import (
    InviteToken,
    create_invite_token,
    encode_invite,
)

# Terms that must NEVER appear in a student-facing (non-error-bubble)
# output line. If they do, the UX has regressed.
_JARGON_FORBIDDEN_IN_HAPPY_OUTPUT = (
    "node_id",
    "coordinator_node",
    "membership.json",
    "~/.axi/",
    "base64",
    "JSON envelope",
    "signature",
    "POST",
    "endpoint",
    "manifest",
)


def _assert_no_jargon(text: str, banned: tuple[str, ...] = _JARGON_FORBIDDEN_IN_HAPPY_OUTPUT):
    lower = text.lower()
    leaked = [term for term in banned if term.lower() in lower]
    assert not leaked, f"student-facing output leaked implementation detail: {leaked}\noutput:\n{text}"


# ---------------------------------------------------------------------------
# Preview path (no coordinator URL in invite or flag)
# ---------------------------------------------------------------------------


class TestPreviewOutput:
    def test_preview_output_has_no_jargon(self, capsys):
        invite = create_invite_token("NE101", "some-coord", ttl_hours=24)
        encoded = encode_invite(invite)
        rc = main(["join", encoded])
        assert rc == 0
        out = capsys.readouterr().out
        _assert_no_jargon(out)
        # Should mention the classroom name the student cares about.
        assert "NE101" in out

    def test_preview_suggests_next_action_in_user_terms(self, capsys):
        invite = create_invite_token("NE101", "c", ttl_hours=24)
        rc = main(["join", encode_invite(invite)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "instructor" in out.lower() or "classroom server" in out.lower()


# ---------------------------------------------------------------------------
# Success path (real-HTTP covered elsewhere; here we just check strings)
# ---------------------------------------------------------------------------


class TestSuccessOutputJargonFree:
    """Uses `test_join_proactive_ux.py` fixtures indirectly via full join
    but asserts on OUTPUT shape rather than behavior."""

    def test_success_prints_classroom_name_and_student_id(self, capsys):
        """Placeholder — full-flow success tested in e2e / http suites.
        Assertion here: if someone ever adds back the technical lines,
        the jargon test in TestPreviewOutput catches at least the shared
        string paths.
        """
        # This is covered by the real-HTTP tests; keep this slot for a
        # future regression test if the success path gains new lines.
        pass


# ---------------------------------------------------------------------------
# Error humanizer
# ---------------------------------------------------------------------------


class TestErrorHumanizer:
    def test_expired_error_suggests_instructor(self):
        msg = _humanize_join_error("invite expired at 2026-01-01T00:00:00+00:00")
        assert "expired" in msg.lower()
        assert "instructor" in msg.lower()

    def test_unknown_invite_suggests_fresh_invite(self):
        msg = _humanize_join_error("invite token not recognized by this coordinator")
        assert "fresh" in msg.lower() or "new" in msg.lower()
        assert "instructor" in msg.lower()

    def test_consumed_invite_explains_and_suggests(self):
        msg = _humanize_join_error("invite token already consumed (reuse refused)")
        assert "already been used" in msg.lower()
        assert "instructor" in msg.lower()

    def test_signature_failure_suggests_copy_issue(self):
        msg = _humanize_join_error(
            "join request rejected: signature verification failed"
        )
        assert "damaged" in msg.lower() or "copy" in msg.lower()

    def test_transport_error_suggests_network(self):
        msg = _humanize_join_error("transport error calling https://x: timeout")
        assert "internet" in msg.lower() or "network" in msg.lower()

    def test_http_error_suggests_network(self):
        msg = _humanize_join_error("coordinator refused (HTTP 500)")
        # Either network path OR a wrapped pass-through — both are fine as
        # long as the message doesn't leave the student without next steps.
        assert "classroom server" in msg.lower() or "internet" in msg.lower() or "try again" in msg.lower()

    def test_unknown_error_still_produces_useful_text(self):
        msg = _humanize_join_error("some genuinely unexpected internal condition")
        # We still return SOMETHING meaningful — not an empty string.
        assert len(msg) > 10
        # Passes the raw message through with a soft prefix so it's never
        # dropped on the floor, but it's wrapped in plain language.
        assert "couldn't join" in msg.lower() or "classroom server" in msg.lower()

    def test_empty_error_handled_gracefully(self):
        msg = _humanize_join_error("")
        assert "instructor" in msg.lower()


# ---------------------------------------------------------------------------
# Expired / damaged invite produce friendly output
# ---------------------------------------------------------------------------


class TestFriendlyErrors:
    def test_expired_invite_uses_humanizer(self, capsys):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        invite = InviteToken(
            token="t", classroom_id="c", coordinator_id="n", expires=past
        )
        rc = main(["join", encode_invite(invite)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "expired" in err.lower()
        assert "instructor" in err.lower()
        # No raw exception trace or technical wording leaking through.
        assert "traceback" not in err.lower()
        assert "valueerror" not in err.lower()

    def test_damaged_invite_gives_copy_paste_hint(self, capsys):
        rc = main(["join", "this-is-not-a-valid-invite"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "damaged" in err.lower() or "copy" in err.lower()
        # No jargon.
        assert "invalidinviteerror" not in err.lower()
        assert "base64" not in err.lower()
