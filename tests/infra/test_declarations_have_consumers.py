# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every declared field must change something, or say why it does not.

TDD does not catch "declared but not wired", and it is worth being precise
about why. The failure is relational: one module declares a field, another is
meant to act on it, and both modules' tests pass because each does its own job
correctly. Nobody tests the join, and you cannot write a failing test for code
you forgot to write — you do not know it is missing.

This repository already contains the technique that does catch it.
``tests/infra/test_skill_dispatch.py`` scans for direct ``registry.invoke``
calls and requires every one to appear in ``DIRECT_INVOKE_EXEMPT`` with a
reason. It caught a real defect in ``ActionRunner`` the week this was written.
The shape that works is: **a registry of known, justified absences, and a test
that fails on any new one** — because it forces a human decision at the moment
of introduction rather than hoping somebody notices later.

This generalises that to declaration surfaces.

The distinction that matters
----------------------------

A field having a *reader* is too weak a property. ``allowed_tools`` had one for
months: the SKILL.md generator rendered it into YAML. It was still dead, because
nothing ever checked that a skill only called the tools it declared. Rendering a
value is not acting on it.

So each field is classified as one of three things, and a field in none of them
fails this test:

``ENFORCED`` — something branches on the value and behaves differently.
``RENDERING_ONLY`` — the value exists to be displayed, and that is honest.
``DECLARED_NOT_ENFORCED`` — a known gap, recorded with what would close it.

The third category is the point. A gap that is written down is a decision; a gap
that is invisible is the bug this file exists to prevent.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "src" / "axiom" / "infra" / "skills.py"

#: field -> the module that BRANCHES on it. Not a module that prints it.
ENFORCED: dict[str, str] = {
    "name": "src/axiom/infra/skills.py",
    "fn": "src/axiom/infra/skills.py",
    "inputs": "src/axiom/extensions/builtins/mcp/skill_tools.py",
    "side_effects": "src/axiom/infra/capability_projection.py",
    "idempotent": "src/axiom/infra/orchestrator/runner.py",
    "surfaces": "src/axiom/extensions/builtins/mcp/skill_tools.py",
    # ADR-114 §1: projected onto the surface by skill_tools, then branched on at
    # dispatch — a caller that does not match is refused before the handler runs.
    "allowed_principals": "src/axiom/extensions/builtins/mcp/server.py",
}

#: field -> why being displayed is the whole of its job.
RENDERING_ONLY: dict[str, str] = {
    "description": "the one-liner a person and a model read before choosing a verb",
    "long_description": "prose for the SKILL.md body; there is nothing to enforce",
}

#: field -> what would make it load-bearing. These are gaps, on purpose visible.
DECLARED_NOT_ENFORCED: dict[str, str] = {
    "allowed_tools": (
        "rendered into every generated SKILL.md and enforced nowhere. A skill "
        "may call any capability regardless of what it declared. Closing it "
        "means checking the declaration at invoke time — the natural place is "
        "skill_dispatch.invoke_capability, which already sees both the caller "
        "and the callee."
    ),
}


def _fields() -> list[str]:
    tree = ast.parse(SKILLS.read_text())
    node = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "SkillSpec"
    )
    return [t.target.id for t in node.body if isinstance(t, ast.AnnAssign)]


def _reads(path: str, field: str) -> bool:
    target = REPO / path
    if not target.is_file():
        return False
    return subprocess.run(
        ["grep", "-q", rf"\.{field}\b", str(target)]
    ).returncode == 0


class TestEveryFieldIsAccountedFor:
    def test_no_field_is_unclassified(self):
        """A new field with no entry fails here, at the moment it is added.

        That is the whole mechanism: the author must say what consumes it, or
        record that nothing does and why. Both are fine; silence is not.
        """
        classified = set(ENFORCED) | set(RENDERING_ONLY) | set(DECLARED_NOT_ENFORCED)
        unclassified = [f for f in _fields() if f not in classified]

        assert not unclassified, (
            f"SkillSpec fields with no recorded consumer: {unclassified}. "
            "Add each to ENFORCED (naming the module that branches on it), to "
            "RENDERING_ONLY, or to DECLARED_NOT_ENFORCED with what would close "
            "the gap."
        )

    def test_a_field_is_in_exactly_one_category(self):
        seen = list(ENFORCED) + list(RENDERING_ONLY) + list(DECLARED_NOT_ENFORCED)

        assert len(seen) == len(set(seen)), "a field is classified twice"

    def test_no_entry_names_a_field_that_no_longer_exists(self):
        """A stale entry is a claim gone false, exactly as in DIRECT_INVOKE_EXEMPT."""
        fields = set(_fields())
        stale = sorted(
            f
            for f in (*ENFORCED, *RENDERING_ONLY, *DECLARED_NOT_ENFORCED)
            if f not in fields
        )

        assert not stale, f"entries for fields that are gone: {stale}"


class TestTheNamedConsumerActuallyReadsIt:
    """Otherwise the registry drifts into a list of good intentions."""

    @pytest.mark.parametrize(("field", "module"), sorted(ENFORCED.items()))
    def test_the_enforcing_module_references_the_field(self, field, module):
        assert _reads(module, field), (
            f"{module} is recorded as enforcing {field!r} and does not mention it. "
            "Either the consumer moved, or it was removed and the field is now a "
            "declaration nothing acts on."
        )


class TestGapsAreDescribedWellEnoughToClose:
    @pytest.mark.parametrize("field", sorted(DECLARED_NOT_ENFORCED))
    def test_each_gap_says_what_would_close_it(self, field):
        """A gap recorded as "TODO" is not recorded. The note has to name the
        place the check would go, or nobody can pick it up."""
        note = DECLARED_NOT_ENFORCED[field]

        assert len(note) > 80, f"{field}: the note is too short to act on"
        assert any(
            hint in note for hint in ("invoke", "dispatch", "means", "Closing")
        ), f"{field}: the note does not say what would close it"

    def test_the_list_is_short_enough_to_be_embarrassing(self):
        """Not a lint rule — a pressure valve.

        A registry of gaps only works while the list is small enough that
        somebody minds it growing. If this assertion starts failing, the
        response is to close gaps, not to raise the number.
        """
        assert len(DECLARED_NOT_ENFORCED) <= 3, (
            f"{len(DECLARED_NOT_ENFORCED)} declared-but-unenforced fields. "
            "Close one before adding another."
        )
