# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Does a name Axiom publishes collide with something the calling harness owns?

The question that started this (Ben, 2026-10-08): with the Axiom MCP configured,
``/renew`` typed into Claude Code, and an Axiom tool also called ``renew`` —
which answers, and how do we make the answer good?

The measured answers live in ``harness_profiles.toml``, each marked by how it
was established. This module is the standing guard built on them:

- the detector is a pure function and has negative controls, so a green lint
  means the detector looked and found nothing, not that it cannot see;
- the lint runs the detector over the *real* exposed surface (composed server +
  memory server) against every supported harness profile and names the tool,
  the harness and the rule when it fails;
- the server-side coercion hook is tested end to end with a real in-process MCP
  client that identifies itself in ``initialize``.
"""

from __future__ import annotations

import dataclasses
import textwrap

import pytest

from axiom.extensions.builtins.mcp import harness_collisions as hc
from axiom.extensions.builtins.mcp.harness_collisions import (
    Exposed,
    find_collisions,
    load_profiles,
)

PROFILES = load_profiles()
CLAUDE = PROFILES["claude-code"]


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


def _rules(findings):
    return {f.rule for f in _errors(findings)}


# ---------------------------------------------------------------------------
# The profile data is honest about what it knows.
# ---------------------------------------------------------------------------


def test_every_supported_harness_has_a_profile():
    """A harness Axiom can install into must have a row, or it is unguarded."""
    from axiom.extensions.builtins.mcp.install import supported_tools

    missing = sorted(set(supported_tools()) - set(PROFILES))
    assert not missing, f"harnesses with no entry in harness_profiles.toml: {missing}"


def test_every_claim_carries_a_known_provenance_mark_and_evidence():
    allowed = {"verified-by-run", "from-binary", "from-docs", "unverified"}
    for name, p in PROFILES.items():
        raw = p.raw
        for section in ("tool_name", "prompt_slash", "noninteractive"):
            block = raw[section]
            assert block["status"] in allowed, f"{name}.{section}: {block['status']!r}"
            assert block.get("evidence"), f"{name}.{section} has no evidence string"
        assert set(raw["slash"]) <= {
            "verified_by_run", "from_binary", "from_docs", "unverified",
        }, f"{name}.slash has a key that is not a provenance mark"


def test_a_harness_that_was_not_run_claims_nothing_verified():
    """The rule that matters most: nothing is presented as fact that was not
    observed. A profile whose binary was absent or broken when the file was
    written may not carry a verified mark anywhere."""
    for name, p in PROFILES.items():
        if p.raw["installed_when_probed"] and p.raw.get("probed_version"):
            continue
        for section in ("tool_name", "prompt_slash", "noninteractive"):
            assert p.raw[section]["status"] == "unverified", (
                f"{name} was not driven, yet {section} is marked "
                f"{p.raw[section]['status']!r}"
            )
        assert not p.raw["slash"].get("verified_by_run"), name
        assert not p.raw["slash"].get("from_binary"), name


def test_session_owning_commands_are_real_axiom_chat_commands():
    """The list is data, so it can rot. It may only name commands the chat REPL
    really has, or the rule it feeds would guard an imaginary command."""
    from axiom.extensions.builtins.chat.commands import CHAT_META_COMMANDS

    real = {c.split()[0].lstrip("/").lower() for c in CHAT_META_COMMANDS}
    owning = set(hc.session_owning_commands())
    assert owning, "no session-owning commands declared"
    assert owning <= real, f"not chat commands: {sorted(owning - real)}"


# ---------------------------------------------------------------------------
# The detector, on synthetic input, with a negative control for each rule.
# ---------------------------------------------------------------------------


def test_a_clean_namespaced_tool_is_not_flagged():
    ok = Exposed("axiom_demo__lookup", "tool", "axiom", "Look a thing up.")
    assert _errors(find_collisions([ok], CLAUDE)) == []


def test_rule_i_a_name_equal_to_a_reserved_command_is_an_exact_collision():
    """Negative control for the property the lint exists for."""
    bare = Exposed("compact", "tool", "axiom", "x")  # /compact is verified-by-run
    found = find_collisions([bare], CLAUDE)
    assert "reserved-exact" in _rules(found), [str(f) for f in found]
    # and it names the tool, the harness and the rule
    msg = str(next(f for f in found if f.rule == "reserved-exact"))
    assert "compact" in msg and "claude-code" in msg and "reserved-exact" in msg


def test_rule_i_an_unprefixed_harness_collides_where_a_prefixing_one_cannot():
    """The prefix is what protects. Same name, two naming schemes."""
    renew = Exposed("renew", "tool", "axiom", "x")
    prefixing = dataclasses.replace(CLAUDE, reserved={"renew": "verified-by-run"})
    flat = dataclasses.replace(
        prefixing, tool_template="{tool}", tool_status="verified-by-run"
    )
    on_prefixing = [f for f in find_collisions([renew], prefixing) if f.rule == "reserved-exact"]
    on_flat = [f for f in find_collisions([renew], flat) if f.rule == "reserved-exact"]
    assert on_flat and "harness shows it as 'renew'" in on_flat[0].detail
    # on the prefixing harness the harness-visible name differs, and the finding says so
    assert on_prefixing and "prefixes it" in on_prefixing[0].detail


def test_rule_i_a_prompt_is_compared_by_its_slash_form():
    p = Exposed("mcp__axiom__resume", "prompt", "axiom", "x")  # contrived: slash form
    # claude shows prompts as /mcp__<server>__<prompt>; build a reserved name equal to that
    prof = dataclasses.replace(
        CLAUDE, reserved={"mcp__axiom__mcp__axiom__resume": "verified-by-run"}
    )
    assert "reserved-exact" in _rules(find_collisions([p], prof))


def test_rule_ii_a_bare_verb_is_flagged_in_every_profile():
    for name, prof in PROFILES.items():
        bare = Exposed("renew", "tool", "axiom", "x")
        assert "bare-verb" in _rules(find_collisions([bare], prof)), name


def test_rule_ii_a_bare_prompt_name_is_flagged_and_a_namespaced_one_is_not():
    assert "bare-verb" in _rules(find_collisions([Exposed("renew", "prompt", "axiom")], CLAUDE))
    assert "bare-verb" not in _rules(
        find_collisions([Exposed("guided.how-to", "prompt", "axiom")], CLAUDE)
    )


def test_rule_iii_two_names_that_flatten_to_one_collide():
    """`.` and `_` both become `_` in Claude Code (verified-by-run)."""
    a = Exposed("axiom_demo__a.b", "tool", "axiom", "x")
    b = Exposed("axiom_demo__a_b", "tool", "axiom", "x")
    found = find_collisions([a, b], CLAUDE)
    assert "transform-collision" in _rules(found), [str(f) for f in found]
    # control: distinct names do not
    c = Exposed("axiom_demo__a_c", "tool", "axiom", "x")
    assert "transform-collision" not in _rules(find_collisions([a, c], CLAUDE))


def test_rule_iii_truncation_merges_long_siblings_only_on_a_trusted_limit():
    long_a = Exposed("axiom_demo__" + "x" * 80 + "_one", "tool", "axiom", "x")
    long_b = Exposed("axiom_demo__" + "x" * 80 + "_two", "tool", "axiom", "x")
    trusted = dataclasses.replace(CLAUDE, max_len=40, max_len_status="from-docs")
    assert "transform-collision" in _rules(find_collisions([long_a, long_b], trusted))
    # the same data with an unverified limit warns instead of failing
    unproven = dataclasses.replace(CLAUDE, max_len=40, max_len_status="unverified")
    found = find_collisions([long_a, long_b], unproven)
    assert _errors(found) == [] and any(f.rule == "transform-collision" for f in found)


def test_rule_iv_a_shadowing_verb_needs_a_clause_saying_what_it_is_not():
    undescribed = Exposed("axiom_demo__compact", "tool", "axiom", "Shrink the thing.")
    assert "verb-shadow" in _rules(find_collisions([undescribed], CLAUDE))

    described = Exposed(
        "axiom_demo__compact", "tool", "axiom",
        "Shrink the thing. Not the harness's own /compact, which summarises a chat.",
    )
    assert "verb-shadow" not in _rules(find_collisions([described], CLAUDE))


def test_rule_iv_a_clause_that_does_not_negate_does_not_count():
    """Mentioning the command is not disambiguating it."""
    mentions = Exposed(
        "axiom_demo__compact", "tool", "axiom", "Like /compact but for the thing."
    )
    assert "verb-shadow" in _rules(find_collisions([mentions], CLAUDE))


def test_rule_iv_a_near_miss_is_caught_too():
    near = Exposed("axiom_demo__compat", "tool", "axiom", "Shrink the thing.")  # 1 edit
    assert "verb-shadow" in _rules(find_collisions([near], CLAUDE))
    far = Exposed("axiom_demo__lookup", "tool", "axiom", "Shrink the thing.")
    assert "verb-shadow" not in _rules(find_collisions([far], CLAUDE))


def test_rule_iv_the_last_word_of_a_compound_verb_counts():
    """`kit_init` is read as `init` by a person and by a model."""
    e = Exposed("axiom_demo__kit_init", "tool", "axiom", "Scaffold a kit.")
    assert "verb-shadow" in _rules(find_collisions([e], CLAUDE))


def test_an_unverified_reserved_name_advises_and_never_fails():
    e = Exposed("axiom_demo__status", "tool", "axiom", "Report.")
    found = find_collisions([e], PROFILES["opencode"])  # /status is unverified there
    shadows = [f for f in found if f.rule == "verb-shadow"]
    assert shadows and all(f.severity == "advisory" for f in shadows)


def test_rule_v_session_owning_commands_are_never_exposed():
    for verb in hc.session_owning_commands():
        e = Exposed(f"axiom_chat__{verb}", "tool", "axiom", "x")
        found = find_collisions([e], CLAUDE)
        assert "session-owning" in _rules(found), verb
    ok = Exposed("axiom_chat__lookup", "tool", "axiom", "x")
    assert "session-owning" not in _rules(find_collisions([ok], CLAUDE))


def test_bare_strings_are_accepted_as_tool_names():
    assert "bare-verb" in _rules(find_collisions(["renew"], CLAUDE))


# ---------------------------------------------------------------------------
# The lint: the real surface, every supported harness.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_surface():
    return hc.exposed_surface()


def _lint(exposed, profiles=PROFILES):
    """One precise line per failure: tool, harness, rule."""
    return [
        str(f)
        for prof in profiles.values()
        for f in _errors(find_collisions(exposed, prof))
    ]


def test_the_real_surface_is_not_empty_and_covers_both_servers(real_surface):
    servers = {e.server for e in real_surface}
    assert servers == {"axiom", "axiom-memory"}, servers
    assert any(e.name == "axiom_memory_append" for e in real_surface)
    assert len(real_surface) > 10


def test_no_exposed_name_collides_with_any_supported_harness(real_surface):
    problems = _lint(real_surface)
    assert not problems, (
        "an exposed MCP name collides with a harness-reserved command, is a bare "
        "verb, or merges with another name. Fix the NAME or add a description "
        "clause saying what it is not (see the rules in "
        "docs/specs/spec-builtin-mcp-server.md, 'Name collisions with a harness').\n  "
        + "\n  ".join(problems)
    )


def test_negative_control_renaming_a_real_tool_to_a_reserved_name_fails_the_lint(real_surface):
    """Mutation check: the lint above must be able to fail for this property."""
    victim = next(e for e in real_surface if e.kind == "tool" and e.server == "axiom")
    for reserved in ("compact", "resume", "init"):
        mutated = [
            dataclasses.replace(e, name=reserved) if e is victim else e
            for e in real_surface
        ]
        problems = _lint(mutated)
        assert any(reserved in p and "claude-code" in p for p in problems), (
            f"renaming {victim.name} to {reserved!r} did not fail the lint:\n"
            + "\n".join(problems[:5])
        )
    # and the failure is attributed to a tool, a harness and a rule
    mutated = [dataclasses.replace(e, name="compact") if e is victim else e for e in real_surface]
    line = next(p for p in _lint(mutated) if "reserved-exact" in p and "claude-code" in p)
    assert "tool=compact" in line


def test_negative_control_stripping_a_disambiguating_clause_fails_the_lint(real_surface):
    shadowing = [
        e for e in real_surface
        if e.name in ("axiom_data__kit_init", "axiom_attest__verify")
    ]
    assert len(shadowing) == 2, "the two known verb-shadow tools are no longer exposed"
    assert _lint(shadowing) == [], "their descriptions should already disambiguate"
    stripped = [dataclasses.replace(e, description="Does a thing.") for e in shadowing]
    problems = _lint(stripped)
    assert any("axiom_data__kit_init" in p and "verb-shadow" in p for p in problems)
    assert any("axiom_attest__verify" in p and "verb-shadow" in p for p in problems)


def test_the_surface_is_read_from_the_servers_not_from_a_copy(real_surface):
    from axiom.extensions.builtins.memory import mcp_server as memory_server

    exposed_memory = {e.name for e in real_surface if e.server == "axiom-memory"}
    assert exposed_memory == {t.name for t in memory_server._TOOLS}


# ---------------------------------------------------------------------------
# Server-side coercion: hide / rename per harness, default no change.
# ---------------------------------------------------------------------------


def test_default_coercion_changes_nothing_for_any_shipped_profile():
    """No shipped profile hides or renames a tool, so no tool name changes."""
    for name, prof in PROFILES.items():
        assert prof.hide == frozenset() and prof.rename == {}, name
    tools = ["a", "b"]
    assert hc.apply_client_coercion(tools, CLAUDE) is tools
    assert hc.apply_client_coercion(tools, None) is tools


def test_the_client_is_identified_by_the_installer_stamp_then_by_the_handshake(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_CLIENT", "opencode")
    assert hc.resolve_client_name(client_info_name="claude-code") == "opencode"
    monkeypatch.delenv("AXIOM_MCP_CLIENT", raising=False)
    assert hc.resolve_client_name(client_info_name="claude-code") == "claude-code"
    monkeypatch.setenv("AXIOM_MCP_CLIENT", "unknown")
    assert hc.resolve_client_name(client_info_name="codex-mcp-client") == "codex-mcp-client"
    assert hc.harness_for_client("codex-mcp-client").name == "codex"
    assert hc.harness_for_client("something-new") is None


_COERCING = textwrap.dedent(
    """
    schema = 1
    [harness.claude-code]
    client_names = ["claude-code"]
    [harness.claude-code.tool_name]
    template = "mcp__{server}__{tool}"
    status = "verified-by-run"
    [harness.claude-code.prompt_slash]
    surfaces = true
    template = "/mcp__{server}__{prompt}"
    status = "verified-by-run"
    [harness.claude-code.slash]
    verified_by_run = []
    [harness.claude-code.coerce]
    hide = ["axiom_node__hooks_list"]
    rename = { "axiom_mcp__client_capabilities" = "axiom_mcp__which_clients" }
    [harness.other]
    client_names = ["other"]
    [harness.other.tool_name]
    template = "{tool}"
    status = "unverified"
    [harness.other.prompt_slash]
    surfaces = false
    template = "/{prompt}"
    status = "unverified"
    [harness.other.slash]
    unverified = []
    """
)


def _listing_and_call(client_name: str, call: str | None = None):
    import asyncio

    from mcp.client.client import Client
    from mcp.types import Implementation

    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
    from axiom.extensions.builtins.mcp.server import build_server

    server = build_server(AggregationRegistry.from_node().build())

    async def go():
        async with Client(server, client_info=Implementation(name=client_name, version="0")) as c:
            tools = [t.name for t in (await c.list_tools()).tools]
            result = await c.call_tool(call, {}) if call else None
            return tools, result

    return asyncio.run(go())


@pytest.fixture
def coercing_profiles(tmp_path, monkeypatch):
    path = tmp_path / "profiles.toml"
    path.write_text(_COERCING, encoding="utf-8")
    monkeypatch.setattr(hc, "PROFILES_PATH", path)
    monkeypatch.delenv("AXIOM_MCP_CLIENT", raising=False)


def test_a_declared_harness_gets_the_hidden_and_renamed_tools_over_the_wire(
    coercing_profiles, tmp_axiom_home
):
    tools, result = _listing_and_call("claude-code", "axiom_mcp__which_clients")
    assert "axiom_node__hooks_list" not in tools
    assert "axiom_mcp__client_capabilities" not in tools
    assert "axiom_mcp__which_clients" in tools
    # the renamed tool still routes to its real handler
    assert result is not None and not result.is_error, result
    assert "clients" in result.content[0].text


def test_every_other_client_sees_the_unchanged_surface(coercing_profiles, tmp_axiom_home):
    for name in ("other", "never-heard-of-it"):
        tools, _ = _listing_and_call(name)
        assert "axiom_node__hooks_list" in tools, name
        assert "axiom_mcp__client_capabilities" in tools, name
        assert "axiom_mcp__which_clients" not in tools, name


def test_without_coercion_rows_the_shipped_listing_equals_the_surface(tmp_axiom_home):
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    expected = sorted(t.name for t in AggregationRegistry.from_node().build().tools)
    tools, _ = _listing_and_call("claude-code")
    assert sorted(tools) == expected


def test_the_client_name_is_read_under_both_sdk_spellings():
    """mcp 1.x says `clientInfo`, mcp 2.x says `client_info`. Reading one
    spelling made attribution return nothing, silently, on the other."""
    from types import SimpleNamespace as NS

    from axiom.extensions.builtins.mcp.server import _client_info

    new = NS(session=NS(client_params=NS(client_info=NS(name="claude-code"))))
    old = NS(session=NS(client_params=NS(clientInfo=NS(name="claude-code"))))
    none = NS(session=NS(client_params=None))
    assert _client_info(new).name == "claude-code"
    assert _client_info(old).name == "claude-code"
    assert _client_info(none) is None
    assert _client_info(object()) is None
