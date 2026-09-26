# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What we emit, we must be able to read back (ADR-063 + the SKILL.md reader).

SKILL.md is the interchange format: it is how another harness discovers an Axiom
skill, and how a hand-written procedure becomes a capability here. Those two
directions were implemented in different modules with no shared contract — the
writer built YAML with f-strings while the reader used ``yaml.safe_load`` — so
they disagreed about escaping. Ordinary punctuation in a description was enough
to emit a file nothing could parse.

These are round-trip properties rather than golden strings: the guarantee that
matters is ``read(render(spec)) == spec``, not any particular byte layout.
"""

from __future__ import annotations

import pytest

from axiom.infra.skill_md import read_skill_md
from axiom.infra.skills import SkillSpec
from axiom.infra.skills_emit import _render_skill_md


def _fn(params, ctx=None):  # pragma: no cover - never called
    ...


def _spec(**over) -> SkillSpec:
    kw = dict(
        name="press.draft",
        description="Draft a release note",
        fn=_fn,
        inputs={},
        allowed_tools=(),
        long_description="Body prose.",
    )
    kw.update(over)
    return SkillSpec(**kw)


HOSTILE_DESCRIPTIONS = [
    pytest.param("Draft: a release note", id="colon"),
    pytest.param("Draft #1 note", id="hash-reads-as-yaml-comment"),
    pytest.param("[draft] a note", id="leading-bracket"),
    pytest.param("Line one\nLine two", id="newline"),
    pytest.param('He said "go"', id="double-quote"),
    pytest.param("It's fine", id="apostrophe"),
    pytest.param("- dash leading", id="leading-dash"),
    pytest.param("100% done", id="percent"),
    pytest.param("a: b: c", id="multiple-colons"),
    pytest.param("trailing space ", id="trailing-space"),
]


@pytest.mark.parametrize("description", HOSTILE_DESCRIPTIONS)
def test_description_survives_the_round_trip(description):
    doc = read_skill_md(_render_skill_md(_spec(description=description), "1.0.0"))
    assert doc.description == description.strip()


HOSTILE_TOOLS = [
    pytest.param(("a,b", "c"), id="comma-inside-a-tool-name"),
    pytest.param(("a]b",), id="bracket-inside-a-tool-name"),
    pytest.param(("ns.tool", "other.tool"), id="ordinary-dotted"),
    pytest.param(("with space",), id="space"),
    pytest.param(("#hash",), id="hash"),
]


@pytest.mark.parametrize("tools", HOSTILE_TOOLS)
def test_allowed_tools_survive_the_round_trip(tools):
    """allowed_tools is the bounded-exposure declaration, so a silent change to
    the SET is a security-relevant defect, not a formatting nit."""
    doc = read_skill_md(_render_skill_md(_spec(allowed_tools=tools), "1.0.0"))
    assert doc.allowed_tools == tools


def test_inputs_survive_the_round_trip():
    inputs = {"topic": "str", "since": "str", "odd:key": "int"}
    doc = read_skill_md(_render_skill_md(_spec(inputs=inputs), "1.0.0"))
    assert doc.inputs == inputs


def test_name_and_version_survive_the_round_trip():
    doc = read_skill_md(_render_skill_md(_spec(name="press.draft"), "1.2.3"))
    assert doc.name == "press.draft"
    assert doc.version == "1.2.3"


def test_body_survives_a_frontmatter_delimiter_in_the_prose():
    """A '---' inside the body must not be mistaken for the end of frontmatter."""
    spec = _spec(long_description="Intro.\n\n---\n\nAfter a rule.")
    doc = read_skill_md(_render_skill_md(spec, "1.0.0"))
    assert "After a rule." in doc.body


def test_negative_control_an_unrelated_value_does_not_match():
    # Proves the assertions above are not vacuously true.
    doc = read_skill_md(_render_skill_md(_spec(description="real"), "1.0.0"))
    assert doc.description != "something else"


# --- the security-relevant case, named so the reason survives ---------------


def test_a_comma_in_a_tool_name_does_not_widen_allowed_tools():
    """REGRESSION. ``allowed-tools`` was emitted as a bare ``[a, b]`` list, so a
    tool whose name contained a comma came back as TWO tools.

    This is not a formatting nit. ``allowed_tools`` is the bounded-exposure
    declaration — the set a caller may use while following the procedure. A
    parse that silently *enlarges* that set is worse than one that fails, because
    nothing anywhere reports it. The contract must round-trip the set exactly.
    """
    spec = _spec(allowed_tools=("billing,refunds", "repo.read"))
    doc = read_skill_md(_render_skill_md(spec, "1.0.0"))
    assert doc.allowed_tools == ("billing,refunds", "repo.read")
    assert len(doc.allowed_tools) == 2, f"tool set was widened: {doc.allowed_tools}"


def test_a_colon_in_a_description_still_yields_a_readable_document():
    """REGRESSION. ``description: Draft: a note`` is invalid YAML, so the whole
    frontmatter failed to parse and the skill became invisible — a skill that
    merely used a colon silently vanished from discovery."""
    doc = read_skill_md(_render_skill_md(_spec(description="Draft: a release note"), "1.0.0"))
    assert doc.name == "press.draft"
    assert doc.description == "Draft: a release note"


# --- the real corpus: every description the fleet actually ships -------------


def _fleet_descriptions() -> list[tuple[str, str]]:
    """Descriptions from every shipped manifest — where real punctuation lives."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "axiom" / "extensions" / "builtins"
    found: list[tuple[str, str]] = []
    for manifest in sorted(root.glob("*/axiom-extension.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for provided in data.get("extension", {}).get("provides", []) or []:
            desc = provided.get("description")
            if isinstance(desc, str) and desc.strip():
                found.append((f"{manifest.parent.name}:{provided.get('name', '?')}", desc))
    return found


def test_every_shipped_description_survives_the_round_trip():
    """Fleet-wide net: a description that cannot round-trip is a skill that
    would emit an unreadable SKILL.md. Cheap to run, and real descriptions are
    where the colons, hashes and quotes actually are."""
    corpus = _fleet_descriptions()
    assert corpus, "found no manifest descriptions — the corpus walk is broken"

    broken = []
    for label, desc in corpus:
        try:
            doc = read_skill_md(_render_skill_md(_spec(description=desc), "1.0.0"))
            if doc.description != desc.strip():
                broken.append((label, desc, doc.description))
        except Exception as exc:  # noqa: BLE001 - report every failure, not the first
            broken.append((label, desc, f"{type(exc).__name__}: {exc}"))

    assert not broken, "descriptions that do not round-trip:\n" + "\n".join(
        f"  {label}: {desc!r} -> {got!r}" for label, desc, got in broken
    )


# --- idempotence: the property a repair loop depends on (ADR-117 §1) --------


def test_rendering_is_idempotent():
    """ADR-117 termination argument #1.

    A steward that regenerates a drifted document must converge. If rendering
    carried anything volatile — a timestamp, a hash, a set ordering — every pass
    would emit a fresh diff and the propose/approve loop would never settle.
    So rendering must be a pure function of (SkillSpec, version).
    """
    spec = _spec(
        description="Draft: a note",
        inputs={"b": "str", "a": "int"},
        allowed_tools=("z.tool", "a.tool"),
    )
    first = _render_skill_md(spec, "1.0.0")
    for _ in range(5):
        assert _render_skill_md(spec, "1.0.0") == first


def test_render_read_render_reaches_a_fixed_point():
    """detect -> repair -> detect must come back empty (ADR-117 fixed-point test).

    Rendering what we parsed from our own output must reproduce that output
    exactly; otherwise a repair would itself register as new drift.
    """
    from axiom.infra.skill_md import render_skill_md

    spec = _spec(
        description="Draft: a #note with punctuation",
        inputs={"topic": "str"},
        allowed_tools=("a,b", "c.d"),
    )
    once = _render_skill_md(spec, "2.0.0")
    doc = read_skill_md(once)
    twice = render_skill_md(
        name=doc.name,
        description=doc.description,
        version=doc.version,
        inputs=doc.inputs,
        allowed_tools=doc.allowed_tools,
        body=doc.body,
    )
    assert twice == once, "a repair would re-register as drift"


# --- provenance marker: which direction owns this file (ADR-117 §2) ---------


def test_a_generated_document_declares_that_it_is_generated():
    """ADR-117 termination argument #2.

    A steward must be able to tell a generated projection from an authored
    source. Without that, regenerating an authored document is indistinguishable
    from repairing a drifted one, and the import/export ping-pong has an edge to
    traverse. The marker is what removes it.
    """
    from axiom.infra.skill_md import GENERATOR_MARKER, read_skill_md

    doc = read_skill_md(_render_skill_md(_spec(), "1.0.0"))
    assert doc.generator == GENERATOR_MARKER
    assert doc.is_generated is True


def test_an_authored_document_claims_no_generator():
    """A hand-written or imported SKILL.md carries no marker and must never be
    regenerated — TIDY validates it instead."""
    from axiom.infra.skill_md import read_skill_md

    authored = "---\nname: hand.written\ndescription: by a person\n---\n\nSteps.\n"
    doc = read_skill_md(authored)
    assert doc.generator == ""
    assert doc.is_generated is False


def test_the_marker_survives_the_round_trip_with_everything_else():
    from axiom.infra.skill_md import read_skill_md

    doc = read_skill_md(_render_skill_md(_spec(description="Draft: a #note"), "3.1.0"))
    assert doc.is_generated and doc.description == "Draft: a #note" and doc.version == "3.1.0"
