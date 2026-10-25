# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`@axi do X` from inside any harness reaches the right Axiom agent.

The point is that a person in Claude Code, Codex or OpenCode should be able to
address this platform the way they address a colleague, without learning a tool
name. The harness reads one instruction at connect ("if you see `@axi`, call
this"), and everything after the handle is the request.

`parse_addressee` already split a leading name off a message, but it expected
the `@` to have been stripped upstream by a chat adapter, and it knew nothing
about the brand handle — a consumer distribution ships its own CLI name, and
addressing it reaches the same orchestrator `@axi` does. A person should not
have to know which name their install was built under.
"""

from __future__ import annotations

import pytest

from axiom.agents.addressing import NotAddressed, split_handle


class TestWhatCountsAsAnAddress:
    @pytest.mark.parametrize(
        "text,handle,rest",
        [
            ("@axi what is failing", "axi", "what is failing"),
            ("@rivet why is CI red", "rivet", "why is CI red"),
            ("@tidy prune the journal", "tidy", "prune the journal"),
            ("  @triage look at this  ", "triage", "look at this"),
            ("@AXI shouting is still addressing", "axi", "shouting is still addressing"),
        ],
    )
    def test_a_leading_handle_is_split_off(self, text, handle, rest):
        got = split_handle(text, known={"axi", "rivet", "tidy", "triage"})
        assert (got.handle, got.request) == (handle, rest)

    def test_the_brand_handle_reaches_the_orchestrator(self):
        """A distribution's own CLI name reaches the same orchestrator `@axi`
        does. A person should not have to know which name their install was
        built under."""
        got = split_handle("@acme status please", known={"axi"}, brand_handle="acme")
        assert got.handle == "axi"
        assert got.request == "status please"

    def test_punctuation_after_the_handle_is_not_part_of_the_name(self):
        got = split_handle("@axi: do the thing", known={"axi"})
        assert got.handle == "axi"
        assert got.request == "do the thing"


class TestWhatIsNotAnAddress:
    @pytest.mark.parametrize(
        "text",
        [
            "what is failing",                      # no handle at all
            "email me at @axi please",              # not leading
            "the @axi handle is how you address it",  # not leading
        ],
    )
    def test_unaddressed_text_is_refused(self, text):
        """A tool that answered unaddressed text would hijack every message the
        harness happened to pass it."""
        with pytest.raises(NotAddressed):
            split_handle(text, known={"axi"})

    def test_a_handle_with_no_request_is_refused(self):
        with pytest.raises(NotAddressed, match="nothing"):
            split_handle("@axi", known={"axi"})

    def test_an_unknown_handle_says_so_and_suggests(self):
        with pytest.raises(NotAddressed) as exc:
            split_handle("@rivets why is CI red", known={"axi", "rivet"})
        assert "rivet" in exc.value.did_you_mean

    def test_an_email_address_is_not_an_address(self):
        """`ben@axi.example` starts with no handle; the @ is mid-token."""
        with pytest.raises(NotAddressed):
            split_handle("ben@axi.example sent this", known={"axi"})


class TestTheHarnessIsActuallyTold:
    """The wiring check. A router no harness knows to call is unreachable, and
    unreachable is exactly where this platform started: `delegate_to_agent`
    existed, resolved names, and nothing ever ran."""

    def test_the_handshake_explains_addressing(self):
        from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS

        assert "ADDRESSING" in SERVER_INSTRUCTIONS
        assert "@axi" in SERVER_INSTRUCTIONS
        assert "agents.address" in SERVER_INSTRUCTIONS

    def test_it_says_to_pass_the_message_verbatim(self):
        """A harness that strips the handle before calling removes the only
        word that said who the message was for, and the router then refuses a
        message that WAS addressed."""
        from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS

        assert "VERBATIM" in SERVER_INSTRUCTIONS

    def test_the_capability_it_names_exists_and_is_mcp_surfaced(self):
        """The instruction names a capability. If that name is wrong, every
        harness is told to call something that is not there."""
        from axiom.extensions.builtins.agents.skills import bind_default

        reg = bind_default()
        assert reg.has("agents.address")
        spec = reg.spec("agents.address") if hasattr(reg, "spec") else None
        if spec is not None:
            assert "mcp" in spec.surfaces


class TestTheSiteContextIsNotSwallowed:
    """`@axi:andretti do X` must not be answered by the LOCAL agent.

    This platform addresses every principal as `@name:context`
    (`@ben.booth:axiom`, `@senna:ut-austin`), so the form will be typed whether
    or not it is wired. The first parser here accepted `@axi` and left
    `"andretti do X"` as the request: the word naming WHICH NODE was asked got
    swallowed into the question, and the local agent answered as though it had
    been addressed directly.

    That is the worst available outcome — an answer about the wrong node, in a
    shape no reader can tell is wrong. Refusing is correct until a turn can
    actually cross a node boundary.
    """

    def test_a_remote_context_is_refused_not_answered_locally(self):
        with pytest.raises(NotAddressed, match="another node"):
            split_handle("@axi:andretti what is failing", known={"axi"})

    def test_the_refusal_says_it_is_unwired_rather_than_unknown(self):
        """"Unknown agent" would send someone checking their spelling."""
        with pytest.raises(NotAddressed) as exc:
            split_handle("@axi:senna status", known={"axi"})
        assert "not wired" in str(exc.value)

    def test_an_explicitly_local_context_is_honoured(self):
        got = split_handle("@axi:local what is failing", known={"axi"})
        assert got.handle == "axi" and got.context == "local"

    def test_a_declared_local_context_is_honoured(self):
        """A node that knows its own site name answers for it."""
        got = split_handle(
            "@axi:andretti what is failing", known={"axi"}, local_contexts={"andretti"}
        )
        assert got.handle == "axi" and got.context == "andretti"

    def test_the_context_is_carried_so_a_caller_can_see_it(self):
        got = split_handle("@rivet:local why is CI red", known={"rivet"})
        assert got.context == "local"
