# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``release.repo_lineage``: origin, mirror, fork or archived, from evidence only.

A repository is classified from what the forge says about it (``fork``,
``mirror_url``, ``parent``; GitLab's pull-mirror flag and push-mirror list)
and from shared-commit evidence (the head of one present in the other's
history). Declared pairs always win. Anything the evidence does not settle
stays ``unverified``; nothing is guessed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.extensions.builtins.release import repo_lineage as rl
from axiom.extensions.builtins.release.skills import bind
from axiom.infra.skills import SkillContext, SkillRegistry


def _by(result):
    return {r["repo"]: r for r in result["repos"]}


def _meta(name, **over):
    return {"full_name": name, "fork": False, "archived": False, "mirror_url": None, **over}


class TestMetadata:
    def test_plain_metadata_is_an_origin(self):
        got = _by(rl.classify([_meta("org/a")]))
        assert got["org/a"]["classification"] == "origin"
        assert got["org/a"]["evidence"]

    def test_a_fork_names_its_parent(self):
        got = _by(rl.classify([_meta("org/b", fork=True, parent={"full_name": "up/b"})]))
        assert got["org/b"]["classification"] == "fork"
        assert got["org/b"]["of"] == "up/b"

    def test_a_pull_mirror_is_a_mirror(self):
        got = _by(rl.classify([_meta("org/c", mirror_url="https://gitlab.example.org/g/c.git")]))
        assert got["org/c"]["classification"] == "mirror"
        assert got["org/c"]["of"] == "gitlab.example.org/g/c"

    def test_archived_wins_over_metadata_shape(self):
        got = _by(rl.classify([_meta("org/d", archived=True, fork=True)]))
        assert got["org/d"]["classification"] == "archived"

    def test_gitlab_pull_mirror_and_fork_shapes(self):
        got = _by(
            rl.classify(
                [
                    {
                        "path_with_namespace": "g/m",
                        "mirror": True,
                        "import_url": "https://github.com/o/m.git",
                    },
                    {
                        "path_with_namespace": "g/f",
                        "forked_from_project": {"path_with_namespace": "g/up"},
                    },
                ],
                default_host="gitlab.example.org",
            )
        )
        assert got["g/m"]["classification"] == "mirror" and got["g/m"]["of"] == "github.com/o/m"
        assert got["g/f"]["classification"] == "fork" and got["g/f"]["of"] == "g/up"

    def test_a_push_mirror_target_in_the_set_is_a_mirror(self):
        got = _by(
            rl.classify(
                [
                    {
                        "full_name": "org/src",
                        "fork": False,
                        "push_mirrors": ["https://github.com/org/copy.git"],
                    },
                    _meta("org/copy"),
                ]
            )
        )
        assert got["org/copy"]["classification"] == "mirror" and got["org/copy"]["of"] == "org/src"
        assert got["org/src"]["classification"] == "origin"

    def test_no_metadata_stays_unverified(self):
        got = _by(rl.classify(["org/e"]))
        assert got["org/e"]["classification"] == "unverified"


class TestDeclared:
    def test_declared_pairs_win_over_metadata(self):
        declared = [
            {
                "origin": {"host": "github.com", "repo": "org/f"},
                "mirror": {"host": "github.com", "repo": "org/g"},
            }
        ]
        got = _by(rl.classify([_meta("org/f", fork=True), _meta("org/g")], declared=declared))
        assert got["org/f"]["classification"] == "origin"
        assert got["org/g"]["classification"] == "mirror" and got["org/g"]["of"] == "org/f"
        assert any("declared" in e for e in got["org/g"]["evidence"])


class TestSharedCommits:
    def _history(self, table, reads=None):
        def read(host, repo):
            if reads is not None:
                reads.append(repo)
            return table.get(repo)

        return read

    def test_a_copy_whose_head_lags_in_the_others_history_is_a_mirror(self):
        hist = {"org/main": ["c3", "c2", "c1"], "org/copy": ["c2", "c1"]}
        got = _by(rl.classify([_meta("org/main"), _meta("org/copy")], history=self._history(hist)))
        assert got["org/copy"]["classification"] == "mirror" and got["org/copy"]["of"] == "org/main"
        assert got["org/main"]["classification"] == "origin"
        assert any("c2" in e for e in got["org/copy"]["evidence"])

    def test_identical_histories_cannot_say_which_is_the_origin(self):
        hist = {"org/x": ["c2", "c1"], "org/y": ["c2", "c1"]}
        got = _by(rl.classify([_meta("org/x"), _meta("org/y")], history=self._history(hist)))
        assert got["org/x"]["classification"] == "unverified"
        assert got["org/y"]["classification"] == "unverified"
        assert got["org/x"]["same_lineage"] == ["org/y"]

    def test_diverged_or_unreadable_histories_are_no_evidence(self):
        hist = {"org/p": ["p2", "base"], "org/q": ["q2", "base"], "org/r": None}
        got = _by(
            rl.classify(
                [_meta("org/p"), _meta("org/q"), _meta("org/r")], history=self._history(hist)
            )
        )
        assert {r["classification"] for r in got.values()} == {"origin"}

    def test_history_reads_are_bounded_and_skip_settled_repos(self):
        reads: list[str] = []
        hist = {f"org/r{i}": [f"h{i}"] for i in range(5)}
        repos = [_meta(f"org/r{i}") for i in range(5)] + [_meta("org/fk", fork=True)]
        result = rl.classify(repos, history=self._history(hist, reads), max_history_reads=3)
        assert len(reads) == 3 and "org/fk" not in reads
        assert result["truncated"] is True


class TestSkill:
    def _ctx(self, tmp_path: Path) -> SkillContext:
        registry = SkillRegistry()
        bind(registry)
        return SkillContext(
            registry=registry, state_dir=tmp_path, logger=logging.getLogger("test.lineage")
        )

    def test_registered_as_a_read_and_invocable_through_the_registry(self, tmp_path):
        ctx = self._ctx(tmp_path)
        spec = ctx.registry.spec("release.repo_lineage")
        assert spec is not None and spec.side_effects is False and spec.risk == "read"
        result = ctx.registry.invoke("release.repo_lineage", {"repos": [_meta("org/a")]}, ctx)
        assert result.ok, result.errors
        assert result.value["repos"][0]["classification"] == "origin"
        assert result.value["counts"]["origin"] == 1

    def test_bad_input_is_refused(self, tmp_path):
        ctx = self._ctx(tmp_path)
        result = ctx.registry.invoke("release.repo_lineage", {"repos": "org/a"}, ctx)
        assert not result.ok
