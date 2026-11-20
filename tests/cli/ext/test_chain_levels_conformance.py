# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chain-readiness levels, as ``axi ext lint`` enforces them (AEOS 0.2 section 4.11).

The fixture set is a conforming extension at each level and one non-conforming
variant per rule, each made by breaking exactly one thing in a conforming base.
Each rule therefore has a case in which it must fire and a base in which it must
stay quiet, so a rule that is deleted, weakened or made unconditional fails a
test. The extensions are generated into a temp directory and linted through the
public entry point, ``lint_extension``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.cli.ext.commands.lint import chain_assessment, lint_extension
from axiom.infra import chain_levels as cl

# ---------------------------------------------------------------- the conforming bases

HEAD = """\
# mcp: not-applicable -- sample extension
[extension]
name = "ws"
version = "0.1.0"
description = "A workspace sample."
license = "Apache-2.0"
owner = "b-tree-labs"
aeos_version = "@AEOS@"
builtin = true
@CLAIM@
"""

LOOK = """
[[extension.provides]]
kind = "skill"
name = "look"
path = "skills/look"
description = "Look at the workspace."
risk = "read"
side_effects = false
"""

MAKE = """
[[extension.provides]]
kind = "skill"
name = "make"
path = "skills/make"
description = "Make a thing."
risk = "reversible-write"
side_effects = true
requires = [{ id = "named", description = "a name is given", fix = "pass name=<text>" }]
produces = [{ kind = "thing_id", how_to_use = "pass as thing to ws.drop" }]
next_steps = [{ target = "ws.drop", when = "produced:thing_id", why = "remove it again", feeds = { thing = "thing_id" } }]
@MR@
"""

DROP = """
[[extension.provides]]
kind = "skill"
name = "drop"
path = "skills/drop"
description = "Remove a thing."
risk = "destructive"
side_effects = true
requires = []
@DR@
"""

RECIPE_BLOCK = """
[[extension.provides]]
kind = "recipe"
name = "tidy"
path = "@PATH@"
description = "Make a thing and remove it."
"""

RECIPE_FILE = """\
id = "tidy"
intent = "Make a thing and remove it."
rollback = "Nothing persists after the drop."

[params]
name = "what to call it"

[[step]]
id = "make"
skill = "ws.make"
purpose = "make it"
[step.inputs]
name = "$name"

[[step]]
id = "drop"
skill = "ws.drop"
purpose = "remove it"
approval = true
[step.inputs]
thing = "$make.thing_id"
"""


def base(level: str) -> dict:
    """A conforming extension at ``level``: parts that variants edit by string replacement."""
    c = level == "C"
    return {
        "head": HEAD.replace("@AEOS@", "0.2.0").replace(
            "@CLAIM@", "" if level == "A" else f'chain_level = "{level}"'
        ),
        "skills": LOOK
        + MAKE.replace("@MR@", 'recipes = ["tidy"]' if c else "")
        + DROP.replace("@DR@", 'recipes = ["tidy"]' if c else ""),
        "recipe_block": RECIPE_BLOCK.replace("@PATH@", "recipes/tidy.toml") if c else "",
        "recipe_file": RECIPE_FILE if c else None,
    }


def write(tmp_path: Path, parts: dict, *, name: str = "ws") -> Path:
    root = tmp_path / name
    (root / "skills").mkdir(parents=True)
    (root / "tests" / "unit_tests").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "tests" / "unit_tests" / "test_standard.py").write_text("")
    (root / "axiom-extension.toml").write_text(
        parts["head"] + parts["skills"] + parts["recipe_block"]
    )
    if parts.get("recipe_file") is not None:
        (root / "recipes").mkdir()
        (root / "recipes" / "tidy.toml").write_text(parts["recipe_file"])
    return root


def codes(root: Path, level: str | None = None) -> set[str]:
    return {f.code for f in lint_extension(root, chain_level=level) if f.code.startswith("AEOS1")}


def edit(parts: dict, key: str, old: str, new: str) -> dict:
    assert old in parts[key], f"fixture drift: {old!r} not in {key}"
    out = dict(parts)
    out[key] = parts[key].replace(old, new, 1)
    return out


# ---------------------------------------------------------------- conforming


@pytest.mark.parametrize("level", ["A", "B", "C"])
def test_a_conforming_extension_has_no_chain_findings_at_its_level(tmp_path, level):
    root = write(tmp_path, base(level))
    assert codes(root) == set()
    a = chain_assessment(root)
    assert a.claimed == level and cl.LEVELS.index(a.achieved) >= cl.LEVELS.index(level)


def test_the_levels_are_ordered_and_c_implies_b(tmp_path):
    root = write(tmp_path, base("C"))
    assert codes(root, "B") == set() and codes(root, "A") == set()
    assert chain_assessment(root).achieved == "C"


def test_an_existing_extension_with_no_declarations_keeps_conforming_at_level_a(tmp_path):
    parts = base("A")
    parts["head"] = HEAD.replace("@AEOS@", "0.1.0").replace("@CLAIM@", "")
    parts["skills"] = """
[[extension.provides]]
kind = "skill"
name = "plain"
path = "skills/plain"
description = "Declares nothing."
"""
    root = write(tmp_path, parts)
    assert codes(root) == set()  # backward compatible: nothing new is required
    findings = lint_extension(root)
    assert not [f for f in findings if f.severity == "error"]
    assert chain_assessment(root).achieved == "A"


def test_trying_level_b_lists_each_skills_finding_with_its_exact_fix(tmp_path):
    parts = base("A")
    parts["skills"] = """
[[extension.provides]]
kind = "skill"
name = "plain"
path = "skills/plain"
description = "Declares nothing."
"""
    root = write(tmp_path, parts)
    found = {f.code: f for f in lint_extension(root, chain_level="B")}
    assert {"AEOS110", "AEOS111", "AEOS102"} <= set(found)
    assert "ws.plain" in found["AEOS110"].message
    assert 'risk = "reversible-write"' in found["AEOS110"].remediation
    assert "requires = []" in found["AEOS111"].remediation


# ---------------------------------------------------------------- one non-conforming variant per rule

CASES: list[tuple[str, str, str, str, str, str]] = [
    # rule, base level, part, old, new, what it breaks
    ("AEOS100", "B", "head", 'chain_level = "B"', 'chain_level = "Z"', "unknown level"),
    ("AEOS101", "B", "head", 'aeos_version = "0.2.0"', 'aeos_version = "0.1.0"', "claim needs 0.2"),
    ("AEOS110", "B", "skills", 'risk = "read"\n', "", "no risk"),
    ("AEOS111", "B", "skills", "requires = []\n", "", "silent requires on a non-read"),
    (
        "AEOS112",
        "B",
        "skills",
        "side_effects = false",
        "side_effects = true",
        "risk read, side effects true",
    ),
    (
        "AEOS112",
        "B",
        "skills",
        'risk = "reversible-write"\nside_effects = true',
        'risk = "reversible-write"\nside_effects = false',
        "write that claims no side effects",
    ),
    ("AEOS113", "B", "skills", 'fix = "pass name=<text>"', 'fix = ""', "requires without a fix"),
    (
        "AEOS114",
        "B",
        "skills",
        'how_to_use = "pass as thing to ws.drop" }]\nnext_steps = [{ target = "ws.drop", when = "produced:thing_id", '
        'why = "remove it again", feeds = { thing = "thing_id" } }]',
        'how_to_use = "an identifier" }]\nnext_steps = [{ target = "ws.drop", when = "ok", why = "remove it again" }]',
        "produced ref nothing consumes",
    ),
    (
        "AEOS115",
        "B",
        "skills",
        'target = "ws.drop"',
        'target = "ws.nothing"',
        "next target unresolved",
    ),
    (
        "AEOS116",
        "B",
        "skills",
        'feeds = { thing = "thing_id" }',
        'feeds = { thing = "other_kind" }',
        "feeds a ref not produced",
    ),
    (
        "AEOS116",
        "B",
        "skills",
        'when = "produced:thing_id"',
        'when = "produced:other_kind"',
        "waits on a ref not produced",
    ),
    (
        "AEOS118",
        "B",
        "skills",
        'description = "Look at the workspace."',
        'description = "Look at the workspace."\nrecipes = ["ghost"]',
        "recipe exists nowhere",
    ),
    (
        "AEOS120",
        "C",
        "recipe_block",
        RECIPE_BLOCK.replace("@PATH@", "recipes/tidy.toml"),
        "",
        "no recipe shipped",
    ),
    (
        "AEOS121",
        "C",
        "recipe_block",
        'path = "recipes/tidy.toml"',
        'path = "recipes/missing.toml"',
        "recipe file missing",
    ),
    ("AEOS122", "C", "recipe_file", 'id = "tidy"', 'id = "BAD ID"', "recipe malformed"),
    ("AEOS123", "C", "recipe_file", "$make.thing_id", "$make.nothing", "recipe wiring broken"),
    (
        "AEOS124",
        "C",
        "skills",
        'description = "Remove a thing."\nrisk = "destructive"\nside_effects = true\nrequires = []\nrecipes = ["tidy"]',
        'description = "Remove a thing."\nrisk = "destructive"\nside_effects = true\nrequires = []',
        "membership one way",
    ),
    (
        "AEOS127",
        "C",
        "recipe_file",
        "approval = true",
        "approval = false",
        "destructive step with no approval",
    ),
]


def _variant(tmp_path, level, part, old, new):
    parts = edit(base(level), part, old, new)
    if part == "recipe_block" and not new:
        # dropping the recipe block also drops the (now dangling) membership declarations
        parts["skills"] = parts["skills"].replace('recipes = ["tidy"]', "")
    return write(tmp_path, parts)


@pytest.mark.parametrize(
    ("rule", "level", "part", "old", "new", "why"),
    CASES,
    ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)],
)
def test_each_rule_fires_on_its_own_violation_and_the_base_stays_quiet(
    tmp_path, rule, level, part, old, new, why
):
    quiet = write(tmp_path / "ok", base(level))
    assert rule not in codes(quiet), f"{rule} fires on a conforming extension"
    bad = _variant(tmp_path / "bad", level, part, old, new)
    assert rule in codes(bad), f"{rule} did not fire on: {why}"


def test_a_failing_claim_is_reported_as_aeos102_with_the_findings_above_it(tmp_path):
    bad = _variant(tmp_path, "B", "skills", 'risk = "read"\n', "")
    found = lint_extension(bad)
    assert "AEOS110" in {f.code for f in found} and "AEOS102" in {f.code for f in found}
    assert any(
        "claims Level B and achieves Level A" in f.message for f in found if f.code == "AEOS102"
    )


def test_a_higher_level_than_claimed_is_noted_not_failed(tmp_path):
    parts = base("C")
    parts["head"] = HEAD.replace("@AEOS@", "0.2.0").replace("@CLAIM@", 'chain_level = "B"')
    root = write(tmp_path, parts)
    assert not [f for f in lint_extension(root) if f.severity == "error"]
    a = chain_assessment(root)
    assert a.achieved == "C" and "AEOS103" in {f.code for f in a.findings}


# ---------------------------------------------------------------- rules that need a different harness


def test_a_foreign_reference_is_a_warning_when_nothing_can_verify_it_and_an_error_when_the_host_can():
    d = cl.manifest_descriptors(
        {
            "extension": {
                "name": "ws",
                "provides": [
                    {
                        "kind": "skill",
                        "name": "make",
                        "risk": "read",
                        "next_steps": [{"target": "other.verb", "when": "ok", "why": "carry on"}],
                    }
                ],
            }
        }
    )
    quiet = cl.assess("ws", d, [], claimed="B", aeos_version="0.2.0", host=None)
    assert "AEOS125" in {f.code for f in quiet.findings} and quiet.achieved == "B"
    from axiom.infra.skills import SkillRegistry

    sure = cl.assess("ws", d, [], claimed="B", aeos_version="0.2.0", host=cl.Host(SkillRegistry()))
    assert "AEOS115" in {f.code for f in sure.findings} and sure.achieved == "A"


def test_the_manifest_copy_must_match_the_code_aeos130():
    from axiom.infra.skill_declarations import Precondition
    from axiom.infra.skills import SkillResult, SkillSpec

    spec = SkillSpec(
        name="ws.make",
        fn=lambda p, c: SkillResult(),
        risk="reversible-write",
        requires=(Precondition("named", "a name is given", fix="pass name=<text>"),),
    )
    code = cl.descriptor_from_spec(spec)
    same = cl.descriptor_from_block(
        {
            "name": "ws.make",
            "risk": "reversible-write",
            "requires": [
                {"id": "named", "description": "a name is given", "fix": "pass name=<text>"}
            ],
        },
        "ws",
    )
    drifted = cl.descriptor_from_block(
        {"name": "ws.make", "risk": "destructive", "requires": []}, "ws"
    )
    ok = cl.assess("ws", [same], [], claimed="B", aeos_version="0.2.0", parity={"ws.make": code})
    bad = cl.assess(
        "ws", [drifted], [], claimed="B", aeos_version="0.2.0", parity={"ws.make": code}
    )
    assert "AEOS130" not in {f.code for f in ok.findings}
    assert "AEOS130" in {f.code for f in bad.findings}


def test_a_declaration_the_schema_rejects_is_a_schema_violation(tmp_path):
    from axiom_tests import validate_manifest

    good = tmp_path / "g"
    root = write(good, base("C"))
    from axiom.cli.ext.commands.lint import _load_manifest

    manifest = _load_manifest(root / "axiom-extension.toml")
    assert validate_manifest(manifest) == []
    manifest["extension"]["provides"][1]["requires"][0].pop("fix")
    assert validate_manifest(manifest), (
        "a requires entry with no fix must be rejected by the schema"
    )
    manifest["extension"]["provides"][1]["requires"][0]["fix"] = "pass name=<text>"
    assert validate_manifest(manifest) == []
    manifest["extension"]["provides"][1]["risk"] = "scary"
    assert validate_manifest(manifest), "an unknown risk must be rejected by the schema"


# ---------------------------------------------------------------- the catalogue


def test_every_rule_has_a_fixture_or_a_named_runtime_test():
    covered = {c[0] for c in CASES} | {
        "AEOS102",
        "AEOS103",
        "AEOS104",
        "AEOS119",
        "AEOS125",
        "AEOS130",
    }
    runtime = {r.id for r in cl.RULES if r.level == "runtime"}
    assert set(cl.RULE_IDS) - covered - runtime == set(), "a rule has no case in which it must fire"
