# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One place decides which site a caller may read.

Every read surface took the tenant from the REQUEST and never checked the
CALLER: an explicit site beat the binding, an HTTP `?site=` went straight to
the query, and `site` was a model-filled MCP argument. Three surfaces, one
missing check.

This is that check, built on the authorization decision point that already
exists rather than beside it, so a site read gets the same rules, the same
precedence (deny > propose > permit) and the same receipts as a tool call.

The posture is open by default, exactly as `open_tool_rule` is: the point of
this change is that the decision now HAPPENS and is recorded, so a site can
close it with a rule instead of a patch to every caller.
"""

from __future__ import annotations

import pytest

from axiom.infra.site_authority import (
    OPEN_SITE_RULE_NAME,
    SITE_RESOURCE_SCHEME,
    SiteNotAuthorized,
    authorize_site,
    build_site_context,
    build_site_read_envelope,
    deny_site_rule,
)

ALICE = "@alice:ut"
SENNA, PROST = "site-b", "site-c"


class TestTheEnvelopeDescribesTheRead:
    def test_the_resource_is_the_site(self):
        env = build_site_read_envelope(SENNA, ALICE)
        assert env.resource.scheme == SITE_RESOURCE_SCHEME
        assert env.resource.identifier == SENNA

    def test_the_actor_is_the_caller_not_the_site(self):
        env = build_site_read_envelope(SENNA, ALICE)
        assert ALICE.lstrip("@").split(":")[0] in str(env.actor)

    @pytest.mark.parametrize("handle", ["", "   ", "@a:b:c"])
    def test_a_handle_that_cannot_be_a_principal_is_refused(self, handle):
        """A read whose caller cannot be identified cannot be authorised.

        The accepted SHAPE is the platform's, not this module's: a bare
        `nocontext` handle is legal there, so it is legal here. Tightening it
        only for site reads would make them refuse callers every other
        surface accepts, which is the kind of divergence this module exists
        to remove.
        """
        with pytest.raises(ValueError):
            build_site_read_envelope(SENNA, handle)

    def test_the_surface_travels_so_a_rule_can_scope_to_it(self):
        """A site may want to permit the CLI and propose over MCP, which is
        only expressible if the envelope says which surface asked."""
        assert build_site_read_envelope(SENNA, ALICE, surface="mcp").surface == "mcp"


class TestTheOpenPostureChangesNoBehaviour:
    def test_a_site_read_is_permitted_when_no_rules_are_loaded(self):
        assert authorize_site(SENNA, principal=ALICE, ctx=build_site_context()) == SENNA

    def test_the_permitting_rule_is_named_so_the_receipt_says_why(self):
        assert OPEN_SITE_RULE_NAME


class TestARuleCanCloseIt:
    def test_a_denied_site_raises_rather_than_returning_it(self):
        ctx = build_site_context()
        ctx.add_rule(deny_site_rule(PROST))
        with pytest.raises(SiteNotAuthorized):
            authorize_site(PROST, principal=ALICE, ctx=ctx)

    def test_denying_one_site_leaves_the_others_readable(self):
        ctx = build_site_context()
        ctx.add_rule(deny_site_rule(PROST))
        assert authorize_site(SENNA, principal=ALICE, ctx=ctx) == SENNA

    def test_the_refusal_names_the_site_and_the_caller(self):
        ctx = build_site_context()
        ctx.add_rule(deny_site_rule(PROST))
        with pytest.raises(SiteNotAuthorized) as exc:
            authorize_site(PROST, principal=ALICE, ctx=ctx)
        assert PROST in str(exc.value) and ALICE in str(exc.value)


class TestTheBindingIsADefaultAndStillChecked:
    """The bug this closes: `site or config.site` meant a bound node
    restricted nothing, it only defaulted."""

    def test_an_omitted_site_falls_back_to_the_bound_one(self):
        assert authorize_site(
            "", principal=ALICE, bound=SENNA, ctx=build_site_context()
        ) == SENNA

    def test_the_bound_site_is_decided_on_too_not_waved_through(self):
        """A default is not an authorisation. If the policy denies the bound
        site, falling back to it must fail like any other."""
        ctx = build_site_context()
        ctx.add_rule(deny_site_rule(SENNA))
        with pytest.raises(SiteNotAuthorized):
            authorize_site("", principal=ALICE, bound=SENNA, ctx=ctx)

    def test_an_explicit_site_no_longer_silently_beats_the_binding(self):
        ctx = build_site_context()
        ctx.add_rule(deny_site_rule(PROST))
        with pytest.raises(SiteNotAuthorized):
            authorize_site(PROST, principal=ALICE, bound=SENNA, ctx=ctx)

    def test_a_read_with_no_scope_at_all_is_refused(self):
        """Neither requested nor bound. An unscoped read is not a read."""
        with pytest.raises(SiteNotAuthorized):
            authorize_site("", principal=ALICE, bound="", ctx=build_site_context())


class TestProposeIsARefusalForARead:
    def test_a_proposed_read_raises_rather_than_blocking(self):
        """A write can wait for a human. A read in the middle of a query
        cannot, so `propose` is reported as a refusal that says why."""
        from axiom.infra.site_authority import propose_site_rule

        ctx = build_site_context()
        ctx.add_rule(propose_site_rule(PROST))
        with pytest.raises(SiteNotAuthorized) as exc:
            authorize_site(PROST, principal=ALICE, ctx=ctx)
        assert "approval" in str(exc.value).lower()
