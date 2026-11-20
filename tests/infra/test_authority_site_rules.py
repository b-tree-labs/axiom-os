# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-114 §2 — the site-manifest authority Rule loader (the *whether* gate).

The loader reads node-durable TOML into authz ``Rule``s that layer over the
built-in open posture. deny/propose outrank the open permit by disposition, so
a site can hold or refuse a specific ``tool://<name>`` without a default-deny.
Presence of a (even malformed) policy file is the fail-closed signal.
"""
from __future__ import annotations

import pytest

from axiom.infra import authority_rules as sr


def _write(path, body: str):
    path.write_text(body, encoding="utf-8")
    return path


# ---- absence / presence --------------------------------------------------


def test_absent_file_is_empty_and_not_present(tmp_path):
    policy = sr.load_site_policy(tmp_path / "nope.toml")
    assert policy.rules == ()
    assert policy.present is False


def test_default_path_is_under_the_user_state_dir():
    p = sr.default_site_rules_path()
    assert p.name == "site_rules.toml"
    assert p.parent.name == "authority"


def test_env_override_path(tmp_path, monkeypatch):
    f = _write(tmp_path / "custom.toml", '[[rule]]\ntool="x.y"\ndisposition="deny"\n')
    monkeypatch.setenv(sr.SITE_RULES_ENV, str(f))
    policy = sr.load_site_policy()  # no explicit path -> resolves the env
    assert policy.present is True and len(policy.rules) == 1


# ---- happy path ----------------------------------------------------------


def test_loads_a_deny_rule_with_all_fields(tmp_path):
    f = _write(
        tmp_path / "p.toml",
        '[[rule]]\nname="hold-publish"\ntool="press.publish"\n'
        'disposition="propose"\npriority=100\nactor="@svc:acme"\n',
    )
    policy = sr.load_site_policy(f)
    assert policy.present is True and len(policy.rules) == 1
    r = policy.rules[0]
    assert r.name == "hold-publish"
    assert r.disposition == "propose"
    assert r.priority == 100
    assert r.actor_pattern == "@svc:acme"
    assert r.resource_pattern.value == "tool://press.publish"
    assert r.intent_pattern.value == sr.TOOL_INVOKE_INTENT


def test_actor_defaults_to_wildcard_and_name_is_synthesised(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="data.ingest"\ndisposition="deny"\n')
    r = sr.load_site_policy(f).rules[0]
    assert r.actor_pattern == "*"
    assert "data.ingest" in r.name and "deny" in r.name


def test_multiple_rules_and_wildcard_tool(tmp_path):
    f = _write(
        tmp_path / "p.toml",
        '[[rule]]\ntool="*"\ndisposition="propose"\n'
        '[[rule]]\ntool="press.publish"\ndisposition="deny"\n',
    )
    rules = sr.load_site_policy(f).rules
    assert len(rules) == 2
    assert rules[0].resource_pattern.value == "tool://*"
    assert rules[1].disposition == "deny"


# ---- malformed -> loud SiteRuleError (never a silent/partial policy) ------


def test_invalid_disposition_raises(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="x.y"\ndisposition="yolo"\n')
    with pytest.raises(sr.SiteRuleError, match="invalid disposition"):
        sr.load_site_policy(f)


def test_missing_tool_raises(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ndisposition="deny"\n')
    with pytest.raises(sr.SiteRuleError, match="missing required 'tool'"):
        sr.load_site_policy(f)


def test_non_integer_priority_raises(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="x.y"\ndisposition="deny"\npriority="high"\n')
    with pytest.raises(sr.SiteRuleError, match="priority must be an integer"):
        sr.load_site_policy(f)


def test_malformed_toml_raises(tmp_path):
    f = _write(tmp_path / "p.toml", "this is not = valid = toml [[[")
    with pytest.raises(sr.SiteRuleError):
        sr.load_site_policy(f)


def test_rule_must_be_an_array_of_tables(tmp_path):
    f = _write(tmp_path / "p.toml", 'rule = "not a list"\n')
    with pytest.raises(sr.SiteRuleError, match="array of tables"):
        sr.load_site_policy(f)


# ---- end-to-end: a loaded rule actually decides --------------------------


def test_a_loaded_deny_rule_denies_through_decide(tmp_path):
    """The loaded Rule is a real authz Rule: it wins over the open permit."""
    from axiom.governance import Decision
    from axiom.infra import authority

    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="write_file"\ndisposition="deny"\n')
    ctx = authority.build_tool_context(session_factory=None)  # open posture
    for rule in sr.load_site_policy(f).rules:
        ctx.add_rule(rule)
    env = authority.build_tool_envelope("write_file", {}, "@alice:axiom")
    verdict = authority.decide_tool_call(env, ctx)
    assert verdict.decision == Decision.DENY


# ---- ADR-114 §3: surface-scoped rules ------------------------------------


def test_surface_key_scopes_the_rule(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="press.publish"\ndisposition="propose"\nsurface="mcp"\n')
    r = sr.load_site_policy(f).rules[0]
    assert r.surface_pattern == "mcp"


def test_surface_defaults_to_none_any_surface(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="press.publish"\ndisposition="propose"\n')
    assert sr.load_site_policy(f).rules[0].surface_pattern is None


def test_invalid_surface_raises(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="x.y"\ndisposition="deny"\nsurface="carrier-pigeon"\n')
    with pytest.raises(sr.SiteRuleError, match="invalid surface"):
        sr.load_site_policy(f)


def test_wildcard_surface_is_none_equivalent(tmp_path):
    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="x.y"\ndisposition="deny"\nsurface="*"\n')
    # "*" is accepted; the matcher treats it as any-surface (like None)
    assert sr.load_site_policy(f).rules[0].surface_pattern == "*"


def test_surface_scoped_rule_matches_only_that_surface(tmp_path):
    """The loaded surface-scoped Rule matches an mcp envelope, not a cli one,
    and never a surface-less one (fail-closed on scope)."""
    from axiom.governance import Decision
    from axiom.infra import authority

    f = _write(tmp_path / "p.toml", '[[rule]]\ntool="press.publish"\ndisposition="deny"\nsurface="mcp"\n')
    ctx = authority.build_tool_context(session_factory=None)
    for rule in sr.load_site_policy(f).rules:
        ctx.add_rule(rule)

    mcp_env = authority.build_tool_envelope("press.publish", {}, "@a:axiom", surface="mcp")
    cli_env = authority.build_tool_envelope("press.publish", {}, "@a:axiom", surface="cli")
    bare_env = authority.build_tool_envelope("press.publish", {}, "@a:axiom")

    assert authority.decide_tool_call(mcp_env, ctx).decision == Decision.DENY
    # cli + surface-less fall through to the open permit
    assert authority.decide_tool_call(cli_env, ctx).decision == Decision.PERMIT
    assert authority.decide_tool_call(bare_env, ctx).decision == Decision.PERMIT
