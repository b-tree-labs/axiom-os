# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi hygiene stat repo`: the repo standard's Check layer (ADR-221)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from axiom.extensions.builtins.context import core as context_core
from axiom.extensions.builtins.hygiene import _legacy_cli
from axiom.extensions.builtins.hygiene.repo_standard import audit_repo_standard

AGENTS = """\
# Example

## What this repo is

Text.

## What this repo is not

Text.

## Where code goes

Text.

## Safety rules

Text.

## How to run and test

Text.
"""

WORKFLOW = """\
name: CI
on: [push, pull_request]
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs: {}
"""


def _standard_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    for name in ("README.md", "CONTRIBUTING.md", "LICENSE", ".editorconfig", ".gitignore"):
        (root / name).write_text("x\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "CODEOWNERS").write_text("* @someone\n", encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    (root / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: context\n"
        "        entry: axi context check\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "tests").mkdir()
    context_core.init(root)
    return root


def test_a_repo_that_meets_the_standard_has_no_findings(tmp_path):
    assert audit_repo_standard(_standard_repo(tmp_path)) == []


def test_an_empty_directory_is_told_every_required_file(tmp_path):
    checks = {f.check for f in audit_repo_standard(tmp_path)}
    assert {"repo_required_file", "repo_ci_workflow", "repo_docs_dir", "repo_tests_dir"} <= checks
    missing = {f.message for f in audit_repo_standard(tmp_path) if f.check == "repo_required_file"}
    assert "AGENTS.md is missing" in missing and "CODEOWNERS is missing" in missing


def test_a_workflow_without_a_concurrency_group_is_flagged(tmp_path):
    root = _standard_repo(tmp_path)
    (root / ".github" / "workflows" / "ci.yml").write_text(
        WORKFLOW.replace("concurrency:", "x-concurrency:"), encoding="utf-8"
    )
    assert "repo_ci_concurrency" in {f.check for f in audit_repo_standard(root)}


def test_a_release_workflow_needs_no_concurrency_group(tmp_path):
    root = _standard_repo(tmp_path)
    (root / ".github" / "workflows" / "publish.yml").write_text(
        "name: Publish\non:\n  push:\n    tags: ['v*']\njobs: {}\n", encoding="utf-8"
    )
    assert audit_repo_standard(root) == []


def test_a_missing_authored_section_is_named(tmp_path):
    root = _standard_repo(tmp_path)
    agents = root / "AGENTS.md"
    # The managed block mentions tests too; its heading must not stand in for
    # the authored section, so exactly this one finding comes back.
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("## How to run and test", "## Notes"),
        encoding="utf-8",
    )
    msgs = [f.message for f in audit_repo_standard(root) if f.check == "repo_agents_section"]
    assert msgs == ["AGENTS.md has no 'how to run and test' section"]


def test_a_stale_managed_block_is_reported_through_the_context_findings(tmp_path):
    root = _standard_repo(tmp_path)
    agents = root / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("one small step", "a big step"),
        encoding="utf-8",
    )
    assert "context_standard_outdated" in {f.check for f in audit_repo_standard(root)}


def test_stat_repo_strict_exits_nonzero_on_warnings(tmp_path, capsys):
    parser = _legacy_cli.build_parser()
    args = parser.parse_args(["repo", "--repo", str(tmp_path), "--strict", "--json"])
    assert _legacy_cli._cmd_repo(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"]

    ok = _standard_repo(tmp_path / "ok")
    args = parser.parse_args(["repo", "--repo", str(ok), "--strict"])
    assert _legacy_cli._cmd_repo(args) == 0
