# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A skill that declares a surface must have that surface.

The four gold verbs were registered with ``surfaces=("cli", "mcp",
"agent_tool")`` and no ``add_parser`` behind the first one. Every inventory
counted them as CLI capabilities; the terminal had never heard of them. The
declaration is what tooling reads, so a declaration nothing backs is worse
than an absent one — it makes the gap invisible to exactly the audit that
would find it.

This is the guard for the class, not the instance.
"""

from __future__ import annotations

import argparse

from axiom.infra.skills import SkillRegistry

from .. import cli as data_cli
from .. import skills as data_skills

_NAMESPACE = "data."


def _parser_verbs() -> set[str]:
    """Every verb the data_platform CLI actually parses."""
    parser = data_cli._parser()
    verbs: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            verbs.update(action.choices)
    return verbs


def _registry() -> SkillRegistry:
    registry = SkillRegistry()
    data_skills.bind(registry)
    return registry


def _cli_skills() -> dict[str, object]:
    registry = _registry()
    out = {}
    for name in registry.list("data"):
        spec = registry.spec(name)
        if spec is not None and "cli" in (spec.surfaces or ()):
            out[name] = spec
    return out


def test_every_skill_that_declares_cli_has_a_parser():
    backed = {skill for _, skill, error in _dispatches() if error is None}
    missing = sorted(name for name in _cli_skills() if name[len(_NAMESPACE) :] not in backed)
    assert not missing, (
        "these skills declare a `cli` surface that no parser backs — either add "
        f"the parser or drop `cli` from the spec: {missing}"
    )


def _dispatches() -> list[tuple[str, str | None, str | None]]:
    """``(spelling, skill, error)`` for every verb, through the CLI's own resolver.

    Resolved the way dispatch resolves it rather than by assuming a verb and its
    skill share a name: ``tables`` is a friendly spelling of ``catalog``, and
    ``retrieval save`` is the skill ``retrieval_save``.
    """
    out = []
    for action in data_cli._parser()._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for verb, sub in action.choices.items():
            nested = [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)]
            if not nested:
                ns = argparse.Namespace(verb=verb, dry_run=False)
                out.append((verb, *data_cli.resolve_skill(ns)))
                continue
            for inner in nested:
                for sub_verb in inner.choices:
                    ns = argparse.Namespace(verb=verb, dry_run=False, **{inner.dest: sub_verb})
                    out.append((f"{verb} {sub_verb}", *data_cli.resolve_skill(ns)))
    return out


def test_every_parsed_verb_resolves_to_a_registered_skill():
    """The other direction: a verb at the terminal that dispatches to nothing."""
    registry = _registry()
    unresolved = sorted(
        spelling
        for spelling, skill, error in _dispatches()
        if error is None and not registry.has(f"{_NAMESPACE}{skill}")
    )
    assert not unresolved, f"parsed verbs with no registered skill: {unresolved}"


def test_the_resolver_check_can_fail():
    """A guard that walks the parser has to be shown to find something."""
    registry = SkillRegistry()  # nothing bound: every verb is unresolved
    assert any(
        error is None and not registry.has(f"{_NAMESPACE}{skill}")
        for _, skill, error in _dispatches()
    )


def test_the_medallion_verbs_are_the_regression_this_guards():
    """Named explicitly, so deleting the parsers fails here and not only in a sweep."""
    parsed = _parser_verbs()
    for verb in ("catalog", "describe", "freshness", "sample", "aggregate", "series"):
        assert verb in parsed, f"{verb} lost its parser"


def test_one_introspection_vocabulary_across_every_tier():
    """The tier is an argument, never part of the verb name.

    Six verbs split these four questions across two naming schemes, so a
    caller had to know which medallion they were on before they could phrase
    the question.
    """
    parsed = _parser_verbs()
    tier_named = sorted(
        v for v in parsed if v.startswith(("gold-", "bronze-", "silver-"))
    )

    assert tier_named == [], f"the tier belongs in --tier, not the verb name: {tier_named}"


def test_the_answering_verbs_expose_the_population_controls():
    """Refusing a blended answer is only usable if the way through is reachable."""
    parser = data_cli._parser()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for verb in ("aggregate", "series"):
            flags = {
                opt
                for sub_action in action.choices[verb]._actions
                for opt in sub_action.option_strings
            }
            assert "--group-by" in flags, f"{verb} cannot split a mixed population"
            assert "--allow-mixed" in flags, f"{verb} cannot state an intended blend"
            assert "--filter" in flags, f"{verb} cannot narrow to one population"
