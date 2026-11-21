# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/skills``: a skill's command line comes from the command tree when the tree knows it.

``<cli> <namespace> <verb>`` is a convention, not a rule: a kind under ``instrument``
keeps its own namespace (ADR-256 D5), so ``gauges.read`` is
``axi instrument gauges read``. The annotated command tree already records which
skill each verb wraps; when it does, that is the answer (spec-instrument INS-P3).
"""

from __future__ import annotations

from axiom.cli import command_space as cs
from axiom.extensions.builtins.chat.commands_ext import skills_cmd
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec


def _ok(p, c):
    return SkillResult(ok=True)


def _registry() -> SkillRegistry:
    r = SkillRegistry()
    r.register_skill(
        SkillSpec(name="gauges.read", fn=_ok, description="Read a channel.", side_effects=False,
                  surfaces=("cli", "mcp"))
    )
    r.register_skill(
        SkillSpec(name="press.draft", fn=_ok, description="Draft.", side_effects=True,
                  surfaces=("cli",))
    )
    return r


def _row(name: str, **kw) -> skills_cmd.SkillRow:
    reg = _registry()
    report = skills_cmd.list_skills(
        catalog=reg, chat_registry=SkillRegistry(), namespaces=[], offered=set(), **kw
    )
    return next(r for r in report.rows if r.name == name)


def test_the_tree_gives_the_command_a_person_types():
    row = _row("gauges.read", command_paths={"gauges.read": ("instrument", "gauges", "read")})
    assert row.cli_verb == "axi instrument gauges read"
    assert row.cli_verb_from_tree is True


def test_without_the_tree_the_convention_is_the_fallback():
    row = _row("press.draft", command_paths={})
    assert row.cli_verb == "axi press draft"
    assert row.cli_verb_from_tree is False


def test_the_paths_come_from_the_annotated_space():
    space = cs.Node(path=())
    noun = cs.Node(path=("instrument",))
    kind = cs.Node(path=("instrument", "gauges"))
    kind.children["read"] = cs.Node(path=("instrument", "gauges", "read"), skill="gauges.read")
    noun.children["gauges"] = kind
    space.children["instrument"] = noun
    assert cs.skill_paths(space) == {"gauges.read": ("instrument", "gauges", "read")}


def test_the_shortest_path_wins_when_two_verbs_wrap_one_skill():
    space = cs.Node(path=())
    a = cs.Node(path=("log",))
    a.children["tail"] = cs.Node(path=("log", "tail"), skill="log.tail")
    b = cs.Node(path=("log", "platform"))
    b.children["tail"] = cs.Node(path=("log", "platform", "tail"), skill="log.tail")
    a.children["platform"] = b
    space.children["log"] = a
    assert cs.skill_paths(space)["log.tail"] == ("log", "tail")


def test_the_detail_says_convention_only_when_it_is_the_convention():
    from_tree = skills_cmd.format_detail(
        _row("gauges.read", command_paths={"gauges.read": ("instrument", "gauges", "read")})
    )
    assert "axi instrument gauges read" in from_tree and "by convention" not in from_tree
    fallback = skills_cmd.format_detail(_row("press.draft", command_paths={}))
    assert "axi press draft" in fallback and "by convention" in fallback
