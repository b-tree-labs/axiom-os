# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A SKILL.md is UNTRUSTED INPUT, so it gets hostile-input tests.

The reader exists to ingest documents written by other harnesses and by people.
That makes it an attack surface rather than a convenience: the name it parses
becomes a registry key AND a filesystem path component (``skills_emit`` writes
to ``<ext>/skills/<leaf>/SKILL.md``), and the tools it parses become a
bounded-exposure set.

Chaos testing found a name containing ``..`` and separators being accepted and
then used as a path, so the emitted file landed outside the skills directory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from axiom.infra.skill_md import SkillDocumentError, read_skill_md


def _doc(front: str, body: str = "Body.") -> str:
    return "---\n" + front + "\n---\n\n" + body + "\n"


# --- names are identifiers, not paths ---------------------------------------


TRAVERSAL_NAMES = [
    pytest.param("../../etc/passwd", id="relative-traversal"),
    pytest.param("../../../tmp/probe", id="deeper-traversal"),
    pytest.param("/etc/passwd", id="absolute"),
    pytest.param("a/b", id="separator"),
    pytest.param("a\\b", id="windows-separator"),
    pytest.param("..", id="bare-dotdot"),
    pytest.param("a/../b", id="embedded-dotdot"),
]


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_a_name_that_is_a_path_is_refused(name):
    """The name becomes a directory component in `skills_emit`. A name that can
    traverse is a file-write primitive, so it must not parse at all."""
    with pytest.raises(SkillDocumentError):
        read_skill_md(_doc(f"name: {name!r}"))


def test_negative_control_ordinary_names_still_parse():
    # Proves the guard above is not simply rejecting everything.
    for ok in ("press.draft", "data.gold_aggregate", "analytics.compute", "plain"):
        assert read_skill_md(_doc(f"name: {ok}")).name == ok


def test_an_absurdly_long_name_is_refused():
    with pytest.raises(SkillDocumentError):
        read_skill_md(_doc("name: " + "a" * 10_000))


def test_a_control_character_in_a_name_is_refused():
    with pytest.raises(SkillDocumentError):
        read_skill_md(_doc("name: \"a\\u0007b\""))


# --- defense in depth: emission refuses to leave its directory --------------


def test_emission_refuses_to_write_outside_the_target_directory():
    """Even if a bad name reached a SkillSpec by another route, writing must
    stay inside the directory it was handed. One validation point is the kind of
    brittleness this whole change is removing."""
    from axiom.infra.skills import SkillSpec
    from axiom.infra.skills_emit import emit_md_for_spec

    def fn(params, ctx=None):  # pragma: no cover
        ...

    spec = SkillSpec(
        name="../../escape", description="d", fn=fn,
        inputs={}, allowed_tools=(), long_description="b",
    )
    with tempfile.TemporaryDirectory() as td:
        intended = Path(td) / "ext" / "skills"
        intended.mkdir(parents=True)
        with pytest.raises(ValueError):
            emit_md_for_spec(spec, intended / ".." / ".." / "escaped", "1.0.0")


# --- interoperability: documents other harnesses actually write -------------


def test_a_crlf_document_is_readable():
    """A SKILL.md authored on Windows, or checked out with CRLF, must import.
    Rejecting it outright defeats the point of a cross-harness format."""
    doc = "---\r\nname: press.draft\r\ndescription: d\r\n---\r\n\r\nBody.\r\n"
    assert read_skill_md(doc).name == "press.draft"


def test_a_utf8_bom_does_not_hide_the_frontmatter():
    """Many Windows editors prepend a BOM; it must not make the document
    unreadable."""
    doc = "﻿---\nname: press.draft\ndescription: d\n---\n\nBody.\n"
    assert read_skill_md(doc).name == "press.draft"


# --- allowed_tools is a security set, so junk must not enter it -------------


def test_non_scalar_tool_entries_are_dropped_not_stringified():
    """``allowed-tools: [[a],[b]]`` used to yield the tool names "['a']" and
    "['b']" — junk in a bounded-exposure set. Dropping is right; inventing a
    tool name from a nested structure is not."""
    doc = read_skill_md(_doc("name: a.b\nallowed-tools: [[a],[b]]"))
    assert doc.allowed_tools == ()


def test_a_mapping_of_tools_yields_no_tools():
    doc = read_skill_md(_doc("name: a.b\nallowed-tools:\n  x: y"))
    assert doc.allowed_tools == ()


def test_negative_control_ordinary_tool_lists_survive():
    doc = read_skill_md(_doc("name: a.b\nallowed-tools: [press.publish, repo.read]"))
    assert doc.allowed_tools == ("press.publish", "repo.read")
