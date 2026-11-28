# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""AEOS 1.0 keeps an honest section status table and stays publishable.

A section is ``proven`` only when a named check passes against it, and that
check must exist in this repository. Every numbered section appears in the
table exactly once and is numbered exactly once. The standard names no private
product documents and no outside project, and the federation documents use no
em-dashes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AEOS = ROOT / "docs" / "specs" / "spec-aeos-1.0.md"
FEDERATION_DOCS = [
    ROOT / "docs" / "specs" / "spec-agent-accord-protocol.md",
    ROOT / "docs" / "specs" / "spec-aeos-federation-conformance-kit.md",
    *sorted((ROOT / "docs" / "adrs").glob("adr-276-*.md")),
]
STATUSES = {"draft", "proven", "informative"}
_HEADING = re.compile(r"^## (\d+)\. (.+)$", re.M)
_ROW = re.compile(r"^\|\s*(\d+)\. ([^|]+?)\s*\|\s*(\w+)\s*\|([^|]*)\|([^|]*)\|$", re.M)


def _text() -> str:
    return AEOS.read_text(encoding="utf-8")


def _status_block(text: str) -> str:
    start = text.index("## Section status")
    return text[start : text.index("\n---\n", start)]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^#{2,3} ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + (nxt.start() if nxt else len(text))]


def test_numbered_sections_are_unique_and_consecutive():
    numbers = [int(n) for n, _ in _HEADING.findall(_text())]
    assert numbers == list(range(1, len(numbers) + 1)), numbers


def test_status_table_lists_every_numbered_section_once():
    text = _text()
    headings = {int(n): title for n, title in _HEADING.findall(text)}
    rows = _ROW.findall(_status_block(text))
    listed = [int(r[0]) for r in rows]
    assert sorted(listed) == sorted(headings), (listed, sorted(headings))
    for number, title, status, _proof, _chunk in rows:
        assert headings[int(number)].startswith(title.strip()), (number, title)
        assert status in STATUSES, (number, status)


def test_proven_sections_name_a_check_that_exists():
    for number, _title, status, proof, _chunk in _ROW.findall(_status_block(_text())):
        paths = re.findall(r"`([^`]+\.py)`", proof)
        if status == "proven":
            assert paths, f"§{number} is proven but names no check"
            for p in paths:
                assert (ROOT / p).is_file(), f"§{number}: {p} does not exist"
        else:
            assert not proof.strip(), f"§{number} names a proof but is {status}"


def test_draft_subsections_say_so():
    text = _text()
    for heading in (
        "### 15.7 Agent type inheritance and lineage (draft)",
        "### 15.8 Agent instance state and aging (draft)",
    ):
        assert "*Status: draft." in _section(text, heading), heading


def test_aeos_names_no_private_product_document():
    assert not re.findall(r"\bprd-[\w-]+\.md", _text())


@pytest.mark.parametrize("path", [AEOS, *FEDERATION_DOCS], ids=lambda p: p.name[:40])
def test_no_outside_project_is_named(path):
    assert "agentlink" not in path.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("path", FEDERATION_DOCS, ids=lambda p: p.name[:40])
def test_federation_documents_use_no_em_dash(path):
    assert "\u2014" not in path.read_text(encoding="utf-8")


def test_new_aeos_sections_use_no_em_dash():
    text = _text()
    for part in (
        _status_block(text),
        _section(text, "### 15.7 Agent type inheritance and lineage (draft)"),
        _section(text, "### 15.8 Agent instance state and aging (draft)"),
        _section(text, "## 19. Federation-Native Where Applicable"),
    ):
        assert "\u2014" not in part, part[:60]
