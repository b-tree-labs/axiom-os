# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.publish`` to a wiki target — a local bare repository stands in
for the wiki remote, so git runs for real and nothing touches the network."""

from __future__ import annotations

import copy
import json
import logging
import subprocess

import pytest

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills import publish
from axiom.extensions.builtins.program.skills._gantt import render_gantt_png
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM
from axiom.infra.skills import SkillContext, SkillRegistry


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def wiki_remote(tmp_path):
    remote = tmp_path / "wiki.git"
    _git("init", "--bare", "--quiet", "-b", "master", str(remote))
    return remote


@pytest.fixture
def node(tmp_path):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    raw = copy.deepcopy(GENERIC_PROGRAM)
    raw["program"]["publish"] = {"wiki": {"repo": "example-org/program-tracker", "page": "Program-Status"}}
    raw["schedule"][0]["delivery"] = "in_progress"
    (state / "program" / "data.json").write_text(json.dumps(raw), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(), state_dir=state, logger=logging.getLogger("test.program.publish"),
        user_prompt=None, surface="cli",
    )


def _commits(remote):
    try:
        return _git("--git-dir", str(remote), "rev-list", "--count", "master").strip()
    except subprocess.CalledProcessError:
        return "0"


class TestWikiPublish:
    def test_first_publish_commits_the_page_and_gantt(self, node, wiki_remote):
        result = publish.run({"_remote": str(wiki_remote)}, node)
        assert result.ok, result.errors
        assert result.value["committed"] is True
        assert _commits(wiki_remote) == "1"
        files = _git("--git-dir", str(wiki_remote), "ls-tree", "--name-only", "master").split()
        assert "Program-Status.md" in files
        assert "Program-Status-gantt.png" in files

    def test_a_second_publish_of_unchanged_data_makes_no_commit(self, node, wiki_remote):
        publish.run({"_remote": str(wiki_remote)}, node)
        again = publish.run({"_remote": str(wiki_remote)}, node)
        assert again.ok and again.value["committed"] is False
        assert again.actions_taken == []
        assert _commits(wiki_remote) == "1"

    def test_a_change_makes_one_more_commit(self, node, wiki_remote):
        publish.run({"_remote": str(wiki_remote)}, node)
        path = node.state_dir / "program" / "data.json"
        raw = json.loads(path.read_text())
        raw["schedule"][0]["pct"] = 60
        path.write_text(json.dumps(raw))
        assert publish.run({"_remote": str(wiki_remote)}, node).value["committed"] is True
        assert _commits(wiki_remote) == "2"

    def test_no_wiki_declared_is_refused(self, node):
        path = node.state_dir / "program" / "data.json"
        raw = json.loads(path.read_text())
        raw["program"].pop("publish")
        path.write_text(json.dumps(raw))
        result = publish.run({"dry_run": True}, node)
        assert not result.ok and "publish.wiki" in result.errors[0]

    def test_a_git_failure_never_carries_the_token(self, node, tmp_path):
        result = publish.run({"_remote": str(tmp_path / "missing.git"), "_token": "ghs_SECRET"}, node)
        assert not result.ok
        assert "ghs_SECRET" not in " ".join(result.errors)

    def test_no_app_declared_is_refused_without_touching_git(self, node):
        result = publish.run({}, node)
        assert not result.ok and "github_app" in result.errors[0]

    def test_dry_run_renders_locally(self, node, tmp_path):
        out = tmp_path / "out"
        result = publish.run({"dry_run": True, "out": str(out)}, node)
        assert result.ok
        page = (out / "Program-Status.md").read_text()
        assert "# Example Program" in page
        assert "![Program schedule as a Gantt chart](Program-Status-gantt.png)" in page
        assert "in\\_progress" in page
        assert (out / "Program-Status-gantt.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


class TestGantt:
    def test_byte_stable_for_the_same_data(self):
        data = ProgramData(raw=copy.deepcopy(GENERIC_PROGRAM))
        first, second = render_gantt_png(data), render_gantt_png(data)
        assert first is not None and first == second

    def test_no_mermaid_anywhere(self):
        page = publish.render_wiki_markdown(ProgramData(raw=copy.deepcopy(GENERIC_PROGRAM)), gantt_file="g.png")
        assert "mermaid" not in page.lower()


class TestMarkdownEscaping:
    def test_data_cannot_inject_table_structure(self):
        raw = copy.deepcopy(GENERIC_PROGRAM)
        raw["schedule"][0]["label"] = "a | b [x](javascript:alert(1))"
        page = publish.render_wiki_markdown(ProgramData(raw=raw), gantt_file=None)
        assert "a \\| b \\[x\\](javascript:alert(1))" in page


def test_publish_is_a_registered_cli_only_skill():
    from axiom.extensions.builtins.program import cli
    from axiom.extensions.builtins.program.skills import bind

    registry = SkillRegistry()
    bind(registry)
    spec = registry.spec("program.publish")
    assert spec.surfaces == ("cli",) and spec.side_effects
    args = cli._parser().parse_args(["publish", "--dry-run"])
    assert args.capability == "program.publish" and args.dry_run is True
