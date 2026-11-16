# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A tool's ``site`` argument goes through the same decision as a site read.

A tool that takes a ``site`` takes it from whoever is calling, and over MCP
that is a model filling in a field. A model-filled field is untrusted
identity input, and this one selects the tenant — so it is decided at the
gateway rather than by each tool, or by none of them.

Deciding it centrally is the point: a tool added tomorrow that takes a
``site`` is covered without knowing any of this exists.
"""

from __future__ import annotations

import pytest

from axiom.infra import authority, site_authority

SENNA, PROST = "site-b", "site-c"
CALLER = "@partner:senna"


@pytest.fixture
def denies_vcu(monkeypatch):
    ctx = site_authority.build_site_context()
    ctx.add_rule(site_authority.deny_site_rule(PROST))
    monkeypatch.setattr(site_authority, "default_site_context", lambda: ctx)
    return ctx


def _hook(args, principal=CALLER, surface="mcp"):
    return authority.authority_pre_invoke_hook(
        {"tool_name": "daq.preview", "args": args, "principal": principal,
         "surface": surface},
        principal,
    )


class TestADeniedSiteIsRefusedAtTheGateway:
    def test_a_tool_call_naming_a_denied_site_is_denied(self, denies_vcu):
        result = _hook({"site": PROST})
        assert result is not None
        assert getattr(result, "allowed", True) is False or "deny" in str(result).lower()

    def test_the_reason_names_the_site(self, denies_vcu):
        assert PROST in str(_hook({"site": PROST}))

    def test_a_permitted_site_is_not_denied(self, denies_vcu):
        assert _hook({"site": SENNA}) is None


class TestItOnlyActsWhenThereIsASiteToAct_On:
    def test_a_tool_with_no_site_argument_is_untouched(self, denies_vcu):
        assert _hook({"path": "/tmp/x.csv"}) is None

    def test_an_empty_site_is_not_a_claim(self, denies_vcu):
        assert _hook({"site": ""}) is None

    def test_whitespace_is_not_a_claim(self, denies_vcu):
        assert _hook({"site": "   "}) is None


class TestItIsUniformAcrossTools:
    @pytest.mark.parametrize(
        "tool", ["daq.preview", "daq.add_source", "telemetry.metrics", "anything.new"]
    )
    def test_any_tool_carrying_a_site_is_covered(self, denies_vcu, tool, monkeypatch):
        """The reason this lives at the gateway: a tool nobody has written
        yet is covered the moment it takes a `site`."""
        result = authority.authority_pre_invoke_hook(
            {"tool_name": tool, "args": {"site": PROST}, "principal": CALLER,
             "surface": "mcp"},
            CALLER,
        )
        assert result is not None


class TestTheOpenPostureIsUnchanged:
    def test_with_no_deny_rule_a_site_argument_passes(self, monkeypatch):
        """Behaviour-neutral by default, like the rest of this module: what
        changed is that the decision happens and is recorded."""
        ctx = site_authority.build_site_context()
        monkeypatch.setattr(site_authority, "default_site_context", lambda: ctx)
        assert _hook({"site": PROST}) is None
