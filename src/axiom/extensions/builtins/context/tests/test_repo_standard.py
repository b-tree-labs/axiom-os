# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The repo standard's managed block, "Working here with an AI" (ADR-221).

Axiom owns one marked region of an adopting repo's AGENTS.md. These tests pin
the promises the ADR makes about it: init produces the standard, sync keeps
the block current without touching a byte of authored text, check fails when
the block is stale, and every generated tool file carries it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from axiom.extensions.builtins.context import core, generators, standard

AUTHORED_HEAD = "# Twin code\n\nIntro paragraph, written by hand.\n\n"
AUTHORED_TAIL = "## What this repo is\n\nAuthored.  Odd   spacing kept.\n\n## Rules\n\n- One.\n"


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _adopted(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text(AUTHORED_HEAD + AUTHORED_TAIL, encoding="utf-8")
    core.init(tmp_path)
    return tmp_path


def _outside_block(text: str) -> tuple[str, str]:
    head, _, rest = text.partition(standard.BLOCK_BEGIN)
    _, _, tail = rest.partition(standard.BLOCK_END)
    return head, tail


def test_init_on_an_empty_repo_produces_the_standard(tmp_path):
    root = _git_repo(tmp_path)
    res = core.init(root)

    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    for section in (
        "## What this repo is",
        "## What this repo is not",
        "## Where code goes",
        "## Safety rules",
        "## How to run and test",
    ):
        assert section in agents, f"starter is missing {section!r}"
    assert standard.block_state(agents) == "current"
    assert res.standard == "inserted"
    assert (root / "CLAUDE.md").is_symlink()
    for t in generators.TARGETS:
        assert (root / t.relpath).exists(), f"{t.name} not generated"
    assert "axi context check" in (root / ".git/hooks/pre-commit").read_text(encoding="utf-8")
    assert not core.has_drift(core.check(root))


def test_adopting_an_existing_repo_keeps_every_authored_byte(tmp_path):
    original = AUTHORED_HEAD + AUTHORED_TAIL
    (tmp_path / "AGENTS.md").write_text(original, encoding="utf-8")
    core.init(tmp_path)
    after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    head, tail = _outside_block(after)
    assert head == AUTHORED_HEAD
    assert tail == "\n\n" + AUTHORED_TAIL
    # Removing exactly what init added gives back the original file.
    assert after.replace(standard.render_block() + "\n\n", "") == original


def test_the_block_goes_before_the_first_authored_section_not_inside_another_region(tmp_path):
    other = (
        "<!-- axiom:program-priorities:begin -->\n## Current priorities\n\nText.\n"
        "<!-- axiom:program-priorities:end -->\n\n"
    )
    text = AUTHORED_HEAD + other + AUTHORED_TAIL
    out = standard.ensure_block(text)
    assert out.index("<!-- axiom:program-priorities:end -->") < out.index(standard.BLOCK_BEGIN)
    assert out.index(standard.BLOCK_END) < out.index("## What this repo is")


def test_sync_refreshes_the_managed_block_and_leaves_authored_text_alone(tmp_path):
    stale_block = "\n".join(
        [standard.BLOCK_BEGIN, "<!-- axiom repo standard v0. -->", "Old advice.", standard.BLOCK_END]
    )
    head = AUTHORED_HEAD
    tail = "\n\n" + AUTHORED_TAIL + "\nTrailing authored line with no newline"
    (tmp_path / "AGENTS.md").write_text(head + stale_block + tail, encoding="utf-8")

    by = {r.target: r.status for r in core.sync(tmp_path, write=True)}
    assert by[core.STANDARD_TARGET] == "written"

    after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert standard.block_state(after) == "current"
    assert _outside_block(after) == (head, tail)
    # A second sync is a no-op.
    by = {r.target: r.status for r in core.sync(tmp_path, write=True)}
    assert by[core.STANDARD_TARGET] == "unchanged"


def test_sync_does_not_add_the_block_to_a_repo_that_has_not_adopted_it(tmp_path):
    (tmp_path / "AGENTS.md").write_text(AUTHORED_HEAD + AUTHORED_TAIL, encoding="utf-8")
    by = {r.target: r.status for r in core.sync(tmp_path, write=True)}
    assert by[core.STANDARD_TARGET] == "absent"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == AUTHORED_HEAD + AUTHORED_TAIL
    # Not adopted is reported, never failed: an Axiom upgrade breaks no one.
    assert not core.has_drift(core.check(tmp_path))
    assert "context.standard-missing" in {f.code for f in core.findings(tmp_path)}


def test_check_fails_when_the_block_is_stale(tmp_path):
    root = _adopted(tmp_path)
    assert not core.has_drift(core.check(root))

    agents = root / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("one small step", "one giant leap"),
        encoding="utf-8",
    )
    results = core.check(root)
    assert {r.target: r.status for r in results}[core.STANDARD_TARGET] == "drift"
    assert core.has_drift(results)

    run = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.context.cli", "check", "--root", str(root)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1


def test_status_says_the_block_is_outdated_and_how_to_fix_it(tmp_path):
    root = _adopted(tmp_path)
    agents = root / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            f"axiom repo standard v{standard.BLOCK_VERSION}.", "axiom repo standard v0."
        ),
        encoding="utf-8",
    )
    found = {f.code: f for f in core.findings(root)}
    outdated = found["context.standard-outdated"]
    assert "v0" in outdated.message and f"v{standard.BLOCK_VERSION}" in outdated.message
    assert "context sync" in outdated.remediation


def test_unbalanced_markers_are_left_alone_and_fail_the_check(tmp_path):
    text = AUTHORED_HEAD + standard.BLOCK_BEGIN + "\nhalf a block\n" + AUTHORED_TAIL
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    by = {r.target: r.status for r in core.sync(tmp_path, write=True)}
    assert by[core.STANDARD_TARGET] == "conflict"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == text
    assert core.has_drift(core.check(tmp_path))
    assert "context.standard-unbalanced" in {f.code for f in core.findings(tmp_path)}


def test_every_generated_tool_file_carries_the_block(tmp_path):
    root = _adopted(tmp_path)
    block = standard.render_block()
    for t in generators.TARGETS:
        text = (root / t.relpath).read_text(encoding="utf-8")
        assert block in text, f"{t.relpath} does not carry the managed block"
    # Claude Code reads AGENTS.md through the CLAUDE.md symlink.
    assert block in (root / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_block_is_short_enough_to_follow_and_covers_the_coaching():
    block = standard.render_block()
    assert 40 <= len(block.splitlines()) <= 70
    for point in (
        "Plan before you write code",
        "one small step",
        "Run the tests after every step",
        "Commit small",
        "Never hand-edit a generated file",
        "cannot be undone",
        "Axiom vault",
        "gold",
        "When you are stuck, say so",
        "plain words",
        "axi context check",
        "axi hygiene stat repo",
        "axi memory show",
    ):
        assert point in block, f"block lost {point!r}"
    # Plain links only: a root-relative link would be rebased per tool file.
    assert "](" not in block
