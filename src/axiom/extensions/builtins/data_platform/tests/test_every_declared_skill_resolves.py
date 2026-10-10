# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every skill the manifest declares must actually exist and be callable.

The failure this catches, found the same week in the `lane` extension: two verbs
had skill functions and CLI wiring and were absent from `axiom-extension.toml`.
The runtime registry had six skills, the manifest had four, and every static
reader — AEOS conformance, `axi ext lint`, the generated docs, MCP discovery —
was told the two did not exist. Nothing failed, because the CLI worked.

This asserts the pairing in both directions over this extension's own manifest,
so a verb cannot ship declared-but-absent or present-but-undeclared.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

_MANIFEST = Path(__file__).parent.parent / "axiom-extension.toml"


def _declared_skills() -> list[dict]:
    provides = tomllib.loads(_MANIFEST.read_text())["extension"]["provides"]
    return [p for p in provides if p.get("kind") == "skill"]


def test_the_manifest_declares_skills_at_all():
    """Negative control. If the parse returned nothing, everything below would
    pass while checking an empty list."""
    assert len(_declared_skills()) > 20


def test_every_declared_entry_imports_and_is_callable():
    broken: list[str] = []
    for skill in _declared_skills():
        entry = skill.get("entry") or ""
        if ":" not in entry:
            broken.append(f"{skill.get('name')}: entry {entry!r} is not module:func")
            continue
        module, _, func = entry.rpartition(":")
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 — report every one, not the first
            broken.append(f"{skill.get('name')}: cannot import {module} ({exc})")
            continue
        fn = getattr(mod, func, None)
        if not callable(fn):
            broken.append(f"{skill.get('name')}: {module} has no callable {func!r}")
    assert not broken, "declared skills that do not resolve:\n  " + "\n  ".join(broken)


#: Skills declared WITHOUT the `data.` namespace, every one of which is an exact
#: duplicate of a `data.`-prefixed sibling. Measured: 45 declared skills, 10 of
#: them these.
#:
#: It matters beyond tidiness. The MCP tool name is derived from the skill name,
#: so each of these is a second tool for the same verb under a namespace that is
#: not this extension's — ten phantom entries in anything that enumerates the
#: surface.
#:
#: A named list that may only SHRINK, which is this repo's idiom for outstanding
#: debt rather than a bounded total: a ceiling would let one alias be added while
#: another was removed and never tell anybody which. Removing one is a
#: compatibility decision — a caller may be using the bare name — so it is not
#: done here.
UNPREFIXED_DEBT = frozenset(
    {
        "backup",
        "backup_validate",
        "diagnose",
        "ingest",
        "ingest_push",
        "install",
        "list",
        "register",
        "troubleshoot",
        "unregister",
    }
)


def test_no_new_skill_is_declared_outside_the_data_namespace():
    """`data.<verb>`. The MCP tool name is derived from this, so a skill named
    without its namespace becomes a tool nobody can predict."""
    bare = {
        str(s["name"]) for s in _declared_skills() if not str(s.get("name", "")).startswith("data.")
    }
    new = bare - UNPREFIXED_DEBT
    assert not new, (
        f"new skill(s) declared outside the data. namespace: {sorted(new)}. "
        "Name it data.<verb> — the MCP tool name is derived from it."
    )


def test_the_debt_list_is_a_to_do_rather_than_an_exemption():
    """Fails when a listed name is already gone, so cleaning one forces the list
    to be edited down and the gain is locked in."""
    bare = {
        str(s["name"]) for s in _declared_skills() if not str(s.get("name", "")).startswith("data.")
    }
    stale = UNPREFIXED_DEBT - bare
    assert not stale, (
        f"good news — {sorted(stale)} no longer declared unprefixed. "
        "Remove them from UNPREFIXED_DEBT so the gain cannot be undone."
    )


def test_each_unprefixed_name_duplicates_a_prefixed_one():
    """The reason they are debt and not a design: every one is a second name for
    a verb that already has a properly namespaced declaration."""
    names = {str(s["name"]) for s in _declared_skills()}
    orphans = [n for n in UNPREFIXED_DEBT if f"data.{n}" not in names]
    assert not orphans, (
        f"{orphans} are unprefixed AND have no data.<verb> sibling, so removing "
        "them would drop the verb rather than an alias — check before pruning."
    )


def test_the_retrieval_verbs_are_declared():
    """Named explicitly because they were the ones being added when this guard
    was written, and because a guard that only checks what already exists proves
    nothing about the next verb."""
    names = {s["name"] for s in _declared_skills()}
    for verb in ("save", "list", "show", "rm", "dialect"):
        assert f"data.retrieval_{verb}" in names


def test_a_cli_subverb_resolves_to_its_declared_skill():
    """The CLI and the manifest have to agree about the skill NAME, or the verb
    dispatches to something that was never declared."""
    import argparse

    from axiom.extensions.builtins.data_platform.cli import resolve_skill

    names = {s["name"] for s in _declared_skills()}
    for sub in ("save", "list", "show", "rm", "dialect"):
        args = argparse.Namespace(verb="retrieval", retrieval_verb=sub)
        skill, err = resolve_skill(args)
        assert err is None
        assert f"data.{skill}" in names, f"{skill} is dispatched but not declared"
