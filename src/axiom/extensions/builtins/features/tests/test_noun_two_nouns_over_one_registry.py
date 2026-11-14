# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Two command nouns over one registry (owner decision 2026-11-12, amending ADR-254 and ADR-210).

``features`` covers what a node runs: extensions, functions, the role, agents and
maintenance policies. ``flags`` covers feature flags: every flag verb. Both read and
write the one registry; nothing below the surface changes.

NOUN-A1  ``features`` lists no flag; it points at ``flags`` instead (CLI, skill, MCP).
NOUN-A2  ``flags`` is a visible noun again: not internal, no deprecation note.
NOUN-A3  ``flags`` carries every flag verb, each through a ``flags.*`` skill.
NOUN-A4  ``features`` no longer sets a flag: its moved verbs and a flag name point at ``flags``.
NOUN-A5  MCP follows the split; a flag tool that existed only as ``axiom_features__<x>``
         keeps that name for one release as a deprecated alias.
NOUN-A6  HTTP: ``/api/v1/flags`` is the canonical flags endpoint (read, set, explain, kill),
         and ``/api/v1/features`` still returns the whole registry with every field it had.
NOUN-A7  No user-facing CLI output, help text or HTTP error text says "switch".
NOUN-A8  No doc teaches ``features <flag verb>``; ADR-254 and ADR-210 carry the amendment.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import tomllib
from pathlib import Path

import pytest

from axiom.extensions.builtins.features import cli as features_cli
from axiom.extensions.builtins.features.tests.conftest import mcp, web
from axiom.extensions.builtins.flags import cli as flags_cli

BUILTINS = Path(__file__).resolve().parents[2]
REPO = Path(__file__).resolve().parents[6]

#: Every flag verb: its skill, under ``flags``.
FLAG_SKILLS = (
    "list", "resolve", "set", "explain", "kill", "cohorts", "cohort_preview", "cohort_set",
    "cohort_delete", "rollout_preview", "rollout_set", "cohort_import_preview", "cohort_import",
    "cohort_refresh", "stage_plan_preview", "stage_plan_set", "stage_plan_halt", "approve",
    "changes", "change_cancel", "rollout_tick",
)
#: Flag tools that existed as ``axiom_features__<x>`` and are kept one release, deprecated.
DEPRECATED = tuple(s for s in FLAG_SKILLS if s not in ("list", "set"))
#: What ``features`` keeps: the node.
NODE_SKILLS = ("list", "set", "enable", "disable", "role", "agents", "maintenance", "record_of_truth")
#: The CLI verbs that moved from ``features`` to ``flags``.
MOVED_VERBS = ("resolve", "explain", "kill", "cohort", "rollout", "stage", "approve", "changes", "cancel", "tick")


def _out(capsys) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


# ------------------------------------------------------------------ NOUN-A1


def test_noun_a1_features_lists_what_a_node_runs_and_points_at_flags(switchboard, capsys):
    capsys.readouterr()
    assert features_cli.main(["--json"]) == 0
    value = json.loads(_out(capsys)[0])
    assert {r["kind"] for r in value["rows"]} == {"extension", "function"}
    assert value["flags"]["count"] == 3 and value["flags"]["skill"] == "flags.list"
    assert value["flags"]["command"].endswith("flags list")

    assert features_cli.main([]) == 0
    out, _ = _out(capsys)
    assert "timeline" not in out and "library" not in out
    assert re.search(r"3 feature flags.*`\S+ flags list`", out), out

    assert features_cli.main(["list", "--kind", "flag"]) == 0
    out, _ = _out(capsys)
    assert "timeline" not in out and "flags list" in out

    listed = mcp("axiom_features__list", {}, switchboard["state"])
    assert listed["ok"] and {r["kind"] for r in listed["value"]["rows"]} == {"extension", "function"}


# ------------------------------------------------------------------ NOUN-A2


def test_noun_a2_flags_is_a_visible_noun_with_no_deprecation_note(switchboard, capsys):
    manifest = tomllib.loads((BUILTINS / "flags" / "axiom-extension.toml").read_text())
    (cmd,) = [p for p in manifest["extension"]["provides"] if p["kind"] == "cmd"]
    assert cmd["noun"] == "flags" and cmd["tier"] != "internal"
    assert "alias" not in cmd["description"].lower() and "0.71.0" not in cmd["description"]

    from axiom.cli.help_engine import filter_commands

    commands = {"flags": {"tier": cmd["tier"], "extension": "flags"},
                "features": {"tier": "starter", "extension": "features"}}
    assert set(filter_commands(commands, show_all=True)) == {"features", "flags"}

    capsys.readouterr()
    assert flags_cli.main([]) == 0
    out, err = _out(capsys)
    assert err == "" and "timeline" in out and "library" in out


# ------------------------------------------------------------------ NOUN-A3


@pytest.mark.parametrize(
    ("argv", "skill"),
    [
        (["list"], "flags.list"),
        (["resolve", "timeline"], "flags.resolve"),
        (["set", "timeline", "on", "--reason", "r", "--expires", "2099-01-01T00:00:00Z"], "flags.set"),
        (["clear", "timeline"], "flags.set"),
        (["explain", "timeline"], "flags.explain"),
        (["kill", "timeline", "--reason", "r"], "flags.kill"),
        (["cohort", "list"], "flags.cohorts"),
        (["cohort", "set", "beta", "--principal", "p1"], "flags.cohort_preview"),
        (["cohort", "set", "beta", "--principal", "p1", "--apply", "--reason", "r"], "flags.cohort_set"),
        (["cohort", "delete", "beta", "--apply", "--reason", "r"], "flags.cohort_delete"),
        (["cohort", "import", "beta", "--file", "m.txt"], "flags.cohort_import_preview"),
        (["cohort", "import", "beta", "--file", "m.txt", "--apply", "--reason", "r"], "flags.cohort_import"),
        (["cohort", "refresh", "beta"], "flags.cohort_refresh"),
        (["rollout", "set", "timeline", "--rules", "[]"], "flags.rollout_preview"),
        (["rollout", "set", "timeline", "--rules", "[]", "--apply", "--reason", "r"], "flags.rollout_set"),
        (["stage", "set", "timeline", "--plan", "{}"], "flags.stage_plan_preview"),
        (["stage", "set", "timeline", "--plan", "{}", "--apply", "--reason", "r"], "flags.stage_plan_set"),
        (["stage", "halt", "timeline", "--reason", "r"], "flags.stage_plan_halt"),
        (["approve", "c1"], "flags.approve"),
        (["changes"], "flags.changes"),
        (["cancel", "c1", "--reason", "r"], "flags.change_cancel"),
        (["tick"], "flags.rollout_tick"),
    ],
)
def test_noun_a3_every_flag_verb_reaches_a_flags_skill(argv, skill, monkeypatch):
    from axiom.infra.skills import SkillResult

    called = []

    def fake(capability, params):
        called.append(capability)
        return SkillResult(ok=False, errors=["stop here"])

    monkeypatch.setattr(flags_cli, "run_capability", fake)
    assert flags_cli.main(argv) == 1
    assert called == [skill]


def test_noun_a3_a_flag_write_is_audited_under_its_flags_name(switchboard, capsys):
    from axiom.infra import audit_trail

    assert flags_cli.main(["set", "timeline", "on", "--reason", "trial"]) == 0
    assert flags_cli.main(["kill", "timeline", "--reason", "incident"]) == 0
    skills = [r.get("skill") for r in audit_trail.read_chain(audit_trail.audit_path(switchboard["state"]))]
    assert "flags.set" in skills and "flags.kill" in skills
    assert not any(str(s).startswith("features.") for s in skills)


# ------------------------------------------------------------------ NOUN-A4


@pytest.mark.parametrize("verb", MOVED_VERBS)
def test_noun_a4_a_moved_verb_points_at_flags(verb, switchboard, capsys):
    capsys.readouterr()
    assert features_cli.main([verb, "timeline"]) == 2
    out, err = _out(capsys)
    assert f"flags {verb}" in err and out == ""


def test_noun_a4_features_never_sets_a_flag(switchboard, capsys):
    capsys.readouterr()
    for argv in (["set", "timeline", "on"], ["enable", "timeline"], ["disable", "timeline"],
                 ["clear", "timeline"]):
        assert features_cli.main(argv) == 1, argv
        err = _out(capsys)[1]
        assert "feature flag" in err and "flags set timeline" in err, (argv, err)
    assert switchboard["store"].rows() == []
    refused = mcp("axiom_features__set", {"key": "timeline", "value": "on"}, switchboard["state"])
    assert not refused["ok"] and "axiom_flags__set" in json.dumps(refused)


# ------------------------------------------------------------------ NOUN-A5


def test_noun_a5_mcp_follows_the_split_with_deprecated_aliases_for_one_release(switchboard):
    from axiom.extensions.builtins.features.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    for name in FLAG_SKILLS:
        spec = reg.spec(f"flags.{name}")
        assert "alias" not in spec.description.lower() and "0.71.0" not in spec.description, name
    for name in NODE_SKILLS:
        assert reg.has(f"features.{name}"), name
    for name in DEPRECATED:
        spec = reg.spec(f"features.{name}")
        assert spec.description.startswith(f"Deprecated: use flags.{name}"), name
        assert "0.71.0" in spec.description, name
        assert spec.fn is reg.spec(f"flags.{name}").fn

    tools = skill_tool_contribution(
        reg, ctx_factory=lambda: SkillContext(registry=reg, state_dir=switchboard["state"], logger=logging.getLogger("t"))
    ).dispatch
    for name in FLAG_SKILLS:
        flags_spec = reg.spec(f"flags.{name}")
        if "mcp" in flags_spec.surfaces:
            assert f"axiom_flags__{name}" in tools, name
    for name in ("list", "set", "enable", "disable", "role"):
        assert f"axiom_features__{name}" in tools

    state = switchboard["state"]
    new = mcp("axiom_flags__explain", {"key": "timeline"}, state)
    old = mcp("axiom_features__explain", {"key": "timeline"}, state)
    assert new["ok"] and old["ok"] and new["value"]["value"] == old["value"]["value"]


def test_noun_a5_the_scheduler_runs_the_flags_tick():
    manifest = tomllib.loads((BUILTINS / "features" / "axiom-extension.toml").read_text())
    (tick,) = manifest["extension"]["schedule"]
    assert tick["action"] == "flags.rollout_tick"


def test_noun_a5_flag_changes_keep_their_kind_in_the_change_log():
    from axiom.extensions.builtins.settings import changes

    for skill, kind in (("flags.kill", "kill-switch"), ("flags.cohort_set", "cohort"),
                        ("flags.rollout_set", "rollout"), ("flags.stage_plan_set", "stage-plan"),
                        ("flags.approve", "approval"), ("flags.change_cancel", "scheduled-change")):
        assert changes._KIND_BY_SKILL[skill] == kind
        assert changes._KIND_BY_SKILL[skill.replace("flags.", "features.")] == kind


# ------------------------------------------------------------------ NOUN-A6

#: Every field appkit's ``FlagProvider`` reads from ``GET /api/v1/flags``.
APPKIT_FIELDS = {"flags", "sources", "metadata", "can_set", "environment"}


def test_noun_a6_flags_is_the_canonical_flags_endpoint(switchboard, monkeypatch):
    client = web(monkeypatch, roles=("site-admin",))
    body = client.get("/api/v1/flags").json()
    assert APPKIT_FIELDS <= set(body) and "switches" not in body
    assert body["flags"]["timeline"] == "off"

    set_ = client.patch("/api/v1/flags/timeline", json={"value": "on", "scope": "node", "reason": "trial",
                                                        "expires_at": "2099-01-01T00:00:00Z"})
    assert set_.status_code == 200, set_.text
    (row,) = switchboard["store"].rows("timeline")
    assert row["reason"] == "trial"

    explained = client.get("/api/v1/flags/timeline/explain")
    assert explained.status_code == 200 and explained.json()["value"] == "on"
    killed = client.post("/api/v1/flags/timeline/kill", json={"reason": "incident"})
    assert killed.status_code == 200, killed.text
    assert killed.json()["flags"]["timeline"] == "off"


def test_noun_a6_features_still_returns_the_whole_registry(switchboard, monkeypatch):
    client = web(monkeypatch, roles=("site-admin",))
    body = client.get("/api/v1/features").json()
    for field in (*APPKIT_FIELDS, "values", "waiting_on", "variant", "switches", "kinds", "scopes", "node", "drift"):
        assert field in body, field
    kinds = {r["kind"] for r in body["switches"]}
    assert kinds == {"extension", "function", "flag"}
    assert body["kinds"] == ["extension", "function", "flag"]
    patched = client.patch("/api/v1/features/timeline", json={"value": "soon", "scope": "node"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["flags"]["timeline"] == "soon"
    assert client.get("/api/v1/features/timeline/explain").status_code == 200


# ------------------------------------------------------------------ NOUN-A7

_SWITCH = re.compile(r"\bswitch(es|ed|board)?\b", re.IGNORECASE)


def _user_words(text: str) -> list[str]:
    """Every "switch" a person would read; the kill switch is the industry's name for the verb."""
    return _SWITCH.findall(re.sub(r"kill[ -]switch(es)?", "", text, flags=re.IGNORECASE))


def _helps(parser: argparse.ArgumentParser) -> str:
    out = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out.append(_helps(sub))
    return "\n".join(out)


def test_noun_a7_no_help_or_output_says_switch(switchboard, capsys, monkeypatch):
    assert _user_words(_helps(features_cli.build_parser())) == []
    assert _user_words(_helps(flags_cli.build_parser())) == []

    capsys.readouterr()
    texts = []
    for argv in ([], ["set", "widgets", "soon"], ["set", "widgets", "on", "--scope", "account", "--id", "a"],
                 ["enable", "nothing-here"], ["set", "nothing-here", "on"]):
        features_cli.main(argv)
        texts.extend(_out(capsys))
    assert _user_words("\n".join(texts)) == []

    client = web(monkeypatch, roles=())
    refused = client.patch("/api/v1/features/widgets", json={"value": "on", "scope": "node"})
    assert refused.status_code == 403 and _user_words(refused.text) == []
    refused = client.patch("/api/v1/flags/timeline", json={"value": "on", "scope": "node"})
    assert refused.status_code == 403 and _user_words(refused.text) == []


# ------------------------------------------------------------------ NOUN-A8

_TEACHES_OLD = re.compile(
    r"(?:^|[\s`(])features (resolve|explain|kill|cohort|rollout|stage|approve|changes|cancel|tick)\b"
    r"|features list --kind flag",
    re.MULTILINE,
)


def test_noun_a8_no_doc_teaches_features_for_a_flag_verb():
    docs = [p for p in (REPO / "docs").rglob("*.md") if not {"adrs", "working"} & set(p.relative_to(REPO / "docs").parts)]
    docs += list((REPO / "src" / "axiom" / "extensions" / "builtins").glob("*/README.md"))
    offenders = [f"{p.relative_to(REPO)}: {m.group(0)}" for p in docs for m in _TEACHES_OLD.finditer(p.read_text())]
    assert offenders == []


def test_noun_a8_the_adrs_carry_the_dated_amendment():
    adrs = REPO / "docs" / "adrs"
    for name in ("adr-254-one-switchboard-features-functions-and-flags-are-one-registry.md",
                 "adr-210-a-feature-flag-is-declared-by-its-extension-and-resolved-default-env-node-account-user.md"):
        head = "\n".join((adrs / name).read_text().splitlines()[:12])
        assert "Amended 2026-11-12 by the owner: two command nouns over one registry" in head, name
